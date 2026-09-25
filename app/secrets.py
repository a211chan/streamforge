"""秘匿値の暗号化と伏せ字。

配信先URLにはストリームキーや SRT の passphrase が丸ごと含まれる。これを
平文でDBに置くと、DBのコピー・バックアップ・画面共有から漏れる。

守れる範囲は正直に書いておく:
  * 鍵ファイルは data_dir に 0600 で置く。DB だけが持ち出された場合には有効
  * 同じマシンの同じユーザーから読まれることは防げない
  * したがってこれは「事故の被害を減らす」ための措置であって、
    多人数運用時のアクセス制御の代わりにはならない
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_FILENAME = "secret.key"


class SecretBox:
    def __init__(self, data_dir: Path) -> None:
        self.key_path = data_dir / KEY_FILENAME
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()

        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        # 先に 0600 で作ってから書く。作成と権限設定の間に隙を作らない
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("保存された値を復号できません（鍵が変わった可能性があります）") from exc


MASK = "●●●●●●"

# これより短い値は単体で置換しない。
# 短い文字列（"x" のようなテスト用キー）を無差別に置換すると、コマンドやログの
# 無関係な箇所まで壊れる（実際に drawtext が "drawte●●●t" になる事故を踏んだ）。
# 短い秘匿値は URL ごとの置換（redact の url→display_url）でカバーする。
MIN_MASKABLE_LEN = 8


def mask_values(text: str, values) -> str:
    """文字列中の秘匿値を伏せる。

    長い値から先に置換する。短い値が長い値の一部だった場合に、
    置換後の文字列に断片が残るのを防ぐため。
    """
    if not text:
        return text
    targets = [v for v in values if v and len(v) >= MIN_MASKABLE_LEN]
    for value in sorted(targets, key=len, reverse=True):
        text = text.replace(value, MASK)
    return text


def mask_url(url: str, values) -> str:
    """URL の中の秘匿値を伏せる。長さの下限は設けない。

    置換対象が URL という限られた文脈なので、1文字の値でも巻き添えが出ない。
    自由文（コマンド・ログ）への置換とは要件が違うので関数を分けている。
    """
    if not url:
        return url
    for value in sorted((v for v in values if v), key=len, reverse=True):
        url = url.replace(value, MASK)
    return url


def redact(text: str, *, url: str = "", display_url: str = "", values=()) -> str:
    """コマンド文字列やログ行から秘匿値を取り除く。

    まず URL 全体を伏せ字版に置き換える。これは完全一致なので巻き添えが出ない。
    そのうえで、URL 以外の場所に現れうる十分な長さの秘匿値を個別に潰す。
    """
    if url and display_url and url != display_url:
        text = text.replace(url, display_url)
    return mask_values(text, values)
