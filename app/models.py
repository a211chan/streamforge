"""API のリクエスト/レスポンススキーマ。

既定値をここに集約している。UIは何も指定せずに送出できる（憲章 Principle 2 の前段）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field


class EncodeSettings(BaseModel):
    """エンコード条件。テンプレートに保存されるのはこの内容そのもの。"""

    mode: Literal["encode", "passthrough"] = "encode"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    video_encoder: Literal["libx264", "h264_videotoolbox"] = "libx264"
    bitrate_kbps: int = 4500
    maxrate_kbps: int | None = None  # None なら bitrate と同じ
    bufsize_kbps: int | None = None  # None なら bitrate の2倍
    gop_sec: float = 2.0
    preset: str = "veryfast"
    bframes: int = 2
    profile: str = "high"
    tune: str | None = None

    audio_codec: Literal["aac", "opus"] = "aac"
    audio_bitrate_kbps: int = 128
    samplerate: int = 48000
    channels: int = 2

    @property
    def effective_maxrate(self) -> int:
        return self.maxrate_kbps or self.bitrate_kbps

    @property
    def effective_bufsize(self) -> int:
        return self.bufsize_kbps or self.bitrate_kbps * 2

    def summary(self) -> str:
        if self.mode == "passthrough":
            return "再エンコードなし（-c copy）"
        return (
            f"{self.width}x{self.height}@{self.fps} / {self.bitrate_kbps}k / "
            f"GOP {self.gop_sec}s / {self.preset}"
        )

    # ------------------------------------------------ テンプレート化のための正規化

    def normalized(self) -> dict:
        """同一性を判定するための正規形。

        maxrate/bufsize の None は既定値に埋めてから比較する。そうしないと
        「4500k / maxrate 未指定」と「4500k / maxrate 4500k」が別物になり、
        同じ設定なのにテンプレートが2件できてしまう。
        passthrough のときは映像パラメータを比較対象から外す（意味を持たないため）。
        """
        if self.mode == "passthrough":
            return {"mode": "passthrough"}

        data = self.model_dump()
        data["maxrate_kbps"] = self.effective_maxrate
        data["bufsize_kbps"] = self.effective_bufsize
        return dict(sorted(data.items()))

    def settings_hash(self) -> str:
        canonical = json.dumps(self.normalized(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def auto_name(self) -> str:
        """テンプレートの自動命名。ユーザーに名付けを強いないための仕組み。"""
        if self.mode == "passthrough":
            return "Passthrough（再エンコードなし）"

        codec = "H.264"
        if self.video_encoder == "h264_videotoolbox":
            codec += "(VT)"
        gop = f"{self.gop_sec:g}"
        return f"{self.height}p{self.fps} · {self.bitrate_kbps}k · {codec} · GOP{gop}s"


class SourceSpec(BaseModel):
    """映像ソース。

    - testsrc  : 内蔵テストパターン。ファイル不要で常に動く（既定）
    - file     : media_dir に置いたファイルをループ再生
    - live_url : 公開ライブソースを取り込む
    """

    type: Literal["testsrc", "file", "live_url"] = "testsrc"

    # testsrc
    pattern: Literal["testsrc2", "smptebars"] = "testsrc2"
    with_tone: bool = True

    # file — media_dir からの相対パス。絶対パスは受け付けない
    path: str = ""
    loop: bool = True
    # ループの継ぎ目の扱い。auto は素材が短ければメモリ保持に切り替える
    seam: Literal["auto", "reload", "buffer"] = "auto"

    # live_url
    url: str = ""
    reconnect: bool = True
    # 実時間で読むか。auto は再生リストを見て VOD/ライブを判定する
    pace: Literal["auto", "realtime", "asfast"] = "auto"
    # 素材を何秒ぶん手前から読むか（ライブHLSのみ。0で無効）。
    #
    # 時計は drawtext を通る時点の壁時計を焼くので、**バッファが焼き込みより
    # 手前にある限り、測定遅延には乗らない**。素材が一瞬詰まってもエンコードが
    # 途切れないための保険で、遅延測定とは両立する。
    # 実測: 既定は供給停止に 0.1秒しか耐えられないが、5〜10秒積むとその秒数ぶん耐える
    prebuffer_sec: int = 5
    # prepare_live_source() が決める。ライブ端から何セグメント手前で読み始めるか
    start_index: int = 0

    def summary(self) -> str:
        if self.type == "file":
            return f"file:{self.path}" + ("（ループ）" if self.loop else "")
        if self.type == "live_url":
            return f"live:{self.url}"
        return f"testsrc:{self.pattern}" + ("+tone" if self.with_tone else "")

    def has_own_audio(self) -> bool:
        """入力自身が音声を持ちうるか。testsrc だけは別入力でトーンを足す。"""
        return self.type in ("file", "live_url")


POSITIONS = ("top-left", "top-center", "top-right",
             "bottom-left", "bottom-center", "bottom-right")

INFO_ITEMS = ("protocol", "resolution", "codec", "bitrate", "gop", "preset", "job")


class OverlaySettings(BaseModel):
    """映像に焼き込む情報（機能設計書 F-4）。

    受信側のスクリーンショット1枚から「いつの映像か」と「どの条件で送ったか」が
    読めることが目的。
    """

    clock_enabled: bool = True
    clock_mode: Literal["wallclock", "framecount", "both"] = "both"
    clock_position: Literal[POSITIONS] = "top-center"  # type: ignore[valid-type]
    clock_scale: float = 16.0   # フォントサイズ = 映像高 / この値

    info_enabled: bool = True
    info_items: list[str] = Field(default_factory=lambda: list(INFO_ITEMS))
    info_position: Literal[POSITIONS] = "bottom-left"  # type: ignore[valid-type]
    info_scale: float = 28.0

    def enabled(self) -> bool:
        return self.clock_enabled or self.info_enabled


class JobCreate(BaseModel):
    """送出先は URL を直接渡すか、保存済み Target の id で指定する。

    id 指定なら平文URLがリクエストを通らないので、履歴からの再実行で
    ストリームキーがブラウザ側に出てこない。
    """

    target_url: str = ""
    target_id: int | None = None
    target_label: str = ""
    encode: EncodeSettings = Field(default_factory=EncodeSettings)
    template_id: int | None = None   # 指定するとテンプレートの設定を使う（encode より優先）
    bearer_token: str = ""           # WHIP の Authorization（target_id 指定時は保存済みを使う）
    # 配信先が WebRTC で配信する場合（Ceeblue 等）。Bフレーム無効・baseline に落とす
    webrtc_delivery: bool = False
    overlay: OverlaySettings = Field(default_factory=OverlaySettings)
    source: SourceSpec = Field(default_factory=SourceSpec)
    duration_sec: int | None = None  # None は設定ファイルの既定、0 は無期限
    auto_restart: bool = False


class PreviewRequest(JobCreate):
    """実行せずコマンドだけ生成する。UIのプレビュー用。"""


class TargetSave(BaseModel):
    url: str = Field(..., min_length=1)
    label: str = ""
    protocol: str | None = None   # 手動上書き。None なら自動判定に従う
    bearer_token: str = ""        # WHIP の Authorization。暗号化して保存する


class TemplatePatch(BaseModel):
    name: str | None = None
    is_pinned: bool | None = None
