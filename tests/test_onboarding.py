"""初回ツアーが指す先が、画面に実在するかを見る。

ツアーは index.html の id を名指しで指す。画面の組み替えで id が消えたり
変わったりしても JS は黙って「指す先なし」に倒れるので、目で見ないと
気づけない。ここで機械的に突き合わせる。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


class OnboardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.js = (STATIC / "onboarding.js").read_text(encoding="utf-8")
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")

    def test_targets_exist_in_index_html(self) -> None:
        targets = re.findall(r"target:\s*'([^']+)'", self.js)
        self.assertTrue(targets, "ツアーの指す先が1つも見つかりません")
        ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        for t in targets:
            with self.subTest(target=t):
                self.assertRegex(t, r"^#[\w-]+$", "指す先は id だけにする（class は見た目の調整で外れる）")
                self.assertIn(t[1:], ids, f"{t} が index.html にありません")

    def test_loaded_before_app_js(self) -> None:
        # app.js の init() が window.SFOnboarding を呼ぶので、先に読まれている必要がある
        a = self.html.find('src="/static/onboarding.js"')
        b = self.html.find('src="/static/app.js"')
        self.assertNotEqual(a, -1, "onboarding.js が読み込まれていません")
        self.assertLess(a, b, "onboarding.js は app.js より前に読み込む")

    def test_help_button_exists(self) -> None:
        self.assertIn('id="help"', self.html, "ツアーを見直す「使い方」ボタンがありません")


if __name__ == "__main__":
    unittest.main()
