"""FFmpeg の出力を、原因の説明に翻訳する（機能設計書 F-6）。

憲章 Principle 3。stderr をそのまま出しても切り分けの役に立たない。
「何が起きたか」ではなく「次に何を確認すればいいか」まで言い切る。

翻訳は**追加情報**として出すもので、生ログは常に併記する。
翻訳が外れていたときに元の手がかりが消えないようにするため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Diagnosis:
    key: str
    message: str
    severity: str   # "error" | "warning"

    def as_dict(self) -> dict:
        return {"key": self.key, "message": self.message, "severity": self.severity}


# (キー, 正規表現, プロトコル制限, 重大度, メッセージ)
# 上から順に評価し、最初に当たったものを採用する。
# 具体的で行動につながるものほど前に置く（例: 空クエリは「接続失敗」より前）。
_RULES: list[tuple[str, str, str | None, str, str]] = [
    (
        "bad_query_option",
        r"query string option '' does not exist|option not found",
        None,
        "error",
        "URLのクエリに空の項目があります（`&&` や末尾の `&`）。"
        "FFmpegは接続を試みる前にここで失敗します。URLを貼り直してください",
    ),
    (
        "rtmp_connect",
        r"connection to tcp://.*failed|error opening output .*rtmp://",
        "rtmp",
        "error",
        "RTMPサーバーに接続できません。URL・ポート（既定1935）・ファイアウォールを確認してください",
    ),
    (
        "srt_km_refused",
        r"km refused|encryption failed|wrong passphrase",
        None,
        "error",
        "SRTのpassphraseが一致していません。配信先の指定と pbkeylen も合わせて確認してください",
    ),
    (
        "srt_dropped",
        r"error submitting a packet to the muxer|error muxing a packet",
        "srt",
        "error",
        "接続後に切断されました。同じ streamid で既に配信中でないか、"
        "配信先の同時接続数・権限・帯域の制限を確認してください",
    ),
    (
        "srt_handshake",
        r"connection to srt://.*failed|srt.*(connection setup failure|timeout)",
        "srt",
        "error",
        "SRTのハンドシェイクが完了しませんでした。ポート（UDP）が開いているか、"
        "streamid が配信先の要求形式と一致しているかを確認してください",
    ),
    (
        "auth",
        r"\b(401|403)\b|unauthorized|not authorized|forbidden",
        None,
        "error",
        "認証に失敗しました。ストリームキーまたは streamid を確認してください",
    ),
    (
        "dns",
        r"name or service not known|failed to resolve|nodename nor servname",
        None,
        "error",
        "ホスト名を解決できません。URLのホスト部分を確認してください",
    ),
    (
        "refused",
        r"connection refused",
        None,
        "error",
        "接続を拒否されました。宛先のサーバーが起動しているか確認してください",
    ),
    (
        "timeout",
        r"connection timed out|operation timed out",
        None,
        "error",
        "接続がタイムアウトしました。経路とファイアウォールを確認してください",
    ),
    (
        "input_gone",
        r"error in the pull function|input/output error.*in#0|end of file",
        None,
        "error",
        "入力ソースが切れました。外部ライブソースの場合は配信元の状態を確認してください",
    ),
    (
        "no_such_file",
        r"no such file or directory",
        None,
        "error",
        "入力ファイルが見つかりません。media ディレクトリの中身を確認してください",
    ),
    (
        "invalid_data",
        r"invalid data found when processing input",
        None,
        "error",
        "入力を解釈できません。対応していない形式か、ファイルが壊れている可能性があります",
    ),
    (
        "buffer_underflow",
        r"buffer underflow|circular buffer overrun|thread message queue blocking",
        None,
        "warning",
        "入力の取り込みが追いついていません。回線か素材の読み出し速度を確認してください",
    ),
    (
        "past_duration",
        r"past duration .* too large",
        None,
        "warning",
        "タイムスタンプが乱れています。ループの継ぎ目や可変フレームレートの素材で起きます",
    ),
]

_COMPILED = [
    (key, re.compile(pattern, re.IGNORECASE), proto, sev, msg)
    for key, pattern, proto, sev, msg in _RULES
]


def diagnose_line(line: str, protocol: str = "") -> Diagnosis | None:
    """ログ1行を見て、当てはまる説明を1つ返す。"""
    for key, pattern, only_proto, severity, message in _COMPILED:
        if only_proto and protocol and only_proto != protocol:
            continue
        if pattern.search(line):
            return Diagnosis(key, message, severity)
    return None


SLOW_THRESHOLD = 0.9
STARTUP_GRACE_SAMPLES = 4   # 起動直後はパイプラインが温まるまで見ない
SLOW_STREAK_SAMPLES = 3     # 連続で遅いときだけ言う

# セルフプレビューの静止画が何秒更新されなければ異常とみなすか。
# 毎秒1枚書いているので、15秒空くのは明確に異常
SNAPSHOT_STALE_SEC = 15.0
# 起動直後は1枚目がまだ無い。ここを待たずに見ると必ず誤報になる
SNAPSHOT_GRACE_SAMPLES = 20


def pick_rate(progress: dict) -> tuple[float, bool]:
    """判定に使うレートと、それが映像基準かどうかを返す。

    **映像基準を優先する。** FFmpeg の out_time は音声・映像のうち進んでいる方を
    指すため、映像が完全に止まっていても音声が流れていれば 1.00x に見える。
    実測では映像が128秒間止まっているあいだ、out_time 基準の値は 1.00x のままで、
    警告が一度も出なかった。
    """
    video = progress.get("video_rate")
    if video is not None:
        return float(video), True
    rate = progress.get("rate")
    if rate is None:
        rate = progress.get("speed") or 0
    return float(rate), False


class ProgressHealth:
    """進捗の数値から不調を見る。ジョブごとに1つ持つ。

    `-re` で実時間読みしている以上、speed は 1.0 で頭打ちになる。つまり
    **揺らぎは必ず下振れとして現れる**。1サンプルの落ち込みで警告を出して
    出しっぱなしにすると、実際には正常に流れているのに「追いついていません」が
    画面に残り続ける。実際にこれで誤解を招いた。

    そこで、
      * 起動直後の数サンプルは見ない
      * 連続して遅いときだけ警告する
      * 回復したら取り消す（解消も通知する）
    """

    def __init__(self) -> None:
        self._samples = 0
        self._slow_streak = 0
        self._slow_active = False
        self._last_drops = 0
        self._snapshot_stale = False

    def observe(self, progress: dict) -> tuple[list[Diagnosis], list[str]]:
        """(新しく出す指摘, 解消したキー) を返す。"""
        found: list[Diagnosis] = []
        resolved: list[str] = []
        self._samples += 1

        # 累積平均ではなく瞬間レートで見る。累積だと序盤の落ち込みに引きずられて
        # 実態より良く見え、遅れ続けていても閾値を超えて警告が出ない
        speed, is_video = pick_rate(progress)
        # 映像基準の 0 は「映像が止まっている」という実測値なので必ず拾う。
        # out_time 基準の 0 は進捗が N/A のときにも出るので、そちらは従来どおり外す
        slow = speed < SLOW_THRESHOLD and (is_video or speed > 0)
        if self._samples > STARTUP_GRACE_SAMPLES and slow:
            self._slow_streak += 1
        else:
            if self._slow_active:
                resolved.append("slow_encode")
                self._slow_active = False
            self._slow_streak = 0

        if self._slow_streak >= SLOW_STREAK_SAMPLES and not self._slow_active:
            self._slow_active = True
            stopped = is_video and speed <= 0.01
            if stopped:
                message = (
                    f"映像が出ていません（{self._slow_streak}秒間、"
                    "フレームが1枚も進んでいません）。"
                    "音声だけが送出されている状態です。"
                    "設定を軽くするか、エンコーダを変えてください"
                )
            else:
                message = (
                    f"実時間に追いついていません（映像 {speed:.2f}x が"
                    f"{self._slow_streak}秒続いています）。原因は2つあります: "
                    "① エンコードが重い ② 送出先への書き込みが詰まっている"
                    "（上り帯域不足など）。"
                    "「処理能力を測る」を押すと、送出せずに処理だけを計測できるので"
                    "切り分けられます"
                )
            found.append(
                Diagnosis("slow_encode", message, "error" if stopped else "warning")
            )

        # av_skew_sec では警告を出さない。
        # これは out_time（音声エンコーダの到達点）と映像フレームの差であって、
        # **実際に送出された中身のずれではない**。出力側の
        # `-max_interleave_delta 0` が映像を待たせるため、送出のずれは 0.02 秒に
        # 収まっている（実測）のに、この値は重い条件では平常時でも 100 秒を超える。
        # ここで鳴らすと誤報になる。映像が遅れている事実は video_rate が拾う

        # セルフプレビューの静止画が更新されているか。
        # **これは fps や out_time とは独立した、映像が出ていることの裏取りになる。**
        # 実際にこのプロジェクトは「out_time は 1.00x のまま映像だけ128秒停止」を
        # 踏んでいる。数値を信じきらないための2本目の物差しとして見る。
        #
        # 映像が止まっていること自体は上の slow_encode が言うので、ここでは
        # **数値は正常なのに絵だけ止まっている**場合だけを拾う（言うことを重ねない）。
        if "snapshot_age_sec" in progress:
            age = progress.get("snapshot_age_sec")
            stale = age is None or age > SNAPSHOT_STALE_SEC
            numbers_look_fine = (not is_video) or speed > 0.1
            if stale and numbers_look_fine and self._samples > SNAPSHOT_GRACE_SAMPLES:
                if not self._snapshot_stale:
                    self._snapshot_stale = True
                    found.append(
                        Diagnosis(
                            "snapshot_stale",
                            "セルフプレビューの絵が更新されていません"
                            + (f"（{age:.0f}秒前のまま）。" if age is not None
                               else "（1枚も出ていません）。")
                            + "fps などの数値は正常なので、原因は2つあります: "
                            "① フィルタを通った後の映像だけが止まっている "
                            "② 静止画の書き出しに失敗している（保存先の空き容量など）。"
                            "受信側の絵も止まっているかを確認してください",
                            "warning",
                        )
                    )
            elif self._snapshot_stale and not stale:
                self._snapshot_stale = False
                resolved.append("snapshot_stale")

        drops = progress.get("drop_frames") or 0
        if drops > self._last_drops:
            delta = drops - self._last_drops
            self._last_drops = drops
            found.append(
                Diagnosis(
                    "dropping",
                    f"フレームを落としています（直近 {delta} 枚 / 累計 {drops} 枚）。"
                    "上り帯域または送出先の受信能力を確認してください",
                    "warning",
                )
            )

        return found, resolved


def diagnose_progress(progress: dict) -> list[Diagnosis]:
    """1サンプルだけを見る版。連続性を見ないので、単発の判定にだけ使う。

    実際のジョブ監視には ProgressHealth を使うこと。
    """
    found: list[Diagnosis] = []
    speed, is_video = pick_rate(progress)
    if speed < SLOW_THRESHOLD and (is_video or speed > 0):
        found.append(
            Diagnosis(
                "slow_encode",
                f"エンコードが実時間に追いついていません（{speed:.2f}x）",
                "warning",
            )
        )
    if (progress.get("drop_frames") or 0) > 0:
        found.append(
            Diagnosis(
                "dropping",
                f"フレームを {progress['drop_frames']} 枚落としています",
                "warning",
            )
        )
    return found
