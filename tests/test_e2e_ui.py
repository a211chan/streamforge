"""画面の E2E テスト（Playwright）。

④「記録と資産」の各タブにある「使う」系ボタンを押したとき、
④ が閉じて ①②③ の組み立てに戻ることを、実際のブラウザで確かめる。
（backToComposer が自分自身を呼んで無限再帰していた不具合の再発防止）

Playwright は開発用の依存なので requirements.txt には入れていない。
入っていない環境では、このファイルのテストはスキップされる。

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m playwright install chromium
    .venv/bin/python -m unittest tests.test_e2e_ui -v

サーバーは一時ディレクトリをデータ置き場にして空きポートで別プロセス起動する。
config.toml や ~/StreamForge の実データには触れない。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # 開発用依存が無ければ丸ごとスキップ
    sync_playwright = None

REPO_ROOT = Path(__file__).resolve().parent.parent
FFMPEG = shutil.which("ffmpeg")

# app.main の cfg を差し替えてから起動する。lifespan は起動時に cfg を読むので、
# ここで書き換えれば DB・メディア・ログはすべて一時ディレクトリに向く
SERVER_SCRIPT = """
import sys
from pathlib import Path
import uvicorn
import app.main as m
root = Path(sys.argv[1])
m.cfg.data_dir = root
m.cfg.media_dir = root / "media"
m.cfg.log_dir = root / "logs"
uvicorn.run(m.app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "playwright が入っていない（requirements-dev.txt）")
class TestBackToComposer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="streamforge-e2e-")
        root = Path(cls.tmp.name)
        (root / "media").mkdir()
        if FFMPEG:
            # メディアタブに1件出すための短い動画
            subprocess.run(
                [FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc=size=160x120:rate=10", "-t", "1",
                 str(root / "media" / "sample.mp4")],
                check=True,
            )

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server = subprocess.Popen(
            [sys.executable, "-c", SERVER_SCRIPT, str(root), str(cls.port)],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            cls._wait_for_server()
            cls._seed()
            cls.pw = sync_playwright().start()
            try:
                cls.browser = cls.pw.chromium.launch()
            except PlaywrightError as exc:
                cls.pw.stop()
                raise unittest.SkipTest(f"Chromium を起動できない（playwright install chromium）: {exc}")
        except BaseException:
            cls._stop_server()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls._stop_server()

    @classmethod
    def _stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
        cls.server.stderr.close()
        cls.tmp.cleanup()

    @classmethod
    def _wait_for_server(cls):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError(f"サーバーが起動しなかった:\n{cls.server.stderr.read().decode()}")
            try:
                urllib.request.urlopen(cls.base + "/", timeout=1).close()
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        raise RuntimeError("サーバーが 20 秒以内に応答しなかった")

    @classmethod
    def _api(cls, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            cls.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.load(res)

    @classmethod
    def _seed(cls):
        # 受け口のない空きポートを送出先にする。保存はされ、送出は即失敗して履歴に残る
        url = f"rtmp://127.0.0.1:{_free_port()}/live/e2e"
        cls._api("POST", "/api/targets", {"url": url, "label": "E2E"})
        if not FFMPEG:
            return
        cls._api("POST", "/api/jobs", {"target_url": url})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls._api("GET", "/api/jobs")["running_job_id"] is None:
                return
            time.sleep(0.2)
        raise RuntimeError("検証用ジョブが終わらなかった")

    def _press(self, tab: str, label: str):
        """組み立てを畳んだ状態で ④ のタブを開き、最初の該当ボタンを押す。"""
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.addCleanup(page.close)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(self.base + "/")
        page.wait_for_function("typeof foldComposer === 'function'")
        page.evaluate("foldComposer(true)")
        expect(page.locator("#composer-body")).to_be_hidden()

        page.click("#vault-toggle")
        page.click(f'.tab[data-tab="{tab}"]')
        button = page.locator(f'[data-panel="{tab}"] button', has_text=label).first
        expect(button).to_be_visible()
        button.click()

        expect(page.locator("#composer-body")).to_be_visible()
        expect(page.locator("#composer-folded")).to_be_hidden()
        expect(page.locator("#vault-body")).to_be_hidden()
        expect(page.locator("#composer")).to_be_in_viewport()
        self.assertEqual(errors, [])
        return page

    def test_templates_use(self):
        self._press("templates", "使う")

    def test_targets_use(self):
        page = self._press("targets", "使う")
        expect(page.locator("#url")).to_be_disabled()

    @unittest.skipIf(FFMPEG is None, "ffmpeg が無いと検証用の動画を作れない")
    def test_media_use(self):
        page = self._press("media", "使う")
        expect(page.locator("#src-type")).to_have_value("file")

    @unittest.skipIf(FFMPEG is None, "ffmpeg が無いと履歴を作れない")
    def test_history_reuse(self):
        page = self._press("history", "この条件で")
        expect(page.locator("#url")).to_be_disabled()


if __name__ == "__main__":
    unittest.main()
