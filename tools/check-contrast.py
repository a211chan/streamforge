#!/usr/bin/env python3
"""theme.css の配色が、全シーンで読める／見分けられるかを確かめる。

見るのは3つ。
  1. コントラスト比  本文 4.5:1、大きい文字・UI部品・境界・図 3:1
  2. 塗りの上の文字  --on-* が対応する塗り色に対し 4.5:1
  3. 色覚の識別      --chart-1〜5 の全ペアが、通常・P型・D型のいずれでも
                     CIE Lab の ΔE76 で 20 以上離れていること

3つめが要る理由：2色が「別の色」であることは、コントラスト比では分からない。
青と紫は比が同じでも、P型では同じ色に潰れる。実際に潰れた組み合わせを
この検証で見つけている。

トークンが1つも無いシーンは「未調色」として報告し、成功にも失敗にも数えない
（空のブロックが黙って通ると、調色前のシーンに ✅ が付いてしまう）。

    python3 tools/check-contrast.py        # 一覧
    python3 tools/check-contrast.py -q     # 失敗のときだけ出す
"""

from __future__ import annotations

import itertools
import math
import re
import sys
from pathlib import Path

DEFAULT_CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "theme.css"


def css_path() -> Path:
    """既定は app/static/theme.css。引数で別ファイルも見られる（調色中の下書き用）。"""
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    return Path(args[0]) if args else DEFAULT_CSS

TEXT_MIN = 4.5    # 本文
UI_MIN = 3.0      # 大きい文字・UI部品・境界・図
DELTA_E_MIN = 25.0  # 色覚シミュレーション下の系列色どうしの隔たり
GRAY_MIN = 8.0      # 白黒印刷したときの明度差（CIE Lab の L*）

# シーン別の下限。全シーン共通の値では表現が成り立たないときだけ、
# 理由を添えてここに書く。書いた分は資料の「下限を変えた記録」にも残す。
# 下げるときは、そのぶん別の手段で識別を担保すること（typewriter は線種の併用）。
SCENE_DELTA_E_MIN: dict[str, float] = {}
SCENE_GRAY_MIN = {
    "typewriter": 10.0,   # 色相を諦めるぶん、白黒の明度差を厚くする
}

# ΔE を判定しないシーン。
# 彩度を持たない設計では系列色の色相成分がほぼゼロになり、ΔE76 の定義から
# ΔE ≥ |ΔL*| が常に成り立つので、ΔL* に下限を課した時点で ΔE の下限は
# 自動的に満たされる。実測でも ΔL* 10.01 に対して ΔE は 9.97〜10.23 で、
# ΔE は ΔL* の別名でしかない。発火しない下限を置いて「合格」と出すと
# 合格印の意味が薄まるので、値は参考として出し、判定はしない。
# 識別は ΔL* 10 と線種の併用で担保する。
SCENE_DELTA_E_EXEMPT = {"typewriter"}


def delta_e_judged(scene: str) -> bool:
    return scene not in SCENE_DELTA_E_EXEMPT


def delta_e_floor(scene: str) -> float:
    return SCENE_DELTA_E_MIN.get(scene, DELTA_E_MIN)


def gray_floor(scene: str) -> float:
    return SCENE_GRAY_MIN.get(scene, GRAY_MIN)

SCENES = ("light", "dark", "terminal", "tactical", "typewriter")
VISIONS = ("normal", "protan", "deutan")

# (前景, 背景, 下限, 説明)
PAIRS = [
    ("text", "bg", TEXT_MIN, "本文 / ページ地"),
    ("text", "surface", TEXT_MIN, "本文 / カード"),
    ("text", "surface-well", TEXT_MIN, "本文 / 沈めた面"),
    ("text-muted", "bg", TEXT_MIN, "補助文字 / ページ地"),
    ("text-muted", "surface", TEXT_MIN, "補助文字 / カード"),
    ("text-muted", "surface-well", TEXT_MIN, "補助文字 / 沈めた面"),
    ("border-strong", "bg", UI_MIN, "意味のある境界 / ページ地"),
    ("border-strong", "surface", UI_MIN, "意味のある境界 / カード"),
    ("accent", "surface", UI_MIN, "アクセント / カード"),
    ("success", "surface", UI_MIN, "正常 / カード"),
    ("warning", "surface", UI_MIN, "注意 / カード"),
    ("danger", "surface", UI_MIN, "失敗 / カード"),
    ("focus-ring", "bg", UI_MIN, "フォーカス / ページ地"),
    ("focus-ring", "surface-well", UI_MIN, "フォーカス / 沈めた面"),
    ("selection-text", "selection-bg", TEXT_MIN, "選択文字 / 選択地"),
    ("on-accent", "accent", TEXT_MIN, "塗りの上の文字 / アクセント"),
    # ホバーは塗り色が変わる。ここを見ていないと、押せる状態のときだけ
    # 文字が読めない配色を見逃す
    ("on-accent", "accent-hover", TEXT_MIN, "塗りの上の文字 / アクセント（ホバー）"),
    ("accent-hover", "surface", UI_MIN, "アクセント（ホバー） / カード"),
    ("on-success", "success", TEXT_MIN, "塗りの上の文字 / 正常"),
    ("on-warning", "warning", TEXT_MIN, "塗りの上の文字 / 注意"),
    ("on-danger", "danger", TEXT_MIN, "塗りの上の文字 / 失敗"),
]
# 判定はしないが数値は出す組み合わせ。
# --focus-ring は地とアクセントに挟まれるので、両方から 3:1 離れる色が
# どのシーンにも存在しない。outline-offset を正にしてリングを地の上へ
# 浮かせる運用が前提で、その前提のもとでは「対 地」だけが効く。
# 前提が崩れたときに効いてくる数字なので、参考として必ず出す
INFO_PAIRS = [
    ("focus-ring", "accent", "フォーカス / アクセント（塗りに接した場合）"),
    ("focus-ring", "accent-hover", "フォーカス / アクセント・ホバー（同上）"),
]

# 系列色は5本。何本必要かは StreamForge 側の仕様であって、
# 探索が満たせないからといって減らすものではない
CHARTS = [f"chart-{i}" for i in range(1, 6)]
# 図の色は地と沈めた面の両方に置かれる
CHART_BACKDROPS = ("bg", "surface-well")

# --text-disabled は「操作できない」ことを示す色なので下限の対象にしない。
# ただし定義漏れは検出したいので、存在だけは確かめる
MUST_EXIST = [
    "bg", "surface", "surface-well", "text", "text-muted", "text-disabled",
    "selection-bg", "selection-text", "border", "border-strong",
    "accent", "accent-hover", "on-accent", "success", "on-success",
    "warning", "on-warning", "danger", "on-danger", "focus-ring",
    *CHARTS,
    "font-ui", "font-mono", "font-display",
    "radius", "border-width", "glow-color", "shadow", "shadow-inset",
]


# ------------------------------------------------------------------ 読み取り

def parse(text: str) -> dict[str, dict[str, str]]:
    """シーン名 → トークンの辞書。:root は light として扱う。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    scenes: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        selector = " ".join(selector.split())
        tokens = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", body))
        if not tokens:
            continue
        name = None
        if selector == ":root":
            name = "light"
        else:
            m = re.fullmatch(r'\[data-scene="([a-z]+)"\]', selector)
            if m:
                name = m.group(1)
        if name:
            scenes.setdefault(name, {}).update({k: v.strip() for k, v in tokens.items()})
    # :root は全シーンの土台。上書きしていないトークンは light の値が効く
    base = dict(scenes.get("light", {}))
    for name, tokens in scenes.items():
        if name != "light":
            scenes[name] = {**base, **tokens}
    return {name: resolve_vars(tokens) for name, tokens in scenes.items()}


def resolve_vars(tokens: dict[str, str]) -> dict[str, str]:
    """`--success: var(--accent)` のような参照を実際の値に置き換える。

    「他のトークンと同じ」であることを宣言として書けるようにしてあるので、
    検証側もそれを辿れないと、同一だと分かっているものを読めないと言って落ちる。
    """
    out = dict(tokens)
    for _ in range(8):
        changed = False
        for key, value in list(out.items()):
            m = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value.strip())
            if m and m.group(1) in out and out[m.group(1)] != value:
                out[key] = out[m.group(1)]
                changed = True
        if not changed:
            break
    return out


def declared(text: str) -> dict[str, int]:
    """そのシーンのブロックが自前で持っているトークン数（継承ぶんを除く）。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    out = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        selector = " ".join(selector.split())
        name = "light" if selector == ":root" else None
        m = re.fullmatch(r'\[data-scene="([a-z]+)"\]', selector)
        if m:
            name = m.group(1)
        if name:
            out[name] = out.get(name, 0) + len(re.findall(r"--([a-z0-9-]+)\s*:", body))
    return out


# --------------------------------------------------------------------- 色計算

def channels(value: str) -> tuple[float, float, float]:
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ValueError(f"色として読めません: {value!r}")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def to_linear(value: str) -> tuple[float, float, float]:
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                 for c in channels(value))


def luminance(value: str) -> float:
    r, g, b = to_linear(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: str, bg: str) -> float:
    lo, hi = sorted((luminance(fg), luminance(bg)))
    return (hi + 0.05) / (lo + 0.05)


# Viénot, Brettel & Mollon (1999) の二色覚シミュレーション。
# 線形sRGB に直接かける行列で、論文どおりの係数。
#
# 以前ここに LMS 空間を経由する実装を置いていたが、順変換と逆変換で
# 出所の違う行列を混ぜていたため、変換後の R と G が全色で一致していた
# （＝ 色相の情報が丸ごと落ちた状態で「識別できている」と判定していた）。
# 下の SELF_TEST がその状態を検出する。
VIENOT = {
    "protan": ((0.11238, 0.88762, 0.0),
               (0.07276, 0.92724, 0.0),
               (0.0,     0.0,     1.0)),
    "deutan": ((0.29275, 0.70725, 0.0),
               (0.30268, 0.69732, 0.0),
               (0.0,     0.0,     1.0)),
}


def to_hex(linear) -> str:
    def gamma(v: float) -> int:
        v = max(0.0, min(1.0, v))
        v = v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
        return round(v * 255)
    return "#%02x%02x%02x" % tuple(gamma(v) for v in linear)


def simulate(value: str, vision: str) -> str:
    """二色覚の見えをシミュレートする。

    sRGB のガンマを外して線形にし、行列をかけ、再エンコードして返す。
    ガンマを外さずにかけると暗部の挙動がずれる。
    """
    if vision == "normal":
        return value
    try:
        matrix = VIENOT[vision]
    except KeyError:
        raise ValueError(f"未知の色覚: {vision}") from None
    r, g, b = to_linear(value)
    return to_hex(tuple(m[0] * r + m[1] * g + m[2] * b for m in matrix))


def self_test() -> list[str]:
    """シミュレーションが壊れていないかを確かめる。

    過去に「変換後の R と G が全色で一致する」実装を出荷している。
    その状態では色相が完全に落ちるため、どんな配色でも識別できているように
    見えてしまう。検証の土台が黙って壊れるのがいちばん危ないので、
    毎回ここを通す。
    """
    problems: list[str] = []

    # ゴールデン値。正典 Viénot 1999 なら固定の出力になる。
    # 不変条件（R≠G・青が動かない・無彩色が動かない）だけでは、
    # P型とD型の行列を取り違えた実装がすり抜ける。純赤はP型とD型で
    # 大きく違うので、取り違えはここで必ず落ちる
    for source, want_p, want_d in (
        ("#ff0000", "#5e4c00", "#939500"),
        ("#00ff00", "#f2f700", "#dbd900"),
        ("#0000ff", "#0000ff", "#0000ff"),
        ("#808080", "#808080", "#808080"),
    ):
        for vision, want in (("protan", want_p), ("deutan", want_d)):
            got = simulate(source, vision)
            if got != want:
                problems.append(f"{vision}: {source} → {got}（正典は {want}）")

    probe = ["#067949", "#f1411e", "#06516b", "#4f0882", "#3bf58a",
             "#fe7a06", "#c25bdc", "#0364a0", "#d24904", "#03a07b"]
    for vision in ("protan", "deutan"):
        results = [simulate(c, vision) for c in probe]
        same = [h for h in results if h[1:3] == h[3:5]]
        if len(same) == len(results):
            problems.append(
                f"{vision}: 変換後の R と G が全色で一致している"
                f"（色相が落ちた実装。{results[0]} など）")
        # 青チャンネルは二色覚でも保たれる
        for src, out in zip(probe, results):
            if to_linear(src)[2] != to_linear(out)[2]:
                problems.append(f"{vision}: {src} の青チャンネルが変わっている → {out}")
                break
    # 灰色は二色覚でも灰色のまま
    for vision in ("protan", "deutan"):
        grey = simulate("#808080", vision)
        if delta_e(grey, "#808080") > 1.0:
            problems.append(f"{vision}: 無彩色が動いている（#808080 → {grey}）")
    return problems


def lab(value: str) -> tuple[float, float, float]:
    r, g, b = to_linear(value)
    X = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    Y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    Z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29
    fx, fy, fz = f(X / 0.95047), f(Y / 1.0), f(Z / 1.08883)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    return math.dist(lab(a), lab(b))


# ------------------------------------------------------------------- 検証本体

def check_scene(name: str, tokens: dict[str, str]) -> tuple[list[str], list[str]]:
    failures, lines = [], []

    missing = [t for t in MUST_EXIST if t not in tokens]
    if missing:
        failures.append(f"{name}: 未定義のトークン {', '.join(missing)}")

    for fg, bg, floor, label in PAIRS:
        if fg not in tokens or bg not in tokens:
            continue
        try:
            r = ratio(tokens[fg], tokens[bg])
        except ValueError as exc:
            failures.append(f"{name} {fg}: {exc}")
            continue
        ok = r >= floor
        if not ok:
            failures.append(f"{name} {label}: {r:.2f}:1（下限 {floor}）")
        lines.append(f"  {'✓' if ok else '✗'} {r:6.2f}:1  {label}")

    for fg, bg, label in INFO_PAIRS:
        if fg in tokens and bg in tokens:
            r = ratio(tokens[fg], tokens[bg])
            lines.append(f"  · {r:6.2f}:1  {label}（参考・判定対象外）")

    for backdrop in CHART_BACKDROPS:
        if backdrop not in tokens:
            continue
        for key in CHARTS:
            if key not in tokens:
                continue
            r = ratio(tokens[key], tokens[backdrop])
            ok = r >= UI_MIN
            if not ok:
                failures.append(f"{name} {key} / {backdrop}: {r:.2f}:1（下限 {UI_MIN}）")
            lines.append(f"  {'✓' if ok else '✗'} {r:6.2f}:1  {key} / {backdrop}")

    if all(k in tokens for k in CHARTS):
        # 白黒印刷では色相が落ちて明度だけが残る。ΔE が足りていても、
        # 明度が並んでいれば紙の上では同じ線になる
        gray_worst = min(
            (abs(lab(tokens[a])[0] - lab(tokens[b])[0]), a, b)
            for a, b in itertools.combinations(CHARTS, 2))
        g_min = gray_floor(name)
        if gray_worst[0] < g_min:
            failures.append(
                f"{name} {gray_worst[1]}×{gray_worst[2]}（白黒）: "
                f"ΔL* {gray_worst[0]:.1f}（下限 {g_min}）")
        lines.append(
            f"  {'✓' if gray_worst[0] >= g_min else '✗'} ΔL*{gray_worst[0]:6.1f}  "
            f"白黒にしたときの最小の明度差（{gray_worst[1]}×{gray_worst[2]}）")

        e_min = delta_e_floor(name)
        judged = delta_e_judged(name)
        worst = {}
        for vision in VISIONS:
            sim = {k: simulate(tokens[k], vision) for k in CHARTS}
            for a, b in itertools.combinations(CHARTS, 2):
                d = delta_e(sim[a], sim[b])
                if judged and d < e_min:
                    failures.append(
                        f"{name} {a}×{b}（{vision}）: ΔE76 {d:.1f}（下限 {e_min}）")
                if vision not in worst or d < worst[vision][0]:
                    worst[vision] = (d, a, b)
        for vision, (d, a, b) in worst.items():
            if not judged:
                lines.append(
                    f"  · ΔE {d:6.1f}  系列色の最小間隔（{vision}・{a}×{b}）"
                    f"（参考・判定対象外）")
                continue
            ok = d >= e_min
            lines.append(f"  {'✓' if ok else '✗'} ΔE {d:6.1f}  系列色の最小間隔（{vision}・{a}×{b}）")

    return failures, lines


def main() -> int:
    quiet = "-q" in sys.argv

    # 判定の土台が壊れていないかを先に見る。ここが壊れると、
    # 配色の良し悪し以前に全部の数字が嘘になる
    broken = self_test()
    if broken:
        print("色覚シミュレーションの自己診断に失敗しました：")
        for b in broken:
            print(f"  - {b}")
        return 2

    text = css_path().read_text(encoding="utf-8")
    scenes = parse(text)
    own = declared(text)

    failures, out, done, todo = [], [], [], []
    for name in SCENES:
        if own.get(name, 0) == 0 and name != "light":
            todo.append(name)
            out.append(f"\n{name}  — 未調色（トークンが1つも無いので検証しない）")
            continue
        if name not in scenes:
            failures.append(f"{name}: ブロックがありません")
            continue
        scene_failures, lines = check_scene(name, scenes[name])
        failures += scene_failures
        (todo if scene_failures else done).append(name)
        out.append(f"\n{name}  {'✗ 不合格' if scene_failures else '✓ 合格'}")
        out += lines

    if not quiet or failures:
        print("\n".join(out))
    if failures:
        print(f"\n{len(failures)} 件が基準に届きません：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n合格: {', '.join(done) or 'なし'}")
    if todo:
        print(f"未調色（検証していない）: {', '.join(todo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
