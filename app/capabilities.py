"""FFmpeg の能力検出。

「動くはず」を前提にせず、起動時に実物へ問い合わせる。
WHIP muxer の有無もここで見ており、対応ビルドに差し替えた時点で
UI 側が自動的に解禁される構造にしてある。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field


@dataclass
class Capabilities:
    ffmpeg_path: str
    available: bool = False
    version: str = ""
    protocols: dict[str, bool] = field(default_factory=dict)
    muxers: dict[str, bool] = field(default_factory=dict)
    filters: dict[str, bool] = field(default_factory=dict)
    encoders: dict[str, bool] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        # WHIP はプロトコルではなく muxer として実装されているので、確認先が違う
        ready = [p for p in ("rtmp", "srt") if self.protocols.get(p)]
        if self.muxers.get("whip"):
            ready.append("whip")
        d["ready_protocols"] = sorted(ready)
        d["whip_ready"] = bool(self.muxers.get("whip"))
        d["overlay_ready"] = bool(self.filters.get("drawtext"))
        return d


def _run(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return out.stdout + out.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect(ffmpeg_path: str) -> Capabilities:
    caps = Capabilities(ffmpeg_path=ffmpeg_path)

    version_out = _run([ffmpeg_path, "-hide_banner", "-version"])
    if not version_out:
        caps.error = f"FFmpeg を実行できません: {ffmpeg_path}"
        return caps

    caps.available = True
    m = re.search(r"ffmpeg version (\S+)", version_out)
    caps.version = m.group(1) if m else "unknown"

    # -protocols は "Input:" / "Output:" の2ブロックに分かれ、各行は空白+名前。
    protocols_out = _run([ffmpeg_path, "-hide_banner", "-protocols"])
    names = {line.strip() for line in protocols_out.splitlines() if line.startswith("  ")}
    for p in ("rtmp", "rtmps", "srt", "tls", "https", "file", "pipe"):
        caps.protocols[p] = p in names

    muxers_out = _run([ffmpeg_path, "-hide_banner", "-muxers"])
    for mux in ("flv", "mpegts", "whip", "tee", "matroska"):
        caps.muxers[mux] = _has_entry(muxers_out, mux)

    filters_out = _run([ffmpeg_path, "-hide_banner", "-filters"])
    for f in ("drawtext", "testsrc2", "setpts", "asetpts"):
        caps.filters[f] = _has_entry(filters_out, f)

    encoders_out = _run([ffmpeg_path, "-hide_banner", "-encoders"])
    for e in ("libx264", "h264_videotoolbox", "aac", "libopus"):
        caps.encoders[e] = _has_entry(encoders_out, e)

    return caps


def _has_entry(listing: str, name: str) -> bool:
    """`-muxers` 等の一覧はフラグ列 + 空白 + 名前 + 空白 + 説明 の形。

    部分一致だと 'aac' が 'aac_at' に、'flv' が 'flv_metadata' に当たってしまうため
    2列目の完全一致で見る。
    """
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False
