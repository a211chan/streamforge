"""エンコード条件のテンプレート（機能設計書 F-3）。

このモジュールの目的はひとつ。**名前を付けて保存する作業をユーザーから取り除く**こと。

  * 保存は「送出開始を押した瞬間」。設定を触っただけの中間状態は残さない
  * 同じ内容は内容ハッシュで1件にまとめ、使用回数だけ増やす
  * 名前は自動生成。付けたければ後から付けられる
  * 増えたぶんは自動で片付ける（LRU）。自動保存の副作用は自動で回収する
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .models import EncodeSettings

# 非ピン・非ビルトイン・自動命名のままのテンプレートを、この件数まで残す
LRU_KEEP = 30

# 出荷時テンプレート。これだけで大半のテストが済む状態にしておく
BUILTINS: list[tuple[str, EncodeSettings]] = [
    ("Standard 1080p30", EncodeSettings()),
    (
        "Low 720p30",
        EncodeSettings(width=1280, height=720, fps=30, bitrate_kbps=2500),
    ),
    (
        "LowLatency 720p30",
        EncodeSettings(
            width=1280, height=720, fps=30, bitrate_kbps=2500,
            gop_sec=1.0, preset="ultrafast", tune="zerolatency", bframes=0,
        ),
    ),
    (
        "High 1080p60",
        EncodeSettings(width=1920, height=1080, fps=60, bitrate_kbps=6000),
    ),
    (
        "WebRTC互換 720p30",
        EncodeSettings(
            width=1280, height=720, fps=30, bitrate_kbps=2500,
            gop_sec=1.0, preset="veryfast", bframes=0, profile="baseline",
        ),
    ),
    ("Passthrough", EncodeSettings(mode="passthrough")),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TemplateStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------ 初期化

    def seed_builtins(self) -> None:
        """出荷時テンプレートを用意する。既にあるものの使用回数は壊さない。"""
        for name, enc in BUILTINS:
            h = enc.settings_hash()
            row = self.conn.execute(
                "SELECT id FROM templates WHERE settings_hash=?", (h,)
            ).fetchone()
            if row:
                # 利用者が同じ設定を先に使っていた場合、その行をビルトインに昇格させる
                self.conn.execute(
                    "UPDATE templates SET name=?, is_builtin=1, is_auto_named=0 WHERE id=?",
                    (name, row["id"]),
                )
                continue
            self.conn.execute(
                """INSERT INTO templates
                   (name, is_builtin, is_pinned, is_auto_named, settings_hash,
                    settings_json, created_at, use_count)
                   VALUES (?,1,0,0,?,?,?,0)""",
                (name, h, enc.model_dump_json(), _now()),
            )
        self.conn.commit()

    # ------------------------------------------------------------ 読み取り

    def list(self) -> list[dict]:
        """ピン留め → 最近使った順。使っていないものは最後に回す。"""
        rows = self.conn.execute(
            """SELECT * FROM templates
               ORDER BY is_pinned DESC,
                        (last_used_at IS NULL),
                        last_used_at DESC,
                        id DESC,          -- 同一秒での並び順を固定する
                        is_builtin DESC"""
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, template_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        return self._row(row)

    def settings_of(self, template_id: int) -> EncodeSettings | None:
        row = self.conn.execute(
            "SELECT settings_json FROM templates WHERE id=?", (template_id,)
        ).fetchone()
        return EncodeSettings.model_validate_json(row["settings_json"]) if row else None

    def last_used(self) -> dict | None:
        row = self.conn.execute(
            # last_used_at は秒精度なので、続けて送出すると同着になる。
            # id を第2キーに置いて「本当に最後に使ったもの」を確定させる
            "SELECT * FROM templates WHERE last_used_at IS NOT NULL "
            "ORDER BY last_used_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return self._row(row)

    # ------------------------------------------------------------ 自動保存

    def record_use(self, enc: EncodeSettings) -> dict:
        """送出開始時に呼ぶ。既存なら使用実績だけ更新し、無ければ自動保存する。"""
        h = enc.settings_hash()
        row = self.conn.execute("SELECT id FROM templates WHERE settings_hash=?", (h,)).fetchone()

        if row:
            self.conn.execute(
                "UPDATE templates SET last_used_at=?, use_count=use_count+1 WHERE id=?",
                (_now(), row["id"]),
            )
            self.conn.commit()
            return self.get(row["id"])

        cur = self.conn.execute(
            """INSERT INTO templates
               (name, is_builtin, is_pinned, is_auto_named, settings_hash,
                settings_json, created_at, last_used_at, use_count)
               VALUES (?,0,0,1,?,?,?,?,1)""",
            (enc.auto_name(), h, enc.model_dump_json(), _now(), _now()),
        )
        self.conn.commit()
        created = self.get(int(cur.lastrowid))
        self.prune()
        return created

    def prune(self) -> int:
        """自動命名のまま放置されたものを古い順に片付ける。

        ビルトイン・ピン留め・リネーム済み（＝利用者が意思を示したもの）は残す。
        """
        rows = self.conn.execute(
            """SELECT id FROM templates
               WHERE is_builtin=0 AND is_pinned=0 AND is_auto_named=1
               ORDER BY (last_used_at IS NULL), last_used_at DESC, id DESC"""
        ).fetchall()
        doomed = [r["id"] for r in rows[LRU_KEEP:]]
        if doomed:
            self.conn.executemany(
                "DELETE FROM templates WHERE id=?", [(i,) for i in doomed]
            )
            self.conn.commit()
        return len(doomed)

    # ------------------------------------------------------------ 更新・削除

    def patch(self, template_id: int, *, name: str | None, is_pinned: bool | None) -> dict | None:
        row = self.conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            return None

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("名前を空にはできません")
            # 名付けた時点で「利用者が意思を示した」とみなし、LRU整理の対象から外す
            self.conn.execute(
                "UPDATE templates SET name=?, is_auto_named=0 WHERE id=?", (name, template_id)
            )
        if is_pinned is not None:
            self.conn.execute(
                "UPDATE templates SET is_pinned=? WHERE id=?", (int(is_pinned), template_id)
            )
        self.conn.commit()
        return self.get(template_id)

    def delete(self, template_id: int) -> tuple[bool, str]:
        row = self.conn.execute(
            "SELECT is_builtin FROM templates WHERE id=?", (template_id,)
        ).fetchone()
        if row is None:
            return False, "テンプレートが見つかりません"
        if row["is_builtin"]:
            return False, "ビルトインテンプレートは削除できません"
        self.conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
        self.conn.commit()
        return True, ""

    # ------------------------------------------------------------------ 内部

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["settings"] = json.loads(d.pop("settings_json"))
        d["is_builtin"] = bool(d["is_builtin"])
        d["is_pinned"] = bool(d["is_pinned"])
        d["is_auto_named"] = bool(d["is_auto_named"])
        d["summary"] = EncodeSettings.model_validate(d["settings"]).summary()
        return d
