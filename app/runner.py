"""Job Runner — FFmpeg プロセスの起動・監視・停止。

設計上の要点:
  * 同時に running なジョブは1件だけ（憲章の単一出力方針）
  * プロセスグループを作って起動し、停止はグループごと送る。
    FFmpeg の子や孫が残ると「止めたのに送出が続く」状態になる
  * API 再起動時に DB の running を必ず整合させる。
    幽霊ジョブを残さない（憲章 Principle 4）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command import SnapshotSpec, build_command, to_shell
from .config import Config
from .errors import ProgressHealth, diagnose_line
from .models import EncodeSettings, JobCreate, OverlaySettings, SourceSpec
from .secrets import redact

LOG_BUFFER_LINES = 500
TERM_GRACE_SEC = 5.0

# 壊れた素材は同じ警告を毎秒数十行吐く。全部を SSE で流すと受け手が死ぬ
# （実際にブラウザがフリーズした）。ffmpeg 自身の "Last message repeated N times"
# と同じ考え方で、同種の行はまとめる。
REPEAT_SUMMARY_SEC = 2.0      # まとめを出す間隔
MAX_EMIT_PER_SEC = 25         # 種類が違っても、この本数を超えたら間引く

# 順調なときは FFmpeg が何も言わないので、画面が無言になる。
# 「動いていること」を定期的に自分から報告する
# 「今の」レートを均す窓（progress は毎秒来るので、およそこの秒数）
RATE_WINDOW_SAMPLES = 6

# 指標をDBに残す間隔。毎秒残すと2時間で7200行になるので間引く
SAMPLE_STORE_SEC = 5.0

# 映像がどれだけ遅れたら定期ログに書き添えるか。
# 継ぎ目や一瞬の詰まりで1秒前後は動くので、そこは拾わない
VIDEO_LAG_LOG_SEC = 5.0

HEARTBEAT_SEC = 60.0
# 最初の1回だけは早めに出す。短いジョブで一度も出ないと、
# 「動いているのか」が分からないまま終わってしまう
FIRST_HEARTBEAT_SEC = 15.0

# 自動再接続のバックオフ（秒）。最後の値以降はそれを繰り返す
BACKOFF_SEC = (2, 4, 8, 16, 30, 60)
# この回数を1時間で超えたら、再接続をあきらめて failed にする
MAX_RESTARTS_PER_HOUR = 10

_HEX = re.compile(r"0x[0-9a-f]+")
_NUM = re.compile(r"\d+")


def log_shape(line: str) -> str:
    """行を「同じ種類か」だけで比べられる形にする。

    アドレスと数値を潰すので、`pts=1234` と `pts=5678` は同じ種類になる。
    """
    return _NUM.sub("#", _HEX.sub("@", line))


class LogThrottle:
    """同種の行をまとめ、全体の流量に上限をかける。

    ログファイルには**全行そのまま**書く。間引くのは画面へ流す分だけで、
    後から追える情報は失わない。

    「直前の行と同じか」だけで判定すると、種類の違う警告が交互に来たときに
    素通りしてしまう（実際、壊れた素材で mpegts と hls の警告が交互に出て
    ほとんど間引けなかった）。**種類ごとに**直近の表示時刻を覚えて、
    一定時間は同じ種類を出さないようにする。
    """

    MAX_TRACKED_SHAPES = 200   # 種類が無限に増えても記憶が膨らまないように

    def __init__(self) -> None:
        self._last_emit: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._window_start = 0.0
        self._in_window = 0

    def feed(self, line: str, now: float) -> list[str]:
        """画面に流すべき行を返す（0〜2行）。"""
        shape = log_shape(line)
        last = self._last_emit.get(shape)

        if last is not None and now - last < REPEAT_SUMMARY_SEC:
            self._suppressed[shape] = self._suppressed.get(shape, 0) + 1
            return []

        # 流量そのものの上限。種類がばらけていても守る
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._in_window = 0
        self._in_window += 1
        if self._in_window == MAX_EMIT_PER_SEC:
            return ["[streamforge] ログが多いため画面表示を間引いています"
                    "（全文はログファイルにあります）"]
        if self._in_window > MAX_EMIT_PER_SEC:
            return []

        self._forget_old(now)
        self._last_emit[shape] = now
        skipped = self._suppressed.pop(shape, 0)

        out = [line]
        if skipped:
            out.append(f"[streamforge] 直前の同種の行を {skipped} 回省略しました")
        return out

    def _forget_old(self, now: float) -> None:
        if len(self._last_emit) <= self.MAX_TRACKED_SHAPES:
            return
        stale = [k for k, t in self._last_emit.items() if now - t > REPEAT_SUMMARY_SEC * 5]
        for k in stale:
            self._last_emit.pop(k, None)
            self._suppressed.pop(k, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobBusy(Exception):
    """すでに送出中。"""


class JobRunner:
    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self.conn = conn
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._job_id: int | None = None
        self._tasks: set[asyncio.Task] = set()
        self._subscribers: set[asyncio.Queue] = set()
        self._log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_LINES)
        self._last_progress: dict[str, Any] = {}
        # FFmpeg の speed は「起動からの累積平均」なので、今の状態を表さない。
        # 直近数秒の窓で実測して「今の」レートを出す
        self._rate_window: deque[tuple[float, float]] = deque(maxlen=RATE_WINDOW_SAMPLES)
        # out_time は「進んでいる方のストリーム」を指すので、映像が止まっていても
        # 音声が流れていれば 1.00x に見える。映像だけの進みを別に持つ
        self._video_window: deque[tuple[float, int]] = deque(maxlen=RATE_WINDOW_SAMPLES)
        self._target_fps = 0.0
        self._job_started_at = 0.0
        self._last_sample_store = 0.0
        self._log_file: Any = None
        self._stopping = False
        self._ctx: dict[str, Any] = {}
        self._diagnoses: dict[str, str] = {}   # 同じ指摘を何度も出さないための記録
        self._health = ProgressHealth()
        self._throttle = LogThrottle()
        self._last_heartbeat = None
        self._heartbeats = 0
        self._rate_window.clear()
        self._video_window.clear()
        self._job_started_at = time.monotonic()
        self._last_sample_store = 0.0
        # 「プロセスは死んでいるが再接続待ち」の間も稼働中として扱う。
        # ここを落とすと、待っている間に停止できず running のまま残る
        self._active = False
        self._finalize_task: asyncio.Task | None = None
        # start() はプロセス生成で await するため、チェックと状態設定のあいだに
        # 別のリクエストが割り込める。ロックで直列化したうえで、フラグ自体も
        # await より前に立てて隙間をなくす
        self._start_lock = asyncio.Lock()
        self._secrets: list[str] = []   # ログ・コマンド表示から伏せる値
        self._redact_url = ""
        self._redact_display = ""

    # ------------------------------------------------------------------ 状態

    @property
    def current_job_id(self) -> int | None:
        return self._job_id

    def is_running(self) -> bool:
        return self._active

    def is_reconnecting(self) -> bool:
        return self._active and (self._proc is None or self._proc.returncode is not None)

    # ------------------------------------------------------ 起動時の整合処理

    def reconcile_on_startup(self) -> list[int]:
        """DB 上 running のまま残っているジョブを片付ける。

        API を再起動するとパイプが切れて追跡できなくなる。プロセスが生きていても
        「動いているつもり」を残さないため、グループごと停止して failed にする。
        """
        rows = self.conn.execute(
            "SELECT id, pid, pgid FROM jobs WHERE status='running'"
        ).fetchall()
        cleaned: list[int] = []
        for row in rows:
            if pid_alive(row["pid"]):
                _kill_group(row["pgid"], row["pid"], signal.SIGTERM)
            self.conn.execute(
                "UPDATE jobs SET status='failed', ended_at=?, last_error=? WHERE id=?",
                (_now(), "APIサーバーの再起動により追跡不能になったため停止しました", row["id"]),
            )
            cleaned.append(row["id"])
        if cleaned:
            self.conn.commit()
        return cleaned

    # ------------------------------------------------------------------ 購読

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def snapshot(self) -> dict[str, Any]:
        """購読開始直後に流す現在値。画面を開いた瞬間から中身が見える。

        **いま出ている指摘も必ず含める。** 指摘は発生時に1回しか流さないので、
        後から画面を開いた人には届かない。実際、稼働中のジョブに途中から
        繋いだら警告が何も見えなかった。
        """
        return {
            "job_id": self._job_id,
            "running": self.is_running(),
            # セルフプレビューの絵が出る条件か（passthrough や無効時は出ない）
            "snapshot_enabled": self.is_running() and self._snapshot_spec() is not None,
            "progress": self._last_progress,
            "log": list(self._log_buffer),
            "diagnoses": [
                {"key": k, "message": m, "severity": "warning", "resolved": False}
                for k, m in self._diagnoses.items()
            ],
        }

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # 読み手が遅い場合は取りこぼす。ログの完全性はファイル側で担保する。
                pass

    # ------------------------------------------------------------------ 起動

    async def start(
        self,
        spec: JobCreate,
        protocol: str,
        *,
        url: str,
        secrets: list[str] | None = None,
        target_id: int | None = None,
        template_id: int | None = None,
        display_url: str = "",
        encode: "EncodeSettings | None" = None,
        overlay: "OverlaySettings | None" = None,
        font_path: str = "",
        bearer_token: str = "",
        webrtc_delivery: bool = False,
        source: "SourceSpec | None" = None,
        seam_frames: int = 0,
    ) -> int:
        """送出を開始する。

        url は送出に使う原文。DB とログに残すのは伏せ字版だけで、
        平文が出るのは FFmpeg の引数配列（プロセスの外に保存しない）のみ。
        """
        async with self._start_lock:
            return await self._start_locked(
                spec, protocol, url=url, secrets=secrets, target_id=target_id,
                template_id=template_id, display_url=display_url, encode=encode,
                overlay=overlay, font_path=font_path, bearer_token=bearer_token,
                webrtc_delivery=webrtc_delivery, source=source, seam_frames=seam_frames,
            )

    async def _start_locked(
        self,
        spec: JobCreate,
        protocol: str,
        *,
        url: str,
        secrets: list[str] | None = None,
        target_id: int | None = None,
        template_id: int | None = None,
        display_url: str = "",
        encode: "EncodeSettings | None" = None,
        overlay: "OverlaySettings | None" = None,
        font_path: str = "",
        bearer_token: str = "",
        webrtc_delivery: bool = False,
        source: "SourceSpec | None" = None,
        seam_frames: int = 0,
    ) -> int:
        if self.is_running():
            raise JobBusy(f"ジョブ #{self._job_id} が送出中です")

        # ここから先に await があるので、先に「稼働中」を立てておく。
        # 立てる前に await すると、その隙間に2本目が起動して両方が動いてしまう
        self._active = True

        secrets = [s for s in (secrets or []) if s]
        self._secrets = secrets
        encode = encode or spec.encode
        source = source or spec.source

        duration = spec.duration_sec
        if duration is None:
            duration = self.cfg.default_duration_sec

        # 映像の実時間比は「目標fpsに対してフレームが何枚進んだか」で見る。
        # passthrough はこちらでfpsを決めないので、その判定はできない
        self._target_fps = float(encode.fps) if encode.mode != "passthrough" else 0.0

        # 再接続のときに同じ条件で組み直せるよう、材料をまとめて持っておく
        self._ctx = {
            "spec": spec,
            "source": source,
            "seam_frames": seam_frames,
            "protocol": protocol,
            "url": url,
            "secrets": secrets,
            "display_url": display_url,
            "encode": encode,
            "overlay": overlay or spec.overlay,
            "font_path": font_path,
            "duration": duration,
            "bearer_token": bearer_token,
            "webrtc_delivery": webrtc_delivery,
            "auto_restart": spec.auto_restart,
            "started_monotonic": time.monotonic(),
            "restart_times": [],
        }

        self.cfg.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._clear_snapshot()

        argv = self._build_argv(duration)
        command_str = redact(to_shell(argv), url=url, display_url=display_url, values=secrets)

        cur = self.conn.execute(
            """INSERT INTO jobs
               (protocol, target_url, target_label, source_json, encode_json, overlay_json,
                duration_sec, auto_restart, status, started_at, resolved_command,
                target_id, template_id)
               VALUES (?,?,?,?,?,?,?,?,'running',?,?,?,?)""",
            (
                protocol,
                display_url,
                spec.target_label,
                source.model_dump_json(),
                encode.model_dump_json(),
                (overlay or spec.overlay).model_dump_json(),
                duration,
                int(spec.auto_restart),
                _now(),
                command_str,
                target_id,
                template_id,
            ),
        )
        self.conn.commit()
        job_id = int(cur.lastrowid)

        log_path = self.cfg.log_dir / f"job-{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,   # プロセスグループを分離する
            )
        except OSError as exc:
            self._active = False
            self.conn.execute(
                "UPDATE jobs SET status='failed', ended_at=?, last_error=? WHERE id=?",
                (_now(), f"FFmpeg を起動できませんでした: {exc}", job_id),
            )
            self.conn.commit()
            raise

        self._proc = proc
        self._job_id = job_id
        self._stopping = False
        self._redact_url = url
        self._redact_display = display_url
        self._log_buffer.clear()
        self._last_progress = {}
        self._diagnoses.clear()
        self._health = ProgressHealth()
        self._throttle = LogThrottle()
        self._last_heartbeat = None
        self._heartbeats = 0
        self._rate_window.clear()
        self._video_window.clear()
        self._job_started_at = time.monotonic()
        self._last_sample_store = 0.0
        self._last_heartbeat: float | None = None
        self._heartbeats = 0
        self._log_file = log_path.open("a", encoding="utf-8")
        self._log_file.write(f"$ {command_str}\n")
        self._log_file.flush()

        self.conn.execute(
            "UPDATE jobs SET pid=?, pgid=?, log_path=? WHERE id=?",
            (proc.pid, proc.pid, str(log_path), job_id),
        )
        self.conn.commit()

        self._spawn(self._pump_progress(proc.stdout))
        self._spawn(self._pump_logs(proc.stderr))
        self._finalize_task = self._spawn(self._wait_and_finalize(proc, job_id))

        self._emit("status", {"job_id": job_id, "status": "running", "command": command_str})
        return job_id

    def _snapshot_spec(self) -> SnapshotSpec | None:
        """セルフプレビューの指定。無効・passthrough のときは None。

        passthrough は `-c copy` でフィルタを通らないため、分岐する場所が無い。
        """
        if not self.cfg.snapshot_enabled:
            return None
        encode = self._ctx.get("encode")
        if encode is not None and encode.mode == "passthrough":
            return None
        return SnapshotSpec(
            path=str(self.cfg.snapshot_path),
            width=self.cfg.snapshot_width,
            fps=self.cfg.snapshot_fps,
        )

    def _clear_snapshot(self) -> None:
        """前のジョブの絵を残さない。

        置き場はジョブをまたいで1つなので、消さないと**止まったジョブの絵を
        新しいジョブの絵として見てしまう**。起動時と終了時の両方で消す。
        """
        with contextlib.suppress(OSError):
            self.cfg.snapshot_path.unlink()

    def _snapshot_age(self) -> float | None:
        """静止画が最後に書かれてからの秒数。まだ1枚も無ければ None。"""
        try:
            return max(0.0, time.time() - self.cfg.snapshot_path.stat().st_mtime)
        except OSError:
            return None

    def _build_argv(self, duration: int | None) -> list[str]:
        c = self._ctx
        return build_command(
            ffmpeg_path=self.cfg.ffmpeg_path,
            protocol=c["protocol"],
            target_url=c["url"],
            source=c["source"],
            seam_frames=c.get("seam_frames", 0),
            encode=c["encode"],
            duration_sec=duration,
            overlay=c["overlay"],
            font_path=c["font_path"],
            job_label=c["spec"].target_label,
            media_dir=str(self.cfg.media_dir),
            bearer_token=c.get("bearer_token", ""),
            webrtc_delivery=c.get("webrtc_delivery", False),
            snapshot=self._snapshot_spec(),
        )

    def _remaining_duration(self) -> int | None:
        """再接続後の残り時間。指定した長さの2倍流れてしまうのを防ぐ。"""
        c = self._ctx
        if not c["duration"]:
            return c["duration"]      # 0 / None は無期限
        elapsed = time.monotonic() - c["started_monotonic"]
        return max(1, int(c["duration"] - elapsed))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _log(self, line: str) -> None:
        """内部メッセージもFFmpegの出力と同じ経路に流す。

        再接続の記録はあとから見返す価値があるので、必ずログファイルにも残す。
        """
        self._log_buffer.append(line)
        if self._log_file:
            self._log_file.write(line + "\n")
            self._log_file.flush()
        self._emit("log", {"line": line})

    def _instant_rate(self, progress: dict) -> float | None:
        """直近数秒の窓で見た「今の」処理レートを返す。

        FFmpeg の `speed` は起動からの累積平均なので、序盤に落ち込むと
        そのあと実態より良く見え続ける。実際、瞬間 0.79x で遅れ続けているのに
        累積は 0.91 を示し、閾値 0.9 を超えているせいで警告も出ていなかった。

        隣り合うサンプルだけで割ると、1秒刻みの取りこぼしで 0.69〜1.20 まで
        暴れる。数秒の窓で均すと 1.00 前後に落ち着く。
        """
        now = time.monotonic()
        out = progress.get("out_time_sec")
        if out is None:
            return None

        # out_time が巻き戻ったら窓を捨てる。再接続直後や、進捗が N/A で
        # 0 と読めたときに起きる。そのまま割ると**負のレート**になり、
        # 平均を壊すうえ「0 < speed」の判定をすり抜けて警告まで止めてしまう
        if self._rate_window and out < self._rate_window[-1][1]:
            self._rate_window.clear()
        self._rate_window.append((now, out))

        if len(self._rate_window) < 3:
            return None
        w0, o0 = self._rate_window[0]
        dwall = now - w0
        if dwall <= 0.5:
            return None
        rate = (out - o0) / dwall
        return round(rate, 3) if rate >= 0 else None

    def _video_rate(self, progress: dict) -> float | None:
        """映像フレームの進みだけで見た実時間比。

        `_instant_rate` は out_time を使うが、FFmpeg の out_time は音声・映像の
        うち**進んでいる方**を指す。そのため映像が完全に止まっていても、音声が
        流れてさえいれば 1.00x に見える。実測では、映像のフレーム生成が128秒間
        止まっているあいだ、out_time も rate も 1.00x のままだった。

        ここはフレーム数と実時間だけで割るので、映像が止まれば必ず 0 になる。
        """
        target = self._target_fps
        frame = progress.get("frame")
        if not target or frame is None:
            return None

        now = time.monotonic()
        # 再接続やループでフレーム番号が巻き戻ったら窓を捨てる
        if self._video_window and frame < self._video_window[-1][1]:
            self._video_window.clear()
        self._video_window.append((now, frame))

        if len(self._video_window) < 3:
            return None
        w0, f0 = self._video_window[0]
        dwall = now - w0
        if dwall <= 0.5:
            return None
        rate = (frame - f0) / target / dwall
        return round(rate, 3) if rate >= 0 else None

    def _av_skew(self, progress: dict) -> float | None:
        """映像が音声エンコーダよりどれだけ後ろにいるか（参考値）。

        out_time（＝音声が進んでいればそれ）と、フレーム数から出した映像の到達点
        との差。

        **送出される中身のずれではない。** 出力側の `-max_interleave_delta 0` が
        映像を待たせるため、実際に出ていく順序のずれは 0.02 秒に収まっている
        （実測）。一方この値は、重い条件なら正常時でも 100 秒を超える。
        したがって**判定には使わない**。映像が遅れている事実は `_video_rate` が拾う。
        """
        target = self._target_fps
        frame = progress.get("frame")
        out = progress.get("out_time_sec")
        if not target or not frame or out is None:
            return None
        return round(out - frame / target, 1)

    def _store_sample(self, progress: dict) -> None:
        """指標を間引いてDBに残す。テスト後に振り返るための記録。"""
        if not self._job_id:
            return
        now = time.monotonic()
        at = now - self._job_started_at
        if at - self._last_sample_store < SAMPLE_STORE_SEC:
            return
        self._last_sample_store = at
        self.conn.execute(
            """INSERT INTO job_samples
               (job_id, at, rate, fps, bitrate_kbps, drop_frames, dup_frames, out_time_sec,
                video_rate, av_skew_sec)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                self._job_id, round(at, 1), progress.get("rate"), progress.get("fps"),
                progress.get("bitrate_kbps"), progress.get("drop_frames"),
                progress.get("dup_frames"), progress.get("out_time_sec"),
                progress.get("video_rate"), progress.get("av_skew_sec"),
            ),
        )
        self.conn.commit()

    def _store_event(self, kind: str, key: str = "", severity: str = "", message: str = "") -> None:
        if not self._job_id:
            return
        self.conn.execute(
            "INSERT INTO job_events (job_id, at, kind, key, severity, message) VALUES (?,?,?,?,?,?)",
            (self._job_id, round(time.monotonic() - self._job_started_at, 1),
             kind, key, severity, message),
        )
        self.conn.commit()

    def _heartbeat(self, progress: dict) -> None:
        """一定間隔で稼働状況を1行流す。

        順調なとき FFmpeg は何も出力しないため、画面が無言になり
        「本当に動いているのか」が分からない。数字は上のパネルにも出ているが、
        **時刻付きで履歴に残る**ことに意味がある（後から見返せる）。
        """
        now = time.monotonic()
        if self._last_heartbeat is None:
            self._last_heartbeat = now
            return
        interval = HEARTBEAT_SEC if self._heartbeats else FIRST_HEARTBEAT_SEC
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        self._heartbeats += 1

        elapsed = int(progress.get("out_time_sec") or 0)
        restarts = self._ctx.get("restart_times", [])
        # レートは映像基準を優先する。out_time 基準だと映像が止まっていても
        # 音声が流れていれば 1.00x と出てしまい、異常が読み取れない
        video_rate = progress.get("video_rate")
        rate = video_rate if video_rate is not None else (
            progress.get("rate") or progress.get("speed", 0)
        )
        parts = [
            f"経過 {elapsed // 3600:d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}",
            f"fps {progress.get('fps', 0):.1f}",
            f"bitrate {progress.get('bitrate_kbps', 0):.0f}k",
            f"レート {rate:.2f}x",
            f"drop {progress.get('drop_frames', 0)}",
        ]
        lag = progress.get("av_skew_sec")
        if lag is not None and abs(lag) >= VIDEO_LAG_LOG_SEC:
            parts.append(f"映像の遅れ {lag:.0f}秒")
        if restarts:
            parts.append(f"再接続 {len(restarts)}回")

        duration = self._ctx.get("duration") or 0
        if duration:
            remaining = max(0, duration - elapsed)
            parts.append(f"残り {remaining // 60}分")

        self._log("[streamforge] 送出中 — " + " / ".join(parts))

    def _clear(self, key: str) -> None:
        """解消した指摘を取り消す。出しっぱなしにしないための対。"""
        if key not in self._diagnoses:
            return
        del self._diagnoses[key]
        self._store_event("resolved", key=key)
        self._emit("diagnosis", {"key": key, "resolved": True})

    def _report(self, found) -> None:
        """翻訳した指摘を1回だけ流す。同じ内容の連発でログを埋めない。"""
        if self._diagnoses.get(found.key) == found.message:
            return
        self._diagnoses[found.key] = found.message
        self._store_event("diagnosis", key=found.key, severity=found.severity,
                          message=found.message)
        self._emit("diagnosis", {**found.as_dict(), "resolved": False})
        if found.severity == "error" and self._job_id:
            self.conn.execute(
                "UPDATE jobs SET last_error=? WHERE id=?", (found.message, self._job_id)
            )
            self.conn.commit()

    # ------------------------------------------------------------ 出力の処理

    async def _pump_progress(self, stream: asyncio.StreamReader | None) -> None:
        """`-progress pipe:1` の key=value 列を1ブロックずつ組み立てて流す。"""
        if stream is None:
            return
        block: dict[str, str] = {}
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            block[key] = value
            if key == "progress":
                self._last_progress = _shape_progress(block)
                self._last_progress["rate"] = self._instant_rate(self._last_progress)
                self._last_progress["video_rate"] = self._video_rate(self._last_progress)
                self._last_progress["av_skew_sec"] = self._av_skew(self._last_progress)
                # 静止画の更新が止まっていないか。stat() 1回なので毎秒でも無視できる。
                # キー自体を出さないことが「プレビュー無効」の合図になる
                if self._snapshot_spec() is not None:
                    self._last_progress["snapshot_age_sec"] = self._snapshot_age()
                self._emit("progress", self._last_progress)
                self._store_sample(self._last_progress)
                self._heartbeat(self._last_progress)
                found, resolved = self._health.observe(self._last_progress)
                for item in found:
                    self._report(item)
                for key in resolved:
                    self._clear(key)
                block = {}

    async def _pump_logs(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = redact(
                raw.decode("utf-8", "replace").rstrip(),
                url=self._redact_url,
                display_url=self._redact_display,
                values=self._secrets,
            )
            if not line:
                continue
            # ファイルには必ず全行残し、画面へ流す分だけ間引く
            if self._log_file:
                self._log_file.write(line + "\n")
                self._log_file.flush()
            for shown in self._throttle.feed(line, time.monotonic()):
                self._log_buffer.append(shown)
                self._emit("log", {"line": shown})

            found = diagnose_line(line, self._ctx.get("protocol", ""))
            if found:
                self._report(found)

    async def _wait_and_finalize(self, proc: asyncio.subprocess.Process, job_id: int) -> None:
        try:
            code = await proc.wait()
            # パイプの読み切りを少し待つ（終了直前のエラー行を取りこぼさないため）
            await asyncio.sleep(0.2)

            if not self._stopping and code != 0 and self._ctx.get("auto_restart"):
                if await self._try_restart(job_id, code):
                    return
        except asyncio.CancelledError:
            # 停止要求でキャンセルされた。後始末は stop() 側が finalize で行う
            raise

        self._finalize(job_id, code)

    def _finalize(self, job_id: int, code: int | None) -> None:
        """ジョブを終了状態にして記録する。二重に呼ばれても安全。

        **いま追跡しているジョブでなければ、DBだけ直して共有状態には触らない。**
        別のジョブのプロセスが終わっただけで稼働中フラグを落とすと、
        本当に動いているジョブが追跡不能になる（実際にそうなった）。
        """
        is_current = self._job_id == job_id
        if is_current and not self._active:
            return
        if is_current:
            self._active = False

        if self._stopping or code == 0:
            status, error = "stopped", None
        else:
            status = "failed"
            # 翻訳済みの説明があればそれを優先する。生の最終行より役に立つ
            translated = next(
                (m for k, m in self._diagnoses.items() if k not in ("slow_encode", "dropping")),
                None,
            )
            error = translated or (self._log_buffer[-1] if self._log_buffer else f"exit code {code}")

        self.conn.execute(
            "UPDATE jobs SET status=?, ended_at=?, exit_code=?, last_error=? WHERE id=?",
            (status, _now(), code, error, job_id),
        )
        self.conn.commit()

        if is_current:
            if self._log_file:
                self._log_file.close()
                self._log_file = None
            self._proc = None
            self._clear_snapshot()

        self._emit("status", {"job_id": job_id, "status": status, "exit_code": code, "error": error})

    # -------------------------------------------------------------- 自動再接続

    async def _try_restart(self, job_id: int, exit_code: int) -> bool:
        """異常終了を検知したら、指数バックオフを置いて同じ条件で立て直す。

        直したことは黙らせない。再接続の回数は障害の証拠になるので必ず記録して表示する
        （機能設計書 F-6）。
        """
        now = time.monotonic()
        times = self._ctx["restart_times"]
        times[:] = [t for t in times if now - t < 3600]

        if len(times) >= MAX_RESTARTS_PER_HOUR:
            self._log(
                f"[streamforge] 1時間に{MAX_RESTARTS_PER_HOUR}回を超えて再接続したため、"
                "自動再接続を止めます"
            )
            return False

        remaining = self._remaining_duration()
        if remaining is not None and remaining <= 1:
            return False   # どうせ終わる時間なので立て直さない

        attempt = len(times)
        delay = BACKOFF_SEC[min(attempt, len(BACKOFF_SEC) - 1)]
        times.append(now)

        self.conn.execute(
            "UPDATE jobs SET restart_count=restart_count+1 WHERE id=?", (job_id,)
        )
        self.conn.commit()

        self._emit("status", {
            "job_id": job_id, "status": "reconnecting",
            "attempt": attempt + 1, "delay_sec": delay, "exit_code": exit_code,
        })
        self._log(
            f"[streamforge] 異常終了（exit {exit_code}）。{delay}秒後に再接続します"
            f"（{attempt + 1}回目）"
        )

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return False
        if self._stopping:
            return False

        try:
            argv = self._build_argv(self._remaining_duration())
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self._log(f"[streamforge] 再接続に失敗しました: {exc}")
            return False

        self._proc = proc
        self._diagnoses.clear()   # 立て直したので指摘もリセットする
        self._health = ProgressHealth()
        self._throttle = LogThrottle()
        self._rate_window.clear()   # 再接続で out_time が巻き戻る
        self._video_window.clear()
        self._last_heartbeat = None
        self._heartbeats = 0
        self._rate_window.clear()
        self._video_window.clear()
        self._job_started_at = time.monotonic()
        self._last_sample_store = 0.0
        self._throttle = LogThrottle()
        self.conn.execute(
            "UPDATE jobs SET pid=?, pgid=? WHERE id=?", (proc.pid, proc.pid, job_id)
        )
        self.conn.commit()

        self._spawn(self._pump_progress(proc.stdout))
        self._spawn(self._pump_logs(proc.stderr))
        self._finalize_task = self._spawn(self._wait_and_finalize(proc, job_id))
        self._emit("status", {"job_id": job_id, "status": "running", "reconnected": True})
        return True

    # ------------------------------------------------------------------ 停止

    async def stop(self) -> bool:
        if not self._active:
            return False

        self._stopping = True
        job_id = self._job_id
        proc = self._proc

        if proc is None or proc.returncode is not None:
            # 再接続待ちの最中。バックオフの sleep を打ち切って、その場で終わらせる
            self._log("[streamforge] 再接続待ちを中止して停止します")
            if self._finalize_task and not self._finalize_task.done():
                self._finalize_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._finalize_task
            self._finalize(job_id, None)
            return True

        pid = proc.pid
        _kill_group(pid, pid, signal.SIGTERM)

        try:
            await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_SEC)
        except asyncio.TimeoutError:
            self._log("[streamforge] SIGTERM に応答しないため SIGKILL します")
            _kill_group(pid, pid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        return True

    async def shutdown(self) -> None:
        await self.stop()
        for task in list(self._tasks):
            task.cancel()


def _kill_group(pgid: int | None, pid: int | None, sig: int) -> None:
    """プロセスグループへ送る。取れなければ単体の pid にフォールバックする。"""
    if pgid:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if pid:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


def _shape_progress(block: dict[str, str]) -> dict[str, Any]:
    """FFmpeg の生の progress を UI が扱いやすい形に整える。"""

    def num(key: str, cast=float, default=None):
        value = block.get(key)
        if value in (None, "", "N/A"):
            return default
        try:
            return cast(value)
        except ValueError:
            return default

    bitrate = block.get("bitrate", "")
    speed = block.get("speed", "")
    out_time_us = num("out_time_us", int, 0) or 0

    return {
        "frame": num("frame", int, 0),
        "fps": num("fps", float, 0.0),
        "bitrate_kbps": _parse_bitrate(bitrate),
        "total_size": num("total_size", int, 0),
        "out_time_sec": round(out_time_us / 1_000_000, 1),
        "dup_frames": num("dup_frames", int, 0),
        "drop_frames": num("drop_frames", int, 0),
        "speed": _parse_speed(speed),
        "progress": block.get("progress", ""),
    }


def _parse_bitrate(value: str) -> float:
    # "4500.3kbits/s" / "N/A"
    if not value or value.startswith("N/A"):
        return 0.0
    try:
        return float(value.replace("kbits/s", "").strip())
    except ValueError:
        return 0.0


def _parse_speed(value: str) -> float:
    if not value or value.startswith("N/A"):
        return 0.0
    try:
        return float(value.replace("x", "").strip())
    except ValueError:
        return 0.0


def load_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    from .db import row_to_dict

    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row_to_dict(row)


def list_jobs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    from .db import row_to_dict

    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def read_log_tail(path: str | None, max_bytes: int) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        return fh.read().decode("utf-8", "replace")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
