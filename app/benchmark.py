"""この設定でこのマシンがどれだけ余裕を持って処理できるかを、その場で測る。

「落ちるかどうか」は機材と設定の組み合わせで決まる。カタログ的な目安を並べるより、
**実際に同じパイプラインを数秒回して実測する**ほうが確実で、説明もいらない。

出力先だけを `-f null` に差し替えるので、送出先には一切影響しない。
"""

from __future__ import annotations

import asyncio
import re
import time

MEASURE_SEC = 5.0
HARD_TIMEOUT = 30.0

# 実時間の何倍出れば安心か
COMFORTABLE = 2.0
TIGHT = 1.2


def to_null_output(argv: list[str]) -> list[str]:
    """送出用のコマンドを、計測用（どこにも送らない）に書き換える。

    build_command() は必ず `-f <muxer> <url>` で終わるので、**末尾の3つ**を
    落として `-f null -` に差し替える。

    先頭から `-f` を探してはいけない。テストパターンのときは入力側に
    `-f lavfi` があり、そこで切ると入力ごと消えてしまう（実際に踏んだ）。
    """
    if "image2" in argv:
        # セルフプレビュー付きのコマンドは出力が2本ある。末尾はプレビューなので、
        # ここで差し替えると送出側が残って**実際に送ってしまう**。
        # 計測用は snapshot=None で組み直すこと（黙って間違えるより落とす）
        raise ValueError("計測用のコマンドはセルフプレビュー無しで組み立ててください")

    body = argv[:-3] if len(argv) >= 3 and argv[-3] == "-f" else list(argv)

    out: list[str] = []
    skip_next = False
    for arg in body:
        if skip_next:
            skip_next = False
            continue
        if arg == "-t":          # タイムアウトはこちらの計測時間で切る
            skip_next = True
            continue
        out.append(arg)
    return out + ["-f", "null", "-"]


async def measure(argv: list[str]) -> dict:
    """数秒動かして、実時間比（speed）を返す。"""
    cmd = to_null_output(argv)
    # 実時間読みが残っていると必ず1.0倍になり、余裕が測れない。
    # **本体は組み立て側（build_command の pacing=False）で外す。**
    # ここは保険で、`-re` 系だけをもう一度見る（フィルタ側までは触らない）
    cmd = _strip_pacing(cmd)

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return {"ok": False, "message": f"計測を開始できませんでした: {exc}"}

    speeds: list[float] = []
    try:
        while time.monotonic() - started < MEASURE_SEC:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=HARD_TIMEOUT)
            if not line:
                break
            m = re.match(rb"speed=\s*([0-9.]+)", line)
            if m:
                speeds.append(float(m.group(1)))
    except asyncio.TimeoutError:
        pass
    finally:
        proc.kill()
        await proc.wait()

    # 最初のサンプルは立ち上がりで暴れるので捨てる
    useful = speeds[1:] or speeds
    if not useful:
        return {
            "ok": False,
            "message": "計測できませんでした。入力が読めているか確認してください",
        }

    speed = min(useful)
    return {
        "ok": True,
        "speed": round(speed, 2),
        "samples": len(useful),
        "verdict": _verdict(speed),
        "message": _message(speed),
    }


def _strip_pacing(cmd: list[str]) -> list[str]:
    """`-re` / `-readrate` 系を外す。付いたままだと必ず1.0倍になる。"""
    out: list[str] = []
    skip = False
    for arg in cmd:
        if skip:
            skip = False
            continue
        if arg == "-re":
            continue
        if arg in ("-readrate", "-readrate_catchup", "-readrate_initial_burst"):
            skip = True
            continue
        out.append(arg)
    return out


def _verdict(speed: float) -> str:
    if speed >= COMFORTABLE:
        return "comfortable"
    if speed >= TIGHT:
        return "tight"
    return "insufficient"


def _message(speed: float) -> str:
    if speed >= COMFORTABLE:
        return (
            f"実時間の {speed:.1f} 倍で処理できます。この設定なら余裕があります"
        )
    if speed >= TIGHT:
        return (
            f"実時間の {speed:.1f} 倍しか出ていません。動きますが余裕がないので、"
            "他の負荷が乗ると処理落ちする可能性があります"
        )
    return (
        f"実時間の {speed:.1f} 倍しか出ていません。この設定では処理落ちします。"
        "preset を軽くする（ultrafast など）か、解像度・fpsを下げてください"
    )
