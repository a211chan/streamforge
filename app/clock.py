"""サーバー時刻のずれを測る（機能設計書 F-4-1 の前提確認）。

壁時計モードで焼き込んだ時刻を受信側と比べて遅延を出す以上、
**サーバーの時計が合っていること**がこの機能の前提になる。合っていなければ
測った遅延はそのままずれるので、UIに状態を出して気づけるようにする。

`chronyc` や `sntp` はOSごとに有無も出力も違うため、SNTP を自前で1往復させる。
依存を増やさずに macOS と Linux で同じ結果が得られる。
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

# 1900-01-01 から 1970-01-01 までの秒数（NTP epoch → Unix epoch）
NTP_DELTA = 2_208_988_800
DEFAULT_SERVER = "time.apple.com"
TIMEOUT_SEC = 2.0
CACHE_SEC = 60.0

_cache: tuple[float, dict] | None = None


@dataclass
class ClockStatus:
    ok: bool
    offset_ms: float | None
    server: str
    message: str

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "offset_ms": self.offset_ms,
            "server": self.server,
            "message": self.message,
        }


def query_offset(server: str = DEFAULT_SERVER) -> ClockStatus:
    """SNTP を1往復させて、システム時計とのずれをミリ秒で返す。"""
    packet = bytearray(48)
    packet[0] = 0x1B   # LI=0, VN=3, Mode=3 (client)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT_SEC)
    try:
        t1 = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
        t4 = time.time()
    except (socket.timeout, OSError) as exc:
        return ClockStatus(False, None, server, f"NTPサーバーに問い合わせできません: {exc}")
    finally:
        sock.close()

    if len(data) < 48:
        return ClockStatus(False, None, server, "NTPの応答が不正です")

    # 受信タイムスタンプ(t2)と送信タイムスタンプ(t3)
    t2 = _to_unix(struct.unpack("!II", data[32:40]))
    t3 = _to_unix(struct.unpack("!II", data[40:48]))

    # 往復遅延を折半して、システム時計のずれを求める
    offset = ((t2 - t1) + (t3 - t4)) / 2
    offset_ms = round(offset * 1000, 1)

    if abs(offset_ms) < 50:
        msg = f"時刻同期は良好です（ずれ {offset_ms:+.1f} ms）"
        ok = True
    elif abs(offset_ms) < 250:
        msg = f"時刻が {offset_ms:+.1f} ms ずれています。絶対遅延の測定値もこの分ずれます"
        ok = True
    else:
        msg = (
            f"時刻が {offset_ms:+.1f} ms ずれています。"
            "壁時計モードでの絶対遅延の測定は当てになりません（フレーム番号モードを使ってください）"
        )
        ok = False

    return ClockStatus(ok, offset_ms, server, msg)


def cached_status(server: str = DEFAULT_SERVER) -> dict:
    """UIから何度も叩かれるので短時間キャッシュする。"""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_SEC:
        return _cache[1]
    status = query_offset(server).as_dict()
    _cache = (now, status)
    return status


def _to_unix(parts: tuple[int, int]) -> float:
    seconds, fraction = parts
    return seconds - NTP_DELTA + fraction / 2**32
