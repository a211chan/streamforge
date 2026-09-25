"""設定の読み込み。

OS固有のパスをコードに埋めないため、パスはすべてここを経由する
（憲章「Macに閉じない」）。config.toml が無くても既定値で動く。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.toml"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8080
    ffmpeg_path: str = "ffmpeg"
    data_dir: Path = field(default_factory=lambda: Path.home() / "StreamForge")
    media_dir: Path = field(default_factory=lambda: Path.home() / "StreamForge" / "media")
    log_dir: Path = field(default_factory=lambda: Path.home() / "StreamForge" / "logs")
    default_duration_sec: int = 7200
    log_tail_bytes: int = 5 * 1024 * 1024
    font_path: str = ""          # 空ならリポジトリ同梱→OS標準の順に自動で探す
    ntp_server: str = "time.apple.com"

    # セルフプレビュー（送出中の絵を送出側で見る）。
    # 既定で入れているのは、実測で追加コストが全体の -2%（誤差の範囲）だったため。
    # 切れるようにしてあるのは、非力な移植先で落とす余地を残すためだけ
    snapshot_enabled: bool = True
    snapshot_width: int = 480      # 480px で時計も設定行も読める（1枚 約11KB）
    snapshot_fps: float = 1.0      # 毎秒1枚。約90kbps で送出と帯域を奪い合わない

    @property
    def db_path(self) -> Path:
        return self.data_dir / "streamforge.db"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshot"

    @property
    def snapshot_path(self) -> Path:
        """送出は同時に1本なので、置き場も1つでよい。

        ジョブIDを入れないのは、コマンドを組み立てる時点ではまだIDが無いため
        （IDはDBへ INSERT した戻りで決まる）。代わりに起動時と終了時に消して、
        前のジョブの絵が残らないようにする。
        """
        return self.snapshot_dir / "current.jpg"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.log_dir, self.snapshot_dir):
            d.mkdir(parents=True, exist_ok=True)


def _expand(value: str) -> Path:
    return Path(value).expanduser()


def load_config(path: Path = CONFIG_PATH) -> Config:
    cfg = Config()
    if not path.exists():
        return cfg

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    server = raw.get("server", {})
    cfg.host = server.get("host", cfg.host)
    cfg.port = int(server.get("port", cfg.port))

    cfg.ffmpeg_path = raw.get("ffmpeg", {}).get("path", cfg.ffmpeg_path)

    paths = raw.get("paths", {})
    if "data_dir" in paths:
        cfg.data_dir = _expand(paths["data_dir"])
    if "media_dir" in paths:
        cfg.media_dir = _expand(paths["media_dir"])
    if "log_dir" in paths:
        cfg.log_dir = _expand(paths["log_dir"])

    job = raw.get("job", {})
    cfg.default_duration_sec = int(job.get("default_duration_sec", cfg.default_duration_sec))
    cfg.log_tail_bytes = int(job.get("log_tail_bytes", cfg.log_tail_bytes))

    overlay = raw.get("overlay", {})
    cfg.font_path = overlay.get("font_path", cfg.font_path)
    cfg.ntp_server = overlay.get("ntp_server", cfg.ntp_server)

    snapshot = raw.get("snapshot", {})
    cfg.snapshot_enabled = bool(snapshot.get("enabled", cfg.snapshot_enabled))
    cfg.snapshot_width = int(snapshot.get("width", cfg.snapshot_width))
    cfg.snapshot_fps = float(snapshot.get("fps", cfg.snapshot_fps))
    return cfg
