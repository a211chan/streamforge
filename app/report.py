"""テスト結果をJSONにまとめる。

送出が終わったあとに「どう流れていたか」を振り返るための出力。
画面のログは間引かれるし、指摘は発生時に1回しか流れないので、
**記録を1ファイルにまとめて外に出せる**ことに意味がある。

秘匿値は入れない。DB に入っている時点で伏せ字済みのものだけを使う。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

REPORT_VERSION = 1
DEFAULT_LOG_LINES = 2000


def build(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    log_lines: int | None = DEFAULT_LOG_LINES,
    ffmpeg_version: str = "",
) -> dict[str, Any] | None:
    from .db import row_to_dict

    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    job = row_to_dict(row)
    if job is None:
        return None

    samples = [
        dict(r) for r in conn.execute(
            "SELECT at, rate, video_rate, av_skew_sec, fps, bitrate_kbps, "
            "drop_frames, dup_frames, out_time_sec "
            "FROM job_samples WHERE job_id=? ORDER BY at", (job_id,)
        )
    ]
    events = [
        dict(r) for r in conn.execute(
            "SELECT at, kind, key, severity, message FROM job_events "
            "WHERE job_id=? ORDER BY at", (job_id,)
        )
    ]

    return {
        "report_version": REPORT_VERSION,
        "tool": {"name": "StreamForge", "ffmpeg": ffmpeg_version},
        "job": {
            "id": job["id"],
            "label": job["target_label"],
            "protocol": job["protocol"],
            "target": job["target_url"],          # 伏せ字済み
            "status": job["status"],
            "exit_code": job["exit_code"],
            "started_at": job["started_at"],
            "ended_at": job["ended_at"],
            "duration_sec": job["duration_sec"],
            "restart_count": job["restart_count"],
            "last_error": job["last_error"],
            "auto_restart": job["auto_restart"],
        },
        "settings": {
            "encode": job["encode"],
            "source": job["source"],
            "overlay": job["overlay"],
            "command": job["resolved_command"],   # 伏せ字済み
        },
        "summary": summarize(samples, events, job),
        "samples": samples,
        "events": events,
        "log": _log_tail(job.get("log_path"), log_lines),
    }


def summarize(samples: list[dict], events: list[dict], job: dict) -> dict[str, Any]:
    """後から見て判断できる数字だけを抜き出す。

    平均だけだと一瞬の落ち込みが埋もれるので、最小値と
    「実時間を割っていた割合」も併記する。
    """
    # 映像基準のレートがあるならそちらを使う。out_time 基準は音声が流れてさえ
    # いれば 1.00x に見えるので、映像が止まった事実が埋もれる
    video_rates = [s["video_rate"] for s in samples if s.get("video_rate") is not None]
    rates = video_rates or [s["rate"] for s in samples if s.get("rate") is not None]
    skews = [s["av_skew_sec"] for s in samples if s.get("av_skew_sec") is not None]
    fps = [s["fps"] for s in samples if s.get("fps")]
    drops = [s["drop_frames"] for s in samples if s.get("drop_frames") is not None]

    summary: dict[str, Any] = {
        "sample_count": len(samples),
        "measured_sec": round(samples[-1]["at"], 1) if samples else 0,
        "diagnoses": sorted({e["key"] for e in events if e["kind"] == "diagnosis"}),
        "restart_count": job.get("restart_count", 0),
        "dropped_frames_total": max(drops) if drops else 0,
    }
    if rates:
        below = [r for r in rates if r < 0.95]
        stopped = [r for r in rates if r <= 0.01]
        summary["rate"] = {
            "basis": "video" if video_rates else "out_time",
            "min": round(min(rates), 3),
            "avg": round(sum(rates) / len(rates), 3),
            "max": round(max(rates), 3),
            "below_0_95_ratio": round(len(below) / len(rates), 3),
            # 映像が1枚も進まなかったサンプル。ここが 0 でないなら送出が途切れている
            "stopped_samples": len(stopped) if video_rates else None,
        }
    if skews:
        # 参考値。out_time（音声エンコーダの到達点）と映像フレームの差であって、
        # **送出された中身のずれではない**。出力側の -max_interleave_delta 0 が
        # 映像を待たせるので、実際に出ていく順序のずれは 0.02 秒に収まっている。
        # 重い条件では平常時でも 100 秒を超えるため、判定には使わない
        summary["encoder_lead_sec"] = {
            "max": round(max(skews), 1),
            "min": round(min(skews), 1),
        }
    if fps:
        summary["fps"] = {"min": round(min(fps), 1), "avg": round(sum(fps) / len(fps), 1)}
    summary["verdict"] = _verdict(summary)
    return summary


def _verdict(summary: dict) -> str:
    """一言で結論を書く。数字だけ渡されても判断に時間がかかるため。"""
    rate = summary.get("rate")
    if not rate:
        return "計測データがありません"
    if summary["restart_count"]:
        return f"再接続が {summary['restart_count']} 回発生しました。安定していません"
    # 映像が止まった事実を最優先で出す。これを「遅い」に混ぜると埋もれる
    if rate.get("stopped_samples"):
        return (
            f"映像が出ていない時間がありました（{rate['stopped_samples']} サンプルで"
            "フレームが1枚も進んでいません）。その間は音声だけが送出されています"
        )
    if rate["below_0_95_ratio"] > 0.1:
        return (
            f"実時間に追いつけていない時間が {rate['below_0_95_ratio'] * 100:.0f}% ありました"
            f"（最低 {rate['min']:.2f}x）"
        )
    if summary["dropped_frames_total"]:
        return f"フレームを {summary['dropped_frames_total']} 枚落としました"
    return f"安定して送出できました（平均 {rate['avg']:.2f}x）"


def _log_tail(path: str | None, max_lines: int | None) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {"lines": [], "truncated": False}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    if max_lines == 0:
        # 0 を素直に `lines[-0:]` と書くと全件が返る。明示的に落とす
        return {"lines": [], "truncated": bool(lines), "total_lines": len(lines)}
    if max_lines is None or len(lines) <= max_lines:
        return {"lines": lines, "truncated": False}
    return {"lines": lines[-max_lines:], "truncated": True, "total_lines": len(lines)}


def filename(job: dict) -> str:
    stamp = (job.get("started_at") or "").replace(":", "").replace("-", "")[:15]
    return f"streamforge-job{job['id']}-{stamp or 'report'}.json"


def dumps(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
