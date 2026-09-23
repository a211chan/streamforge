"""送出先（Target）の保存と、判定の学習。

保存の要点:
  * 原文URLは暗号化して持つ。ストリームキーや passphrase が丸ごと入っているため
  * 表示・ログ・履歴には伏せ字版だけを出す
  * 同じURLは fingerprint で1件にまとめ、使用回数と最終使用日時だけ増やす
  * 手動でプロトコルを上書きしたら記録し、次に同じURLを貼ったときはそれを優先する
    （機能設計書 F-1「誤判定への備え」）
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .protocol import Resolution, resolve
from .secrets import SecretBox, mask_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


class TargetStore:
    def __init__(self, conn: sqlite3.Connection, box: SecretBox) -> None:
        self.conn = conn
        self.box = box

    # ------------------------------------------------------------- 判定

    def resolve_with_learning(self, url: str) -> tuple[Resolution, dict | None]:
        """自動判定に、過去の手動上書きを重ねて返す。"""
        res = resolve(url)
        known = self.find_by_url(url)

        if known and known["detected_by"] == "manual" and known["protocol"] != res.protocol:
            res.reason = (
                f"以前このURLを手動で {known['protocol'].upper()} に変更しているため、"
                f"その判定を使います（自動判定は {res.protocol or '判定不能'}）"
            )
            res.protocol = known["protocol"]
            res.supported = known["protocol"] in ("rtmp", "srt")

        return res, known

    def display_url_for(self, url: str, res: Resolution) -> str:
        """保存せずに伏せ字版URLだけ得る（プレビュー・疎通確認用）。"""
        return mask_url(url, res.secrets.values()) if res.secrets else url

    # --------------------------------------------------------- 読み書き

    def find_by_url(self, url: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM targets WHERE fingerprint=?", (fingerprint(url),)
        ).fetchone()
        return self._row(row)

    def get(self, target_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        return self._row(row)

    def get_url(self, target_id: int) -> str | None:
        """送出に使う原文URL。ここだけが平文に戻す入口。"""
        row = self.conn.execute("SELECT url_enc FROM targets WHERE id=?", (target_id,)).fetchone()
        return self.box.decrypt(row["url_enc"]) if row else None

    def get_token(self, target_id: int) -> str:
        """WHIP の Bearer トークン。無ければ空文字。"""
        row = self.conn.execute(
            "SELECT secret_enc FROM targets WHERE id=?", (target_id,)
        ).fetchone()
        if not row or not row["secret_enc"]:
            return ""
        return self.box.decrypt(row["secret_enc"])

    def list(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM targets ORDER BY COALESCE(last_used_at, created_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def save(
        self,
        url: str,
        res: Resolution,
        *,
        label: str = "",
        detected_by: str = "auto",
        touch: bool = False,
        bearer_token: str | None = None,
    ) -> dict:
        """URLを保存（既存なら更新）して、保存後の行を返す。"""
        url = url.strip()
        fp = fingerprint(url)
        display = mask_url(url, res.secrets.values())
        params = json.dumps([p.as_dict() for p in res.params], ensure_ascii=False)
        token_enc = self.box.encrypt(bearer_token) if bearer_token else None
        existing = self.conn.execute(
            "SELECT id, detected_by, label FROM targets WHERE fingerprint=?", (fp,)
        ).fetchone()

        if existing:
            # 手動上書きの記録は、後から auto で上書きしない
            new_detected = detected_by if detected_by == "manual" else existing["detected_by"]
            self.conn.execute(
                """UPDATE targets
                   SET label=?, protocol=?, detected_by=?, display_url=?, params_json=?,
                       secret_enc=COALESCE(?, secret_enc),
                       last_used_at=CASE WHEN ? THEN ? ELSE last_used_at END,
                       use_count=use_count + CASE WHEN ? THEN 1 ELSE 0 END
                   WHERE id=?""",
                (
                    label or existing["label"],
                    res.protocol,
                    new_detected,
                    display,
                    params,
                    token_enc,
                    touch, _now(),
                    touch,
                    existing["id"],
                ),
            )
            self.conn.commit()
            return self.get(existing["id"])

        cur = self.conn.execute(
            """INSERT INTO targets
               (label, protocol, detected_by, url_enc, display_url, fingerprint,
                params_json, created_at, last_used_at, use_count, secret_enc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                label,
                res.protocol,
                detected_by,
                self.box.encrypt(url),
                display,
                fp,
                params,
                _now(),
                _now() if touch else None,
                1 if touch else 0,
                token_enc,
            ),
        )
        self.conn.commit()
        return self.get(int(cur.lastrowid))

    def delete(self, target_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 内部

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        from .db import row_to_dict

        return row_to_dict(row)
