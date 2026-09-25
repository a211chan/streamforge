"""SQLite の初期化・マイグレーションとアクセス。

P0: jobs
P1: targets（送出先。URLは暗号化して保存する）
P2: templates（エンコード条件。送出開始時に自動保存される）
P6: targets.secret_enc（WHIP の Bearer トークン。URLとは別に暗号化して持つ）
v5: job_samples / job_events（テスト後に振り返るための記録）

Source は P4 で独立テーブルに切り出す。それまでは job に JSON で持たせる
（先にテーブルだけ作っても使い道がない）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol          TEXT    NOT NULL,
    target_url        TEXT    NOT NULL,
    target_label      TEXT    NOT NULL DEFAULT '',
    source_json       TEXT    NOT NULL,
    encode_json       TEXT    NOT NULL,
    overlay_json      TEXT    NOT NULL DEFAULT '{}',
    duration_sec      INTEGER,
    auto_restart      INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL,
    pid               INTEGER,
    pgid              INTEGER,
    started_at        TEXT    NOT NULL,
    ended_at          TEXT,
    resolved_command  TEXT    NOT NULL,
    exit_code         INTEGER,
    restart_count     INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    log_path          TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at DESC);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT    NOT NULL DEFAULT '',
    protocol        TEXT    NOT NULL,
    detected_by     TEXT    NOT NULL DEFAULT 'auto',   -- auto | manual
    url_enc         TEXT    NOT NULL,                  -- 暗号化した原文URL
    display_url     TEXT    NOT NULL,                  -- 秘匿値を伏せた表示用
    fingerprint     TEXT    NOT NULL UNIQUE,           -- 原文URLのハッシュ（重複排除・学習キー）
    params_json     TEXT    NOT NULL DEFAULT '[]',     -- 分解結果（伏せ字済み）
    created_at      TEXT    NOT NULL,
    last_used_at    TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_targets_used ON targets(last_used_at DESC);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    is_pinned       INTEGER NOT NULL DEFAULT 0,
    is_auto_named   INTEGER NOT NULL DEFAULT 1,
    settings_hash   TEXT    NOT NULL UNIQUE,   -- 内容の正規化ハッシュ（重複排除キー）
    settings_json   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    last_used_at    TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_templates_used ON templates(is_pinned DESC, last_used_at DESC);
"""

SCHEMA_V5 = """
-- 稼働中の指標を間引いて残す。後から「あのとき何が起きたか」を追うため
CREATE TABLE IF NOT EXISTS job_samples (
    job_id        INTEGER NOT NULL,
    at            REAL    NOT NULL,   -- ジョブ開始からの経過秒
    rate          REAL,               -- 実時間比（平滑化後）
    fps           REAL,
    bitrate_kbps  REAL,
    drop_frames   INTEGER,
    dup_frames    INTEGER,
    out_time_sec  REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_job ON job_samples(job_id, at);

-- 指摘の発生と解消。発生時に1回しか流れないので、残さないと後から追えない
CREATE TABLE IF NOT EXISTS job_events (
    job_id     INTEGER NOT NULL,
    at         REAL    NOT NULL,
    kind       TEXT    NOT NULL,   -- diagnosis | resolved | status
    key        TEXT    NOT NULL DEFAULT '',
    severity   TEXT    NOT NULL DEFAULT '',
    message    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, at);
"""

# targets に後から足す列
TARGETS_EXTRA_COLUMNS = {
    # WHIP の Authorization。URL の一部ではないので別カラムで暗号化して持つ
    "secret_enc": "TEXT",
}

# jobs に後から足す列（既存DBにも同じものを ALTER で足す）
JOBS_EXTRA_COLUMNS = {
    "target_id": "INTEGER",
    "template_id": "INTEGER",
}

# job_samples に後から足す列。
# rate（out_time 基準）だけでは、映像が止まって音声だけ流れている状態が
# 1.00x に見えてしまい、記録からも異常を追えなかった
SAMPLES_EXTRA_COLUMNS = {
    "video_rate": "REAL",     # 映像フレームの進みで見た実時間比。判定はこちらを使う
    # 映像が音声エンコーダよりどれだけ後ろにいるかの参考値。
    # 送出される中身のずれではない（-max_interleave_delta 0 が映像を待たせる）
    "av_skew_sec": "REAL",
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA_V1)
    conn.executescript(SCHEMA_V2)
    conn.executescript(SCHEMA_V3)
    conn.executescript(SCHEMA_V5)
    _add_missing_columns(conn, "jobs", JOBS_EXTRA_COLUMNS)
    _add_missing_columns(conn, "targets", TARGETS_EXTRA_COLUMNS)
    _add_missing_columns(conn, "job_samples", SAMPLES_EXTRA_COLUMNS)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """JSON カラムを展開して素の dict に変換する。"""
    if row is None:
        return None
    d = dict(row)
    for key, out in (
        ("source_json", "source"),
        ("encode_json", "encode"),
        ("overlay_json", "overlay"),
    ):
        if key in d:
            raw = d.pop(key)
            d[out] = json.loads(raw) if raw else {}
    if "params_json" in d:
        raw = d.pop("params_json")
        d["params"] = json.loads(raw) if raw else []
    if "auto_restart" in d:
        d["auto_restart"] = bool(d["auto_restart"])
    # 暗号文は API に出さない。トークンの有無だけ伝える
    d.pop("url_enc", None)
    if "secret_enc" in d:
        d["has_token"] = bool(d.pop("secret_enc"))
    return d
