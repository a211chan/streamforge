"""配色が基準を割ったら落ちる。

シーンを足したり色を詰めたりすると、どれか1つが必ず沈む。目で見て決めた
つもりでも気づけないので、tools/check-contrast.py を常に通す。

シーンの一覧（theme.css / index.html / app.js）と、typewriter の線種の
割り当て（theme.css / style.css）も、片方だけ直ると静かに壊れるので見る。
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "tools" / "check-contrast.py"


class ThemeContrastTest(unittest.TestCase):
    def test_all_themes_meet_4_5_to_1(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECKER), "-q"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(
            result.returncode, 0,
            f"コントラストが基準に届きません:\n{result.stdout}{result.stderr}",
        )

    def test_scene_list_matches_everywhere(self) -> None:
        """シーンの一覧は theme.css・index.html・app.js で同じでなければならない。"""
        import re

        static = ROOT / "app" / "static"
        theme = set(re.findall(r'\[data-scene="([a-z]+)"\]',
                              (static / "theme.css").read_text(encoding="utf-8")))
        html = (static / "index.html").read_text(encoding="utf-8")
        buttons = set(re.findall(r'<button[^>]*data-scene="([a-z]+)"', html))
        head = set(re.findall(r"'([a-z]+)'",
                              re.search(r"var SCENES = \[(.*?)\]", html, re.S).group(1)))
        js = (static / "app.js").read_text(encoding="utf-8")
        js_scenes = set(re.findall(r"'([a-z]+)'",
                                   re.search(r"const SCENES = \[(.*?)\]", js, re.S).group(1)))

        # theme.css の light は :root に書くので [data-scene] には出ない
        expected = {"light", "dark", "terminal", "tactical", "typewriter"}
        self.assertEqual(theme | {"light"}, expected, "theme.css のシーン")
        self.assertEqual(buttons, expected, "index.html の切り替えボタン")
        self.assertEqual(head, expected, "index.html の先読みスクリプト")
        self.assertEqual(js_scenes, expected, "app.js の SCENES")

    def test_typewriter_series_line_styles_are_wired(self) -> None:
        """線種の割り当ては style.css に実在しなければならない。

        色だけでは5本を分けられないシーンなので、要件として文書に書くだけでは
        実装時に落ちる。theme.css のコメントと style.css の値を突き合わせる。
        """
        import re

        static = ROOT / "app" / "static"
        css = (static / "style.css").read_text(encoding="utf-8")
        expected = {
            1: "none", 2: "6 4", 3: "1 3",
            4: "10 3 1 3", 5: "10 3 1 3 1 3",
        }
        for n, dash in expected.items():
            m = re.search(r"\.series-%d\s*\{(.*?)\}" % n, css, re.S)
            self.assertIsNotNone(m, f".series-{n} が style.css にありません")
            body = m.group(1)
            self.assertIn(f"--series-color: var(--chart-{n})", body,
                          f".series-{n} の色が --chart-{n} ではありません")
            self.assertIn(f"--series-dash: {dash}", body,
                          f".series-{n} の線種が theme.css の記載と違います")
            self.assertIn("--series-marker:", body,
                          f".series-{n} にマーカーがありません")

    def test_components_do_not_hardcode_colors(self) -> None:
        """style.css は色を直接書かない（theme.css のトークン経由だけ）。"""
        import re

        css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css)
        self.assertEqual(literals, [], f"style.css に色が直接書かれています: {literals}")


if __name__ == "__main__":
    unittest.main()
