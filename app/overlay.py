"""オーバーレイ（時計・エンコード設定の焼き込み）のフィルタ生成。

機能設計書 F-4。ここで組み立てた文字列は `-vf` に**1つの argv 要素として**渡す。
シェルを介さないので、考えるべきエスケープは FFmpeg の3層だけになる。

  1. filtergraph パーサ  … `:` `,` `[` `]` `;` が区切り。`\\` で無効化できる
  2. drawtext のオプション解析
  3. テキスト展開（`%{...}`）

`text=` の値を単一引用で囲めば 1 は概ね無害化できるが、`%{eif:...}` のように
展開側で `:` を使う場合は `\\:` と書いて 1 を通り抜けさせる必要がある。
このモジュールの中に閉じ込めて、外からはこの面倒さが見えないようにしている。
"""

from __future__ import annotations

from .models import EncodeSettings, OverlaySettings

# drawtext の text= 値として安全にするための置換
_TEXT_ESCAPES = (
    ("\\", r"\\"),
    ("'", r"\'"),
    (":", r"\:"),
    ("%", r"\%"),
)

# 位置 → (x, y) の式。tw/th は描画するテキストの幅・高さ
_POSITIONS = {
    "top-left":      ("m",              "m"),
    "top-center":    ("(w-tw)/2",       "m"),
    "top-right":     ("w-tw-m",         "m"),
    "bottom-left":   ("m",              "h-th-m"),
    "bottom-center": ("(w-tw)/2",       "h-th-m"),
    "bottom-right":  ("w-tw-m",         "h-th-m"),
}

# 壁時計。`%{localtime}` の既定書式が "YYYY-MM-DD HH:MM:SS" なので、そのまま使う。
# ミリ秒は t（フレームのタイムスタンプ）の小数部から作る。
WALLCLOCK = r"%{localtime}.%{eif\:trunc(mod(t\,1)*1000)\:d\:3}"

# フレーム番号。同じエンコードなら完全に一致するので、厳密な比較はこちらを使う
FRAMECOUNT = r"F\:%{eif\:n\:d\:10}"


def escape_text(value: str) -> str:
    """静的な文字列を drawtext の text= に安全に埋め込む。

    `%` も潰すので、この関数を通した文字列の中で `%{...}` は展開されない。
    展開したい式は WALLCLOCK / FRAMECOUNT のように生のまま使う。
    """
    for src, dst in _TEXT_ESCAPES:
        value = value.replace(src, dst)
    return value


def build_info_text(
    items: list[str],
    *,
    protocol: str,
    encode: EncodeSettings,
    job_label: str = "",
) -> str:
    """焼き込む設定情報の文字列を、実際の設定値から組み立てる。

    手入力させないのが要点。表示と実際の設定が食い違う余地をなくす。
    """
    parts: list[str] = []
    for item in items:
        if item == "protocol":
            parts.append(protocol.upper())
        elif item == "resolution":
            parts.append(f"{encode.width}x{encode.height}@{encode.fps}")
        elif item == "codec":
            parts.append("H.264" + ("(VT)" if encode.video_encoder.endswith("videotoolbox") else ""))
        elif item == "bitrate":
            parts.append(f"{encode.bitrate_kbps}k")
        elif item == "gop":
            parts.append(f"GOP {encode.gop_sec:g}s")
        elif item == "preset":
            parts.append(encode.preset)
        elif item == "job" and job_label:
            parts.append(job_label)
    return " | ".join(parts)


def _drawtext(
    *,
    font_path: str,
    text: str,
    position: str,
    scale: float,
    line_offset: int = 0,
) -> str:
    x, y = _POSITIONS[position]
    fontsize = f"h/{scale:g}"
    margin = f"h/{scale * 2:g}"          # 余白はフォントサイズの半分
    x = x.replace("m", margin)
    y = y.replace("m", margin)

    if line_offset:
        # 2行目は1行分ずらす。上寄せなら下へ、下寄せなら上へ
        shift = f"{line_offset} * (h/{scale:g} * 1.4)"
        y = f"{y} - ({shift})" if position.startswith("bottom") else f"{y} + ({shift})"

    return (
        f"drawtext=fontfile={font_path}"
        f":text='{text}'"
        f":x={x}:y={y}"
        f":fontsize={fontsize}"
        ":fontcolor=white"
        ":box=1:boxcolor=black@0.6:boxborderw=8"
    )


def build_filters(
    overlay: OverlaySettings,
    *,
    font_path: str,
    protocol: str,
    encode: EncodeSettings,
    job_label: str = "",
) -> list[str]:
    """drawtext フィルタの列を返す。空リストならオーバーレイなし。"""
    if not overlay.enabled():
        return []

    filters: list[str] = []

    if overlay.clock_enabled:
        lines: list[str] = []
        if overlay.clock_mode in ("wallclock", "both"):
            lines.append(WALLCLOCK)
        if overlay.clock_mode in ("framecount", "both"):
            lines.append(FRAMECOUNT)
        for i, line in enumerate(lines):
            filters.append(
                _drawtext(
                    font_path=font_path,
                    text=line,
                    position=overlay.clock_position,
                    scale=overlay.clock_scale,
                    line_offset=i,
                )
            )

    if overlay.info_enabled:
        info = build_info_text(
            overlay.info_items, protocol=protocol, encode=encode, job_label=job_label
        )
        if info:
            filters.append(
                _drawtext(
                    font_path=font_path,
                    text=escape_text(info),
                    position=overlay.info_position,
                    scale=overlay.info_scale,
                )
            )

    return filters
