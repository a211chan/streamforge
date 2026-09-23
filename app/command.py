"""Command Builder — 設定から FFmpeg の引数列を組み立てる。

ここが本ツールの中核。UI・DB・プロセス管理から独立した純関数として保つことで、
テストしやすく、生成結果をそのままユーザーに見せられる（憲章 Principle 5）。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .models import EncodeSettings, OverlaySettings, SourceSpec
from .overlay import build_filters

# プロトコル → 出力コンテナ
MUXER_BY_PROTOCOL = {
    "rtmp": "flv",
    "srt": "mpegts",
    "whip": "whip",
}

# 音声コーデック → FFmpeg のエンコーダー名
AUDIO_ENCODERS = {"aac": "aac", "opus": "libopus"}

# フィルタグラフの出口につけるラベル。送出用とセルフプレビュー用の2本に分ける
VIDEO_LABEL = "vout"
SNAPSHOT_LABEL = "snap"

# セルフプレビューの JPEG 品質（2〜31、小さいほど高品質）。
# 7 で 480px 幅が約11KB。1秒に1枚なら約90kbps で、送出と帯域を奪い合わない
SNAPSHOT_QUALITY = 7


@dataclass(frozen=True)
class SnapshotSpec:
    """セルフプレビュー（静止画）の出力先と粒度。

    値は `Config` から渡る。ここで既定を持たないのは、設定ファイルで
    切れるようにしておくため（非力な移植先で落とせる余地を残す）。
    """

    path: str
    width: int = 480
    fps: float = 1.0


def adapt_encode(
    enc: EncodeSettings, protocol: str, webrtc_delivery: bool = False
) -> tuple[EncodeSettings, list[str]]:
    """WebRTC で運ぶ場合の制約に合わせて設定を調整し、変更点を返す。

    対象は2つ。

      * WHIP で**送出する**とき（protocol == "whip"）
      * RTMP/SRT で送るが、**配信先が WebRTC で配信する**とき
        （Ceeblue のような WebRTC CDN）。この場合ブラウザは constrained baseline で
        ネゴシエートするため、Bフレーム入りの Main/High を送ると
        **映像だけ再生できず真っ暗になる**（音声は別コーデックなので正常に聞こえる）

    黙って直すのではなく、**何を変えたかを呼び出し側に返して UI に出す**
    （憲章 Principle 1）。
    """
    if enc.mode == "passthrough" or not (protocol == "whip" or webrtc_delivery):
        return enc, []

    changes: list[str] = []
    data = enc.model_dump()

    # 音声を Opus にするのは、自分が WebRTC の送信者になる WHIP のときだけ。
    # SRT/RTMP で WebRTC CDN に入れる場合、音声は MPEG-TS/FLV で AAC のまま運び、
    # Opus への変換は配信先が行う。ここで Opus にすると逆に運べなくなる
    if protocol == "whip":
        if enc.audio_codec != "opus":
            data["audio_codec"] = "opus"
            changes.append("音声を AAC から Opus に読み替えました（WebRTCはOpus必須）")
        if enc.channels > 2:
            data["channels"] = 2
            changes.append("音声を2chに落としました（WebRTCのOpusは2chまで）")
        if enc.samplerate != 48000:
            data["samplerate"] = 48000
            changes.append("音声を48kHzにしました（WebRTCのOpusは48kHz）")

    if enc.bframes:
        data["bframes"] = 0
        changes.append(
            "Bフレームを無効にしました。"
            "WebRTCのH.264はBフレームを運べず、入れると映像だけ真っ暗になります"
        )
    if enc.gop_sec > 2.0:
        data["gop_sec"] = 2.0
        changes.append("GOPを2秒に短縮しました（受信側が映像を出し始めるまでが速くなります）")
    if enc.profile != "baseline":
        data["profile"] = "baseline"
        changes.append(
            "H.264プロファイルを baseline にしました"
            "（WebRTCのブラウザは constrained baseline でネゴシエートします）"
        )

    return EncodeSettings.model_validate(data), changes


def build_input_args(
    source: SourceSpec,
    enc: EncodeSettings,
    media_dir: str = "",
    seam_frames: int = 0,
    pacing: bool = True,
) -> list[str]:
    """入力部を組み立てる。

    `-re`（実時間読み）の扱いがソースごとに違うのが要点。

      * testsrc / file … 付ける。付けないと全力で生成・デコードして
        送出先にバースト送信してしまう
      * live_url      … **中身次第**。本物のライブなら付けない（入力がすでに
        実時間で流れてくるため、さらに絞ると二重に待つことになる）。
        VOD の再生リストなら付ける（付けないと数十倍速で送出してしまう）。
        判定は `sources.resolve_pace()`
    """
    if source.type == "testsrc":
        return _testsrc_input(source, enc, pacing)
    if source.type == "file":
        return _file_input(source, media_dir, seam_frames, enc, pacing)
    if source.type == "live_url":
        return _live_input(source, enc, pacing)
    raise ValueError(f"未知のソース種別: {source.type}")


def _testsrc_input(
    source: SourceSpec, enc: EncodeSettings, pacing: bool = True
) -> list[str]:
    if source.pattern == "smptebars":
        video = f"smptebars=size={enc.width}x{enc.height}:rate={enc.fps}"
    else:
        video = f"testsrc2=size={enc.width}x{enc.height}:rate={enc.fps}"

    # lavfi は放っておくと全力で生成するが、絞りは realtime フィルタ側で行う。
    # -c copy のときだけフィルタを通らないので入力側で絞る
    pace = _pacing_input_args(source, enc, pacing)
    args = pace + ["-f", "lavfi", "-i", video]
    if source.with_tone:
        args += pace + ["-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate={enc.samplerate}"]
    return args


def _file_input(
    source: SourceSpec,
    media_dir: str,
    seam_frames: int = 0,
    enc: EncodeSettings | None = None,
    pacing: bool = True,
) -> list[str]:
    enc = enc or EncodeSettings()
    path = resolve_media_path(source.path, media_dir)
    args = []
    # seam_frames > 0 のときはフィルタ側（loop/aloop）でループするので、
    # 入力を開き直す -stream_loop は使わない
    if source.loop and not seam_frames:
        # -stream_loop は -i の前に置く必要がある
        args += ["-stream_loop", "-1"]
    return args + _pacing_input_args(source, enc, pacing) + ["-i", path]


def _live_input(
    source: SourceSpec, enc: EncodeSettings, pacing: bool = True
) -> list[str]:
    args: list[str] = []
    # ライブ端より手前から読み始めると、その差がそのまま素材の貯金になる。
    # 時計は drawtext（=この後ろ）で焼くので、測る遅延には乗らない
    if source.start_index:
        args += ["-live_start_index", str(-source.start_index)]
    # VOD の URL をライブとして読むと取れるだけ取ってしまい、送出先へ数十倍速で
    # 叩き込む。判定は sources.prepare_live_source() が済ませ、pace に入っている。
    # 実際の実時間絞りは realtime フィルタ側で行う
    args += _pacing_input_args(source, enc, pacing)
    if source.reconnect:
        # 注意: これらは HTTP(S) プロトコル固有のオプション。
        # HLS/DASH の取り込みには効くが、rtmp:// や srt:// の入力には効かない
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
    return args + ["-i", source.url]


def needs_pacing(source: SourceSpec, pacing: bool = True) -> bool:
    """実時間に絞る必要があるか。本物のライブ入力だけは絞らない。

    `pacing=False` は処理能力の計測用。絞ったまま測ると必ず 1.0 倍になり、
    どれだけ余裕があるのかが一切分からなくなる。
    """
    if not pacing:
        return False
    if source.type == "live_url":
        return source.pace != "asfast"
    return True


def _pacing_input_args(
    source: SourceSpec, enc: EncodeSettings, pacing: bool = True
) -> list[str]:
    """入力側でのペーシング指定。通常は空を返す。

    **`-re` / `-readrate` は使わず、`realtime` フィルタで絞る。**

    理由は実測。`-re` で映像と音声の両方をエンコードすると、処理能力が10倍
    余っているのに実時間の 0.81〜0.83 倍へ**間欠的に落ちる**。同じ条件でも
    1.00 に出る回と 0.81 に落ちる回があるバイモーダルな挙動で、0.83 は
    50fps 相当（スリープ粒度が 20ms に粗くなった形）。映像だけ・音声だけなら
    必ず 1.00 で、両方を符号化したときだけ起きる。

    `realtime` フィルタはフィルタグラフの中で待つため、この現象が出ない
    （同条件5回で 0.99〜1.01）。

    ただし `-c copy` のときはフィルタを通らないので、そこだけ `-re` を使う。
    """
    if enc.mode == "passthrough" and needs_pacing(source, pacing):
        return ["-re"]
    return []


def resolve_media_path(rel_path: str, media_dir: str) -> str:
    """media_dir の中に収まっていることを確認して絶対パスを返す。

    UI から渡された文字列をそのまま FFmpeg に渡すと、`../` で任意のファイルを
    配信できてしまう。ここで必ず閉じ込める。
    """
    if not rel_path:
        raise ValueError("ファイルが指定されていません")
    if not media_dir:
        raise ValueError("media_dir が設定されていません")

    base = Path(media_dir).resolve()
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError("media_dir の外は指定できません")
    if not target.exists():
        raise ValueError(f"ファイルが見つかりません: {rel_path}")
    return str(target)


def build_video_filters(
    source: SourceSpec, enc: EncodeSettings, seam_frames: int = 0, pacing: bool = True
) -> list[str]:
    """オーバーレイより前に掛ける映像フィルタ。

    testsrc は最初から目的の解像度・fps で生成するので、実時間絞り以外は要らない。
    file / live_url は素材側の解像度が分からないため、ここで揃える。
    """
    if source.type == "testsrc":
        return ["realtime"] if needs_pacing(source, pacing) else []

    filters = [
        # アスペクト比を保ったまま収め、余白は黒で埋める。
        # 引き伸ばして歪ませるより、素材の見た目を保つ方を選ぶ
        f"scale={enc.width}:{enc.height}:force_original_aspect_ratio=decrease",
        f"pad={enc.width}:{enc.height}:(ow-iw)/2:(oh-ih)/2:color=black",
        f"fps={enc.fps}",
    ]

    if seam_frames:
        # デコード済みフレームを抱えてループする。入力を開き直さないので、
        # 継ぎ目で出力が止まらない
        filters.append(f"loop=loop=-1:size={seam_frames}:start=0")

    if source.type == "file" and source.loop:
        # ループ境界で PTS が巻き戻るのを防ぐ。ここを外すと長時間試験で必ず崩れる。
        # fps= の後に置くことで、N は固定フレームレートの連番になる
        filters.append("setpts=N/FRAME_RATE/TB")

    if needs_pacing(source, pacing):
        # ここで実時間に絞る。**オーバーレイより前**に置くのが要点で、
        # 待ってから時計を焼くので、焼いた時刻と送出時刻がずれない
        filters.append("realtime")

    return filters


def build_audio_filters(source: SourceSpec, seam_samples: int = 0) -> list[str]:
    """オーバーレイに相当するものが無いので、音声はタイムスタンプの手当てだけ。

    **ここに `arealtime` を置いてはいけない。** 映像は `realtime`、音声は
    `arealtime` と別々の時計で絞ると、両者を結びつけるものが無くなる。
    実測（1920x1080@60 / libx264 ultrafast / 8Mbps、同一素材で複数回）では、
    映像側が一瞬詰まった拍子に音声だけが先へ進み、

      * `arealtime` あり … 映像のフレーム生成が **128秒間まるごと停止**
      * `arealtime` なし … 映像は止まらない

    という差が出た。止まっている間も音声は流れ続けるため、FFmpeg の out_time
    （進んでいる方のストリームを指す）は 1.00x のままで、異常が表に出ない。

    音声の絞りを外しても素材を先読みしすぎることはない。出力側の
    `-max_interleave_delta 0` が、映像が追いつくまで音声を書かせないため。
    """
    filters: list[str] = []
    if seam_samples:
        filters += [f"aloop=loop=-1:size={seam_samples}", "asetpts=N/SR/TB"]
    elif source.type == "file" and source.loop:
        # 映像と同じ理由。音声側のタイムスタンプも張り直す
        filters += ["aresample=async=1", "asetpts=N/SR/TB"]
    return filters


def build_filter_complex(
    video_filters: list[str], snapshot: "SnapshotSpec | None" = None
) -> str:
    """映像のフィルタグラフを `-filter_complex` の1文字列として組み立てる。

    `-vf` ではなくこちらを使うのは、**出口を2つに分けられる**ため。
    セルフプレビューはここで `split` した枝から作る。

    分岐はオーバーレイより**後ろ**に置く。そうすることでプレビューに写るのは
    「実際に焼き込まれた時計入りのフレーム」そのものになり、絵を見れば
    そのフレームの送出時刻まで読める。

    `split` は参照カウントを増やすだけでフレームを複製しないので、追加コストは
    間引いた後の `scale` と JPEG 化だけで済む（実測で全体の -2%、誤差の範囲）。
    """
    body = ",".join(video_filters) if video_filters else "null"
    if snapshot is None:
        return f"[0:v]{body}[{VIDEO_LABEL}]"
    return (
        f"[0:v]{body},split=2[{VIDEO_LABEL}][snapsrc];"
        f"[snapsrc]fps={snapshot.fps:g},scale={snapshot.width}:-2[{SNAPSHOT_LABEL}]"
    )


def build_snapshot_output(snapshot: "SnapshotSpec", duration_sec: int | None) -> list[str]:
    """セルフプレビュー（静止画）の出力部。

    踏んだ罠が2つあるので、どちらも引数で潰してある。

      * `-t` は**出力ごと**の指定。送出側にだけ付けると、この枝は終わらずに
        回り続ける。lavfi 入力だとプロセスが一生終わらない
      * `image2` は既存ファイルがあると `Overwrite? [y/N]` を聞いて止まる。
        `-nostdin` と組み合わさると即死するので、呼び出し側で `-y` を付ける

    `-atomic_writing 1` は一時ファイルに書いてから rename する指定。これが無いと
    `-update 1` は同じファイルを切り詰めて書き直すので、読み手が**書きかけの
    壊れた JPEG** を掴む。
    """
    args = [
        "-map", f"[{SNAPSHOT_LABEL}]",
        "-an",
        "-c:v", "mjpeg",
        "-q:v", str(SNAPSHOT_QUALITY),
        "-update", "1",
        "-atomic_writing", "1",
    ]
    if duration_sec and duration_sec > 0:
        args += ["-t", str(duration_sec)]
    return args + ["-f", "image2", snapshot.path]


def build_map_args(source: SourceSpec, video_label: str = "") -> list[str]:
    """どの入力ストリームを使うか。

    testsrc はトーンが別入力（1番目）。file / live_url は同じ入力の中に音声がある
    ので `0:a?` で拾う。`?` を付けるのは、音声トラックが無いファイルでも
    落ちないようにするため。

    `video_label` を渡した場合、映像は入力ではなく **フィルタグラフの出口**から
    受け取る（`-filter_complex` を使うとき）。音声側の扱いは変わらない。
    """
    video = ["-map", f"[{video_label}]"] if video_label else None
    if source.type == "testsrc":
        return (video or ["-map", "0:v"]) + (["-map", "1:a"] if source.with_tone else [])
    return (video or ["-map", "0:v:0"]) + ["-map", "0:a:0?"]


def build_encode_args(
    enc: EncodeSettings, source: SourceSpec, video_label: str = ""
) -> list[str]:
    has_audio = source.with_tone if source.type == "testsrc" else True

    if enc.mode == "passthrough":
        # 入力をそのまま流す。エンコードしないので解像度・ビットレート等は効かない
        return build_map_args(source) + ["-c", "copy"]

    gop_frames = max(1, round(enc.gop_sec * enc.fps))

    args = build_map_args(source, video_label)

    args += [
        "-c:v", enc.video_encoder,
        "-b:v", f"{enc.bitrate_kbps}k",
        "-maxrate", f"{enc.effective_maxrate}k",
        "-bufsize", f"{enc.effective_bufsize}k",
        "-g", str(gop_frames),
        "-pix_fmt", "yuv420p",
    ]

    if enc.video_encoder == "libx264":
        # preset / bframes / sc_threshold は libx264 固有。
        # VideoToolbox に渡すと落ちるので分岐している。
        args += [
            "-preset", enc.preset,
            "-bf", str(enc.bframes),
            "-sc_threshold", "0",   # GOP長を固定する（シーンチェンジでIを挿れない）
            "-profile:v", enc.profile,
        ]
        if enc.tune:
            args += ["-tune", enc.tune]
    else:
        args += [
            "-realtime", "1",
            # 入力に A53 クローズドキャプションが載っていると、VideoToolbox は
            # それを SEI として出力へ載せ直そうとして壊れる
            #   Unexpected end of SEI NAL Unit parsing type / Error copying packet data
            # 本ツールはエンコードの試験が目的でCCの再現は目的ではないため、無効にする。
            # libx264 は同じ入力でも問題なく通るので、そちらは既定のままにしている。
            "-a53cc", "0",
        ]

    if has_audio:
        args += [
            "-c:a", AUDIO_ENCODERS[enc.audio_codec],
            "-b:a", f"{enc.audio_bitrate_kbps}k",
            "-ar", str(enc.samplerate),
            "-ac", str(enc.channels),
        ]
    else:
        args += ["-an"]

    return args


def build_command(
    *,
    ffmpeg_path: str,
    protocol: str,
    target_url: str,
    source: SourceSpec,
    encode: EncodeSettings,
    duration_sec: int | None = None,
    overlay: OverlaySettings | None = None,
    font_path: str = "",
    job_label: str = "",
    media_dir: str = "",
    bearer_token: str = "",
    webrtc_delivery: bool = False,
    seam_frames: int = 0,
    snapshot: SnapshotSpec | None = None,
    pacing: bool = True,
) -> list[str]:
    """完全なコマンドを組み立てて返す。

    `snapshot` を渡すとセルフプレビュー用の静止画出力が1本増える。
    その場合 **送出用の出力を先に、プレビューを後に**並べる（理由は末尾のコメント）。

    プレビューを付けたコマンドは `benchmark.to_null_output()` に渡せない。
    あちらは「末尾3つが `-f <muxer> <url>`」を前提に出力を差し替えるため。
    計測には `snapshot=None` で組み直すこと。

    `pacing=False` は処理能力の計測用。実時間の絞り（`realtime` フィルタ / `-re`）を
    最初から入れずに組む。絞ったまま測ると必ず 1.0 倍になり、余裕が分からない。
    """
    muxer = MUXER_BY_PROTOCOL.get(protocol)
    if muxer is None:
        raise ValueError(f"プロトコル '{protocol}' の出力コンテナが未定義です")

    encode, _ = adapt_encode(encode, protocol, webrtc_delivery)
    # passthrough はフィルタを通らないので split できない。プレビューも作れない
    if encode.mode == "passthrough":
        snapshot = None
    # 音声側のループ長は、映像のフレーム数から秒に直して求める
    seam_samples = (
        int(seam_frames / encode.fps * encode.samplerate) if seam_frames and encode.fps else 0
    )

    argv = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",                 # 端末を持たないので標準入力を読ませない
        "-loglevel", "warning",
        "-progress", "pipe:1",      # 進捗は stdout に構造化して出す
        "-stats_period", "1",
    ]
    if snapshot is not None:
        # image2 の上書き確認で止まらないようにする（-nostdin と組むと即死する）
        argv += ["-y"]
    argv += build_input_args(source, encode, media_dir, seam_frames, pacing)

    video_label = ""
    if encode.mode != "passthrough":
        overlay_filters = []
        if overlay is not None and font_path:
            overlay_filters = build_filters(
                overlay,
                font_path=font_path,
                protocol=protocol,
                encode=encode,
                job_label=job_label,
            )

        vf = build_video_filters(source, encode, seam_frames, pacing) + overlay_filters
        # 1つの argv 要素として渡す。シェルを介さないのでクォートは不要
        argv += ["-filter_complex", build_filter_complex(vf, snapshot)]
        video_label = VIDEO_LABEL

        af = build_audio_filters(source, seam_samples)
        if af:
            argv += ["-af", ",".join(af)]

    argv += build_encode_args(encode, source, video_label)

    if duration_sec and duration_sec > 0:
        # タイムアウトは -t で ffmpeg 自身に終わらせる。外から殺すより終了が綺麗。
        argv += ["-t", str(duration_sec)]

    if protocol == "whip":
        # Bearer トークンは URL ではなくオプションで渡す（-authorization）
        if bearer_token:
            argv += ["-authorization", bearer_token]
        # ハンドシェイクの既定5秒は、遠いリージョン相手だと短い
        argv += ["-handshake_timeout", "10000"]

    # 片方のストリームだけを先に書き出させない。
    # 既定は10秒で、それを超えると FFmpeg は相手を待たずに書いてしまう。実測では
    # 映像が一度詰まったあと **117秒ぶんの音声を、映像を1枚も挟まずに**送出して
    # いた（出力の書き出し順で音声パケットが5484個連続）。受信側から見ると
    # 「2分間ずっと音声だけ来て映像が来ない」で、WebRTC 配信先ではここが崩れる。
    # 0 は「相手が追いつくまで待つ」。待つあいだ未書き出しのパケットを抱えるので、
    # 映像が完全に死ぬとメモリを食い続ける点だけは引き換えになる
    argv += ["-max_interleave_delta", "0"]

    argv += ["-f", muxer, target_url]

    # **セルフプレビューは必ず最後に置く。**
    # `-progress` が報告するのは「最初の出力」の数字（frame / fps / bitrate /
    # total_size）で、2番目以降は出てこない。先に置くと、監視も指摘も
    # 毎秒1枚のプレビュー枝を見てしまう。実際に順番を入れ替えて測ると
    #
    #   プレビューが先 … frame=3   fps=0.74  total_size=N/A
    #   プレビューが後 … frame=110 fps=27.23 total_size=524288
    #
    # となり、先に置いた場合は video_rate が 1/30 に見えるせいで
    # 「映像が出ていません」の誤報まで出る（実際に出た）。
    if snapshot is not None:
        argv += build_snapshot_output(snapshot, duration_sec)
    return argv


def to_shell(argv: list[str]) -> str:
    """UI表示・記録用の文字列。コピーしてそのままターミナルで実行できる。"""
    return " ".join(shlex.quote(a) for a in argv)
