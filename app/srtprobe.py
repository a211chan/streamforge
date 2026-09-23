"""SRT サーバーが「そもそも応答するか」だけを確かめる（機能設計書 F-1 の補助）。

FFmpeg で送出して失敗したとき、その失敗は次の2つのどちらかで、
**やるべきことが正反対**になる。

  A. サーバーから一切応答がない … 経路・ポート・プロビジョニングの問題。
     streamid や passphrase をいくら直しても無駄
  B. 応答はあるが拒否された     … streamid / passphrase / 権限の問題

ところが FFmpeg はどちらも `Input/output error` としか言わない。実際にこれで
切り分けに時間を取られたので、A と B を機械的に分けられるようにした。

SRT のハンドシェイクは2段階で、1段目（induction）は **streamid を送る前**に
行われる。つまり induction に応答が返るかどうかは、認証情報とは無関係に
「そこに SRT サーバーが居るか」だけを教えてくれる。
"""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass

# SRT control packet: 先頭ビットが1、続く15ビットが制御タイプ（0 = HANDSHAKE）
_CONTROL_HANDSHAKE = 0x8000

HS_TYPE_INDUCTION = 1
HS_TYPE_CONCLUSION = -1

DEFAULT_TIMEOUT = 3.0


@dataclass
class SrtProbeResult:
    responded: bool
    elapsed_ms: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "responded": self.responded,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "detail": self.detail,
        }


def _induction_packet() -> bytes:
    """HSv4 形式の induction 要求。listener は HSv5 でも必ずこれに応答する。"""
    header = struct.pack(
        "!HHIII",
        _CONTROL_HANDSHAKE,   # 制御パケット / タイプ HANDSHAKE
        0,                    # サブタイプ
        0,                    # type-specific information
        0,                    # タイムスタンプ
        0,                    # 宛先ソケットID（induction では 0）
    )
    body = struct.pack(
        "!IHHIIIiII16s",
        4,                                   # version（induction では 4 を送る）
        0,                                   # encryption field
        2,                                   # extension field
        int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF,  # 初期シーケンス番号
        1500,                                # MTU
        8192,                                # フロー制御ウィンドウ
        HS_TYPE_INDUCTION,                   # ハンドシェイク種別
        int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF,  # 自分のソケットID
        0,                                   # SYN クッキー
        b"\x00" * 16,                        # ピアIPアドレス
    )
    return header + body


def probe(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> SrtProbeResult:
    """induction 要求を1発投げて、応答の有無を返す。"""
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_induction_packet(), (host, port))
        data, _ = sock.recvfrom(2048)
    except socket.timeout:
        return SrtProbeResult(
            False,
            (time.monotonic() - started) * 1000,
            f"{timeout:.0f}秒待っても応答がありません",
        )
    except socket.gaierror as exc:
        return SrtProbeResult(False, 0.0, f"ホスト名を解決できません: {exc}")
    except OSError as exc:
        # ICMP port unreachable はここに来る。「届いてはいる」ことの証拠になる
        return SrtProbeResult(
            False,
            (time.monotonic() - started) * 1000,
            f"到達不能の応答が返りました（{exc}）。ホストには届いていますが、"
            "そのポートで待ち受けているものがありません",
        )
    finally:
        sock.close()

    elapsed = (time.monotonic() - started) * 1000
    if len(data) < 16 or not (data[0] & 0x80):
        return SrtProbeResult(True, elapsed, f"SRT以外の応答が返りました（{len(data)}バイト）")

    return SrtProbeResult(True, elapsed, "SRTサーバーが応答しました")


def explain(result: SrtProbeResult, host: str, port: int) -> str:
    """UI に出す文章。次に何を確認すべきかまで書く。"""
    if result.responded:
        return (
            f"{host}:{port} の SRT サーバーは応答しています（{result.elapsed_ms:.0f}ms）。"
            "送出に失敗する場合、原因は経路ではなく streamid / passphrase / 権限の側にあります"
        )
    return (
        f"{host}:{port} から応答がありません（{result.detail}）。"
        "SRTのハンドシェイクは1段目で streamid を送らないので、これは "
        "**streamid や passphrase とは無関係**の問題です。"
        "ポート番号・UDPの経路・配信先側でingestが有効になっているかを確認してください"
    )
