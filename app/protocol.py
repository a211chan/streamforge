"""URL からの送出プロトコル判定と、パラメータの分解。

方針（機能設計書 F-1）:
  * URLを貼るだけでプロトコルを決める。人に選ばせない
  * 分解した値は「表示のため」であって、送出には原文URLをそのまま使う。
    分解ロジックが間違っていても送出は壊れない
  * 判定の根拠を必ず返す。黙って決めない
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

# 実際に送出できるプロトコル
SUPPORTED = {"rtmp", "srt", "whip"}

# 値を伏せて扱うクエリパラメータ
SECRET_QUERY_KEYS = {"passphrase"}


@dataclass
class Param:
    """UI に並べる1項目。"""

    key: str
    label: str
    value: str
    masked: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": "●" * 8 if self.masked else self.value,
            "masked": self.masked,
            "note": self.note,
        }


@dataclass
class Resolution:
    protocol: str | None
    supported: bool
    reason: str
    scheme: str = ""
    params: list[Param] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)  # 伏せ字にすべき実値
    suggested_url: str = ""   # 直したほうがよい場合の候補（勝手には使わない）

    def as_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "supported": self.supported,
            "reason": self.reason,
            "scheme": self.scheme,
            "params": [p.as_dict() for p in self.params],
            "warnings": self.warnings,
            "suggested_url": self.suggested_url,
        }


def parse_query(query: str) -> tuple[list[tuple[str, str]], list[str]]:
    """FFmpeg と同じ見方でクエリを分解する。

    `urllib.parse.parse_qsl` は使えない。理由が2つある。

    1. **`+` を空白に変換してしまう。** FFmpeg が SRT のオプションを取り出す
       `av_find_info_tag` は URL デコードを一切しないので、`streamid=a+b` は
       そのまま `a+b` として libsrt に渡る。parse_qsl で表示すると「a b」に見えて、
       実際に送られる値と食い違う（実際にこれで原因の切り分けが遠回りになった）。
    2. **空のセグメントを黙って捨てる。** `a=1&&b=2` の `&&` は parse_qsl だと
       消えるが、FFmpeg は空のオプション名として扱い
       `Query string option '' does not exist` で**接続前に失敗する**。
       表示から消してしまうと、UIを見ても原因が分からない。

    戻り値は (key, value) の列と、見つかった問題の列。
    """
    pairs: list[tuple[str, str]] = []
    issues: list[str] = []

    if not query:
        return pairs, issues

    for segment in query.split("&"):
        if not segment:
            issues.append("empty_segment")
            continue
        key, sep, value = segment.partition("=")
        if not key:
            issues.append("empty_key")
            continue
        # 値はデコードしない。FFmpeg が受け取るのと同じ文字列を見せる
        pairs.append((key, value if sep else ""))

    return pairs, issues


def _check_query_health(url: str, issues: list[str], res: Resolution) -> None:
    """クエリの書式そのものの問題を拾う。プロトコル共通。"""
    if not issues:
        return
    res.warnings.append(
        "クエリに空の項目があります（`&&` や末尾の `&`）。FFmpegは "
        "\"Query string option '' does not exist\" として、"
        "**接続を試みる前に**失敗します"
    )
    res.suggested_url = clean_query_url(url)


def clean_query_url(url: str) -> str:
    """空のクエリ項目を取り除いたURLを返す。提案用で、送出には使わない。"""
    parts = urlsplit(url)
    segments = [seg for seg in parts.query.split("&") if seg and seg.partition("=")[0]]
    return urlunsplit(parts._replace(query="&".join(segments)))


def resolve(url: str) -> Resolution:
    url = (url or "").strip()
    if not url:
        return Resolution(None, False, "URLが空です")

    scheme = urlsplit(url).scheme.lower()

    if scheme in ("rtmp", "rtmps"):
        res = Resolution("rtmp", True, f"{scheme}:// なのでRTMPと判定しました", scheme)
        _parse_rtmp(url, res)
        return res

    if scheme == "srt":
        res = Resolution("srt", True, "srt:// なのでSRTと判定しました", scheme)
        _parse_srt(url, res)
        return res

    if scheme in ("http", "https"):
        res = Resolution(
            "whip",
            True,
            f"{scheme}:// なのでWebRTC(WHIP)と判定しました"
            "（本ツールの出力先でHTTP(S)を使うのはWHIPだけです）",
            scheme,
        )
        _parse_whip(url, res)
        return res

    if scheme in ("rtsp", "udp", "rtp", "rist", "file"):
        return Resolution(None, False, f"{scheme}:// は本ツールの対象外です", scheme)

    # "host:1935" のようにスキームが無い文字列でも urlsplit は先頭を scheme として
    # 拾うことがある。"://" の有無で本当にスキーム付きかを見分ける。
    if not scheme or "://" not in url:
        return Resolution(
            None, False, "スキームがありません。rtmp:// または srt:// から始まるURLを貼ってください"
        )

    return Resolution(None, False, f"未知のスキーム {scheme}:// です", scheme)


# -------------------------------------------------------------------- WHIP

def _parse_whip(url: str, res: Resolution) -> None:
    parts = urlsplit(url)
    _check_query_health(url, parse_query(parts.query)[1], res)
    res.params.append(Param("host", "ホスト", parts.hostname or ""))
    if parts.port:
        res.params.append(Param("port", "ポート", str(parts.port)))
    res.params.append(Param("path", "パス", parts.path or "/"))

    if "/whip" not in parts.path.lower():
        res.warnings.append(
            "パスに /whip が含まれていません。WHIPのエンドポイントか確認してください"
        )
    if parts.scheme == "http":
        res.warnings.append(
            "http:// です。WHIPはDTLSを張るため、通常はhttps://のエンドポイントが必要です"
        )


# ------------------------------------------------------------------- RTMP

def _parse_rtmp(url: str, res: Resolution) -> None:
    parts = urlsplit(url)
    _check_query_health(url, parse_query(parts.query)[1], res)
    segments = [s for s in parts.path.split("/") if s]

    res.params.append(Param("host", "ホスト", parts.hostname or ""))
    if parts.port:
        res.params.append(Param("port", "ポート", str(parts.port)))

    # rtmp://host/app/streamkey の末尾をキー候補として切り出す。
    # セグメントが1つしか無い場合はアプリ名なので、キー扱いしない。
    if len(segments) >= 2:
        key = segments[-1]
        app = "/".join(segments[:-1])
        res.params.append(Param("app", "アプリ", app))
        res.params.append(
            Param("stream_key", "ストリームキー", key, masked=True, note="末尾セグメントからの推定")
        )
        res.secrets["stream_key"] = key
    else:
        res.params.append(Param("app", "アプリ", "/".join(segments)))
        res.warnings.append(
            "ストリームキーらしきセグメントがありません。"
            "配信先によっては URL の末尾にキーを足す必要があります"
        )


# -------------------------------------------------------------------- SRT

def _parse_srt(url: str, res: Resolution) -> None:
    parts = urlsplit(url)
    pairs, issues = parse_query(parts.query)
    query = dict(pairs)

    _check_query_health(url, issues, res)

    res.params.append(Param("host", "ホスト", parts.hostname or ""))
    res.params.append(Param("port", "ポート", str(parts.port) if parts.port else ""))
    if not parts.port:
        res.warnings.append("ポートがありません。SRTはポート必須です")

    if "streamid" in query:
        res.params.append(Param("streamid", "streamid", query["streamid"]))

    if "latency" in query:
        res.params.append(
            Param("latency", "latency", query["latency"], note="マイクロ秒（FFmpegの仕様）")
        )
        _warn_latency_unit(query["latency"], res)

    for key in ("rcvlatency", "peerlatency"):
        if key in query:
            res.params.append(Param(key, key, query[key], note="マイクロ秒"))
            _warn_latency_unit(query[key], res, key)

    if "passphrase" in query:
        res.params.append(Param("passphrase", "passphrase", query["passphrase"], masked=True))
        res.secrets["passphrase"] = query["passphrase"]
        if "pbkeylen" not in query:
            res.warnings.append(
                "passphrase がありますが pbkeylen が未指定です。FFmpegの既定は16(AES-128)なので、"
                "配信先が256bitを要求する場合は pbkeylen=32 を足してください"
            )

    if "pbkeylen" in query:
        res.params.append(Param("pbkeylen", "pbkeylen", query["pbkeylen"]))

    mode = query.get("mode", "caller")
    res.params.append(Param("mode", "mode", mode))
    if mode != "caller":
        res.warnings.append(
            f"mode={mode} が指定されています。本ツールは送出側（caller）を前提に設計しています"
        )


def _warn_latency_unit(value: str, res: Resolution, key: str = "latency") -> None:
    """FFmpeg の SRT latency はマイクロ秒。ミリ秒のつもりの値を検出して警告する。

    Haivision 系のドキュメントや他ツールの UI はミリ秒で書かれていることが多く、
    `latency=200` をそのまま貼ると 0.2ms になってしまう。実務で刺さる罠なので明示する。
    """
    try:
        num = int(value)
    except ValueError:
        return
    if 0 < num < 10000:
        res.warnings.append(
            f"{key}={num} はFFmpegでは{num}マイクロ秒（{num / 1000:.3g}ミリ秒）として扱われます。"
            f"{num}ミリ秒のつもりなら {key}={num * 1000} です"
        )
