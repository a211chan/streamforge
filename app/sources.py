"""映像ソースの管理（機能設計書 F-2）。

ファイルは `media_dir` に置いたものを一覧するだけの単純な作りにしている。
Web UI からアップロードもできるが、**Finder や scp で直接置いたファイルも
そのまま出る**。母艦が手元の Mac である利点を消さないため。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# FFmpeg に渡して意味のある拡張子だけを見せる
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".ts", ".m2ts", ".mxf", ".webm", ".flv", ".m4v"}

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2GB


@dataclass
class MediaFile:
    name: str
    size_bytes: int
    modified_at: float

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "size_human": _human(self.size_bytes),
            "modified_at": self.modified_at,
        }


def catalog() -> list[dict]:
    """UI に返すカタログ。caution を必ず持たせる。"""
    return [{"caution": False, **entry} for entry in CATALOG]


def list_media(media_dir: Path) -> list[dict]:
    if not media_dir.exists():
        return []
    files = [
        MediaFile(p.name, p.stat().st_size, p.stat().st_mtime)
        for p in sorted(media_dir.iterdir())
        if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
    ]
    files.sort(key=lambda f: f.modified_at, reverse=True)
    return [f.as_dict() for f in files]


def safe_upload_path(media_dir: Path, filename: str) -> Path:
    """アップロード先を media_dir の直下に固定する。

    ブラウザから来たファイル名にはディレクトリ区切りが混ざりうるので、
    名前部分だけを取り出して使う。
    """
    name = Path(filename).name
    if not name or name.startswith("."):
        raise ValueError("ファイル名が不正です")
    if Path(name).suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError(
            f"対応していない拡張子です（{', '.join(sorted(MEDIA_SUFFIXES))}）"
        )
    return media_dir / name


# 既定で載せる公開テストストリーム。
#
# 方針: ライセンスや出所がはっきりしていて**テスト用途で公開されている**ものだけを置く。
# 放送局の linear チャンネルなど、再送出の権利関係が曖昧なものは含めない
# （`caution` フラグと UI の ⚠ 表示は、そういうものを個人用に足す場合に備えて残してある）。
#
# 任意のURLは上級者オプション扱いで、規約の確認は利用者の責任である旨を UI に出す。
#
# `live` が True のものは 24時間流れ続けるので、長時間の耐久試験に使える。
# False のものは尺のある VOD で、毎回同じ内容が流れるため再現性のある比較に向く。
#
# 到達性は 2026-09-13 に ffprobe で実測確認済み。外部サービスなので落ちることはある。
CATALOG = [
    # ---- 24時間ライブ ----
    {
        "id": "unified-live",
        "name": "Unified Streaming ライブ (HLS)",
        "url": "https://demo.unified-streaming.com/k8s/live/stable/live.isml/.m3u8",
        "live": True,
        "note": "H.264 1280x720 25fps。配信サーバーベンダーが常設しているテスト用ライブchannel",
    },
    {
        "id": "unified-scte35",
        "name": "Unified Streaming SCTE-35入り (HLS)",
        "url": "https://demo.unified-streaming.com/k8s/live/stable/scte35.isml/.m3u8",
        "live": True,
        "note": "H.264 1280x720 25fps。SCTE-35マーカーが入る。広告挿入まわりの検証向け",
    },
    {
        "id": "shaka-live",
        "name": "Shaka Player ライブ (HLS)",
        "url": "https://storage.googleapis.com/shaka-live-assets/player-source.m3u8",
        "live": True,
        "note": "H.264 1280x720。Shaka Player プロジェクトが公開している常設ライブ",
    },
    {
        "id": "mux-ll-hls",
        "name": "Mux 低遅延HLS (Big Buck Bunny)",
        "url": "https://stream.mux.com/v69RSHhFelSm4701snP22dYz2jICy4E4FUyk02rW4gxRM.m3u8",
        "live": True,
        "note": "H.264 1280x720 24fps / fMP4。Big Buck Bunny のループ＋タイマー。12時間ごとに再起動",
    },
    # ---- VOD（尺あり・再現性のある比較向け） ----
    {
        "id": "bbb-hls",
        "name": "Big Buck Bunny (HLS / VOD)",
        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        "live": False,
        "note": "Blender Foundation の CC-BY 作品。Mux が公開しているテスト用ストリーム",
    },
    {
        "id": "apple-bipbop",
        "name": "Apple BipBop (HLS / VOD)",
        "url": "https://devstreaming-cdn.apple.com/videos/streaming/examples/"
               "bipbop_4x3/bipbop_4x3_variant.m3u8",
        "live": False,
        "note": "Apple が開発者向けに公開しているサンプルストリーム",
    },
    {
        "id": "tears-of-steel",
        "name": "Tears of Steel (HLS / VOD)",
        "url": "https://demo.unified-streaming.com/k8s/features/stable/video/"
               "tears-of-steel/tears-of-steel.ism/.m3u8",
        "live": False,
        "note": "Blender Foundation の CC-BY 作品。Unified Streaming のデモ",
    },
]


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


# ---------------------------------------------------------------- ライブ判定

PLAYLIST_TIMEOUT = 2.0
_playlist_cache: dict[str, tuple[float, bool | None]] = {}
_CACHE_SEC = 60.0


def is_live_stream(url: str) -> bool | None:
    """HLS の再生リストを見て、ライブなら True / VOD なら False / 不明なら None。

    判定は `#EXT-X-ENDLIST` の有無。終端マーカーがあるものは尺が決まっている＝VOD。

    なぜ要るか: VOD の URL を「外部ライブソース」として読むと、FFmpeg は
    取れるだけ取ってしまい **30倍速以上で送出先へ叩き込む**。実際にこれで
    speed=11x のまま送出され、ログも溢れた。ライブかどうかで実時間読み(-re)の
    要否が正反対になるので、貼られたURLから自動で決める。
    """
    if not url.startswith(("http://", "https://")):
        return None

    cached = _playlist_cache.get(url)
    if cached and time.monotonic() - cached[0] < _CACHE_SEC:
        return cached[1]

    verdict = _fetch_live_flag(url)
    _playlist_cache[url] = (time.monotonic(), verdict)
    return verdict


def _fetch_live_flag(url: str) -> bool | None:
    body = _get(url)
    if body is None or "#EXTM3U" not in body:
        return None

    # マスタープレイリストなら、最初のバリアントを見に行く
    if "#EXT-X-STREAM-INF" in body:
        variant = next(
            (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")),
            None,
        )
        if variant is None:
            return None
        body = _get(urllib.parse.urljoin(url, variant)) or ""

    if "#EXT-X-ENDLIST" in body:
        return False
    if "#EXT-X-PLAYLIST-TYPE:VOD" in body.upper():
        return False
    return True


def _get(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StreamForge/1.0"})
        with urllib.request.urlopen(req, timeout=PLAYLIST_TIMEOUT) as res:
            return res.read(262144).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def resolve_pace(source) -> tuple[bool, str]:
    """(実時間読みするか, 理由) を返す。"""
    if source.type != "live_url":
        return True, ""
    if source.pace == "realtime":
        return True, "実時間読みを明示指定"
    if source.pace == "asfast":
        return False, "実時間読みしない指定"

    live = is_live_stream(source.url)
    if live is True:
        return False, "再生リストにENDLISTが無いのでライブと判定（実時間読みしません）"
    if live is False:
        return True, (
            "再生リストにENDLISTがあるのでVODと判定。実時間読み(-re)を付けます"
            "（付けないと数十倍速で送出してしまいます）"
        )
    return True, "ライブかVODか判定できないため、安全側に倒して実時間読みします"


# ------------------------------------------------------- バリアントの選択

def pick_variant(master_url: str, target_height: int) -> tuple[str, str]:
    """HLSマスタープレイリストから、目的の解像度に合うバリアントを選ぶ。

    FFmpeg にマスターを渡すと **先頭のバリアント**（多くの配信で最低画質）を掴む。
    実測では 224x100 を選び、それを 1280x720 に拡大していたため画質が崩れていた。
    さらにマスターを渡すと全バリアントを開きにいくため、起動が遅い
    （実測 23秒 → バリアント直指定で 4.5秒）。

    どちらも「こちらで1本選んで渡す」だけで解決する。

    戻り値は (使うURL, 説明)。マスターでなければ元のURLをそのまま返す。
    """
    body = _get(master_url)
    if not body or "#EXT-X-STREAM-INF" not in body:
        return master_url, ""

    variants = _parse_variants(body, master_url)
    if not variants:
        return master_url, ""

    # 目的の高さ以上で最も小さいもの。無ければ一番大きいもの。
    # 拡大は画質が崩れるので避け、無駄な縮小もしない
    fits = [v for v in variants if v["height"] and v["height"] >= target_height]
    chosen = min(fits, key=lambda v: v["height"]) if fits else max(
        variants, key=lambda v: (v["height"] or 0, v["bandwidth"])
    )

    note = (
        f"マスタープレイリストから {chosen['label']} のバリアントを選びました"
        f"（全{len(variants)}種）。FFmpegに任せると先頭＝最低画質を掴み、起動も遅くなります"
    )
    return chosen["url"], note


def _parse_variants(body: str, base_url: str) -> list[dict]:
    import re

    variants: list[dict] = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        url = next(
            (ln.strip() for ln in lines[i + 1:i + 4] if ln.strip() and not ln.startswith("#")),
            None,
        )
        if not url:
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        height = int(res.group(2)) if res else 0
        width = int(res.group(1)) if res else 0
        variants.append({
            "url": urllib.parse.urljoin(base_url, url),
            "height": height,
            "width": width,
            "bandwidth": int(bw.group(1)) if bw else 0,
            "label": f"{width}x{height}" if res else f"{(int(bw.group(1)) if bw else 0)//1000}kbps",
        })
    return variants


def segments_for_prebuffer(url: str, seconds: int) -> int:
    """指定秒ぶんの先読みに必要なセグメント数を返す。

    ライブHLSは既定でライブ端の3セグメント手前から読み始めるが、速度制限が
    無いとすぐ端に追いついてしまい、余裕が消える。手前から読み始めたうえで
    1倍速に保つと、その差がそのまま「素材の貯金」になる。
    """
    if seconds <= 0:
        return 0
    body = _get(url) or ""
    if "#EXT-X-STREAM-INF" in body:
        variant = next(
            (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")),
            None,
        )
        if variant:
            body = _get(urllib.parse.urljoin(url, variant)) or ""

    import re

    m = re.search(r"#EXT-X-TARGETDURATION:\s*(\d+)", body)
    target = int(m.group(1)) if m else 0
    if target <= 0:
        # 分からなければ2秒セグメントを仮定する。外しても致命的ではない
        target = 2
    import math

    return max(1, math.ceil(seconds / target))


def prepare_live_source(source, target_height: int):
    """送出前に、外部ライブソースについて決めることをまとめて決める。

    ネットワークを触る判断はここに集約し、コマンド生成側は純粋に保つ。
    戻り値は (調整後のSourceSpec, 説明の列)。
    """
    if source.type != "live_url":
        return source, []

    notes: list[str] = []
    url, variant_note = pick_variant(source.url, target_height)
    if variant_note:
        notes.append(variant_note)

    paced, pace_note = resolve_pace(source)

    # 素材の先読み。ライブ端から何セグメント手前で読み始めるかを決める。
    # 1倍速に保たないとすぐライブ端に追いついて余裕が消えるので、pace も上書きする
    start_index = 0
    if source.prebuffer_sec > 0:
        start_index = segments_for_prebuffer(url, source.prebuffer_sec)

    if start_index:
        paced = True
        notes.append(
            f"素材を約{source.prebuffer_sec}秒ぶん手前から読み、1倍速で保ちます"
            f"（ライブ端の{start_index}セグメント前）。"
            "素材が一瞬詰まってもエンコードが途切れません。"
            "時計はこのバッファより後に焼くので、測る遅延には乗りません"
        )
    elif pace_note:
        notes.append(pace_note)

    prepared = source.model_copy(update={
        "url": url,
        "pace": "realtime" if paced else "asfast",
        "start_index": start_index,
    })
    return prepared, notes


# --------------------------------------------------- ループの継ぎ目の扱い

# メモリに抱えるデコード済みフレームの上限（おおよそのバイト数）
SEAM_BUFFER_BUDGET = 900 * 1024 * 1024


def probe_duration(path: str) -> float | None:
    import json
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=10,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None


def resolve_seam(source, media_path: str, width: int, height: int, fps: int):
    """(メモリ保持でループするか, フレーム数, 説明) を返す。

    `-stream_loop` は継ぎ目で入力を開き直すため、**約200msのあいだ出力が止まる**
    （実測）。下流はバッファ枯渇を起こし、映像と音声が同時に一瞬乱れる。
    デコード済みフレームをメモリに持つ `loop` フィルタなら停止は起きない
    （実測 0回）が、そのぶんメモリを食うので短い素材にだけ使う。
    """
    if source.type != "file" or not source.loop or source.seam == "reload":
        return False, 0, ""

    duration = probe_duration(media_path)
    if duration is None:
        return False, 0, "素材の長さを取得できないため、通常のループにします"

    frames = max(1, int(duration * fps) + 1)
    estimated = frames * width * height * 3 // 2

    if source.seam == "buffer":
        return True, frames, f"継ぎ目対策でフレームを保持します（約{estimated // 1024 // 1024}MB）"

    if estimated > SEAM_BUFFER_BUDGET:
        return False, 0, (
            f"素材が長いため通常のループにします（保持すると約{estimated // 1024 // 1024}MB）。"
            "継ぎ目で約200msの途切れが出ます"
        )
    return True, frames, (
        f"継ぎ目で途切れないよう、デコード済みフレームを保持します"
        f"（{duration:.1f}秒 / 約{estimated // 1024 // 1024}MB）"
    )
