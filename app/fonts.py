"""オーバーレイに使うフォントの解決。

憲章「Macに閉じない」に従い、パスをコードに埋めずに候補から選ぶ。
理想はリポジトリ同梱フォント（両OSで同じ見た目になる）で、無ければ
OS標準の等幅フォントに落とす。どれを使ったかは必ず外から見えるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_DIR = REPO_ROOT / "assets" / "fonts"

# 上から順に探す。同梱フォントが最優先
CANDIDATES: list[tuple[str, str]] = [
    (str(BUNDLED_DIR / "DejaVuSansMono-Bold.ttf"), "リポジトリ同梱"),
    (str(BUNDLED_DIR / "DejaVuSansMono.ttf"), "リポジトリ同梱"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "Linux (Debian系)"),
    ("/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf", "Linux (RHEL系)"),
    ("/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf", "Linux (Arch系)"),
    ("/System/Library/Fonts/Monaco.ttf", "macOS 標準"),
    ("/System/Library/Fonts/Menlo.ttc", "macOS 標準"),
]


@dataclass
class FontChoice:
    path: str
    origin: str
    bundled: bool

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "origin": self.origin,
            "bundled": self.bundled,
            "note": ""
            if self.bundled
            else "OS標準のフォントを使っています。移植先で見た目が変わる可能性があります",
        }


def resolve(configured: str = "") -> FontChoice | None:
    """使うフォントを1つ決める。見つからなければ None（＝オーバーレイ不可）。"""
    if configured:
        p = Path(configured).expanduser()
        if p.exists():
            return FontChoice(str(p), "config.toml の指定", bundled=False)

    for path, origin in CANDIDATES:
        if Path(path).exists():
            return FontChoice(path, origin, bundled=path.startswith(str(BUNDLED_DIR)))
    return None
