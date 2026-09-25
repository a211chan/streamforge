"""疎通確認（機能設計書 F-1）。

本番のビットレートで投げる前に、最小構成で数秒だけ送ってハンドシェイクの成否だけを見る。
「設定を保存する前に、そもそも繋がるのか」を切り分けるための機能。
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from . import srtprobe
from .command import build_command
from .models import EncodeSettings, SourceSpec
from .secrets import redact

PROBE_SECONDS = 3
HARD_TIMEOUT = 20.0

# 最小構成。ここを重くすると「繋がるが重い」と「繋がらない」が混ざる
PROBE_ENCODE = EncodeSettings(
    width=320, height=240, fps=15, bitrate_kbps=200, gop_sec=1.0, preset="ultrafast", bframes=0
)
PROBE_SOURCE = SourceSpec(with_tone=True)


async def test_connection(
    ffmpeg_path: str, protocol: str, url: str, secrets=(), display_url: str = "",
    bearer_token: str = "",
) -> dict:
    argv = build_command(
        ffmpeg_path=ffmpeg_path,
        protocol=protocol,
        target_url=url,
        source=PROBE_SOURCE,
        encode=PROBE_ENCODE,
        duration_sec=PROBE_SECONDS,
        bearer_token=bearer_token,
    )

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return {"ok": False, "message": f"FFmpeg を起動できませんでした: {exc}", "log": ""}

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=HARD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "ok": False,
            "message": f"{HARD_TIMEOUT:.0f}秒たっても終わらないため打ち切りました",
            "log": "",
            "elapsed_sec": round(time.monotonic() - started, 1),
        }

    elapsed = round(time.monotonic() - started, 1)
    log = redact(
        stderr.decode("utf-8", "replace").strip(),
        url=url, display_url=display_url, values=secrets,
    )
    ok = proc.returncode == 0
    message = _summarize(ok, protocol, proc.returncode, log)

    result = {
        "ok": ok,
        "exit_code": proc.returncode,
        "elapsed_sec": elapsed,
        "message": message,
        "log": log[-4000:],
    }

    if not ok and protocol == "srt":
        if _connected_then_dropped(log):
            # ここまで来ているなら経路は問題ない。到達性を調べても意味がないし、
            # 「応答がありません」と出すのはむしろ誤誘導になる
            result["message"] = (
                "接続は成立しましたが、送出中に切断されました。"
                "同じ streamid で既に配信中でないか、公開先の権限・同時接続数・"
                "帯域の制限を確認してください"
            )
        else:
            # 一度も繋がっていない場合だけ、原因が「経路」か「認証情報」かを分ける。
            # FFmpeg はどちらも Input/output error としか言わないため
            hint = await _srt_reachability(url)
            if hint:
                result["srt_probe"] = hint
                result["message"] = hint["explanation"]

    return result


def _connected_then_dropped(log: str) -> bool:
    """接続は成立したが、その後に切れたことを示す痕跡があるか。"""
    lowered = log.lower()
    if "connection to srt://" in lowered and "failed" in lowered:
        return False   # そもそも繋がっていない
    return any(
        marker in lowered
        for marker in (
            "error submitting a packet",
            "error muxing a packet",
            "error writing trailer",
        )
    )


async def _srt_reachability(url: str) -> dict | None:
    """SRT の induction ハンドシェイクだけを投げて、応答の有無を確かめる。"""
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        return None
    probe_result = await asyncio.to_thread(srtprobe.probe, parts.hostname, parts.port)
    return {
        **probe_result.as_dict(),
        "explanation": srtprobe.explain(probe_result, parts.hostname, parts.port),
    }


def _summarize(ok: bool, protocol: str, code: int | None, log: str) -> str:
    if ok:
        return f"{protocol.upper()} で {PROBE_SECONDS}秒間の送出に成功しました"

    lowered = log.lower()

    # P4 の本格的なエラー翻訳の前に、疎通確認で頻出するものだけ拾っておく
    if "does not exist" in lowered or "option not found" in lowered:
        return (
            "URLのクエリに空の項目があります（`&&` や末尾の `&`）。"
            "接続を試みる前に失敗しています"
        )
    if "connection refused" in lowered:
        return "接続を拒否されました。ホスト・ポート・サーバーの起動状態を確認してください"
    if "km refused" in lowered or "encryption failed" in lowered:
        return "SRTのpassphraseが一致していません"
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "接続がタイムアウトしました。"
            + ("SRTはUDPです。ポートとファイアウォールを確認してください" if protocol == "srt"
               else "ホスト・ポート・ファイアウォールを確認してください")
        )
    if "no route to host" in lowered or "name or service not known" in lowered \
            or "failed to resolve" in lowered:
        return "ホスト名を解決できません。URLのホスト部分を確認してください"
    if "403" in lowered or "unauthorized" in lowered or "not authorized" in lowered:
        if protocol == "whip":
            return "WHIPエンドポイントの認証に失敗しました。Bearerトークンを確認してください"
        return "認証に失敗しました。ストリームキーまたは streamid を確認してください"
    if protocol == "whip" and ("dtls" in lowered or "ice" in lowered or "handshake" in lowered):
        return (
            "WebRTCのハンドシェイク（ICE/DTLS）が完了しませんでした。"
            "エンドポイントURLと、UDPが通る経路かを確認してください"
        )

    # SRT は原因を問わず "Connection to ... failed: Input/output error" になりがちで、
    # ログを見ても何も分からない。切り分けの入口だけは示す
    if "connection to" in lowered and "failed" in lowered:
        if protocol == "srt":
            return (
                "SRTのハンドシェイクが完了しませんでした。ホストとポート（UDP）が開いているか、"
                "streamid と passphrase が配信先の要求と一致しているかを確認してください"
            )
        return "接続に失敗しました。URL・ポート・ファイアウォールを確認してください"

    return f"送出に失敗しました（exit {code}）。下のログを確認してください"
