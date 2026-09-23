#!/usr/bin/env python3
"""系列色（--chart-1〜5）を、シーンの世界観を壊さずに探す。

手で色を選ぶと、二色覚や白黒での識別が必ずどこかで破綻する。逆に数値だけで
最適化すると、ターミナルに鮮やかなマゼンタが出てくるような、世界観を壊す解に
着地する。どちらも起きないよう、制約を2種類に分けてこのファイルに書いておく。

  守るもの（SCENES[*]["hues"] / ["deviations"]）
      そのシーンで許す色相の家族と、そこから外れてよい色数。
      terminal は「緑・シアン・琥珀・管白、逸脱は1色まで」。

  目指すもの（TARGET_*）
      下限ギリギリに着地させない。下限は check-contrast.py、
      ここはそれより厳しい目標値を使って探す。

    python3 tools/palette-search.py terminal
    python3 tools/palette-search.py --all
"""

from __future__ import annotations

import colorsys
import importlib.util
import itertools
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("checker", HERE / "check-contrast.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

# --- 目指す水準（下限ではない） --------------------------------------------
# 下限は check-contrast.py の DELTA_E_MIN / GRAY_MIN / UI_MIN。
# 下限すれすれに置くと、色覚モデルを Viénot から Machado に替えただけで
# 不合格に転ぶ。余裕を持たせるためここは一段厳しくする
# 探索は「下限をどれだけ上回っているか」の最小値を最大化する。
# ある指標だけ余らせて他を下限すれすれに置く配分を避けるため。
# 例：ΔE が 50 でも ΔL* が 8.1 なら、その配色の余裕は 8.1/8 = 1.01 と数える
# 下限の何倍を「余裕あり」とみなすか。theme-review.py が印を付ける基準
# （MARGIN 未満に印）と同じ値にしてある。レポートが警告する配色を
# 探索が通してしまうと、2つのツールで基準が食い違う
MARGIN = 1.20

# 彩度の上限。ΔE を稼ぐために彩度へ逃げるのを止める。
# HSV（デザインツールの HSB）で測る。HSL で測ると同じ色が 10 ポイントほど
# 低く出て、上限をかけたつもりが効いていない状態になる（実際に一度そうなった）
SATURATION_MAX = 0.85
# 白黒での明度差はシーンごとに上限が違う。使える L* の幅を
# 「対比の下限」と「地の明るさ」が決めてしまうため（下の TARGET_GRAY 参照）
DEFAULT_TARGET_GRAY = 12.0

# --- シーンごとの世界観の制約 ------------------------------------------------
# hues: (名前, 色相の下限, 色相の上限, 彩度の下限, 彩度の上限, L*の下限, L*の上限)
#       L* の帯を決めておくと、最適化が極端な明度へ逃げず、色が色として残る
# deviations: hues から外してよい色数。0 なら家族の中だけで組む
SCENES = {
    "terminal": {
        "bg": "#060c0a", "well": "#0f1a17",
        "fixed": {0: "#3bf58a"},           # chart-1 は --accent と同じ燐光の緑（L* 86）
        # 管面で使える L* は 46〜86。下は対比 3.5:1、上は chart-1 が占める。
        # 40 しか幅がないので、5色を 12 ずつ離すこと（48 必要）は原理的に不可能。
        # このシーンだけ目標を 10 に下げている
        "target_gray": 8.0,
        # 当初の指定は「緑・シアン・琥珀・管白、逸脱は1色まで」だったが、
        # この組み合わせでは ΔE76 25 を満たす解が存在しない（探索で確認済み）。
        # 効いているのは管白＝無彩色で、二色覚下では他の系列も彩度を失うため、
        # 無彩色だけが「色の無い線」として分離できない。
        # そこで管白を家族から外し、紫紅を家族に入れ、逸脱枠は彩度 0.35 以下の
        # 1色（くすんだ煉瓦色）に限った。燐光の主役（緑・シアン・琥珀）は残る
        "hues": [
            ("シアン", 170, 200, 0.25, 1.0, 50, 84),
            ("琥珀",    28,  55, 0.35, 1.0, 40, 76),
            ("紫紅",   288, 330, 0.25, 0.65, 34, 66),
        ],
        "deviations": 1,
        "deviation_sat_max": 0.35,          # 逸脱する1色は彩度を抑える。
                                            # 鮮やかな色が混じると燐光に見えない
        "deviation_l_range": (34, 78),
        "luma_max": None,
    },
    "typewriter": {
        "bg": "#f3eee1", "well": "#eae2d0",
        "fixed": {},
        # 原稿用紙とインク。赤インクの濃淡と無彩色（墨・灰）だけを使い、
        # 彩度は 40% 以下に切る（「彩度の高い色は一切使わない」は
        # このシーンの世界観そのもので、外すと5シーンを作り分けた意味が消える）。
        # 色相で分けられないので、系列の識別は線種・マーカーの併用が前提。
        # 下限はこのシーンだけ ΔE 18 / ΔL* 10
        # （check-contrast.py の SCENE_DELTA_E_MIN / SCENE_GRAY_MIN）。
        #
        # ただし ΔE 18 はこの色相の範囲では到達できない。総当たりで測った上限は
        # minΔE 9.97（ΔL* 10.01・対比 3.00、いずれも上限に張り付き）。
        # 彩度 40% という制限は効いていない（色相を自由にすれば彩度 40% のまま
        # ΔE 33.7 まで伸びる）。効いているのは色相が赤〜セピアの一方向しかない
        # ことと、生成りの紙に対比 3:1 を保つと L* が 14〜56 の 42 しか使えず、
        # 4つの間隔に割ると ΔL* 10.5 が上限になること。
        # 結果として ΔE ≒ ΔL* になり、どちらも 10 前後で頭打ちになる。
        "target_gray": 10.0,
        "hues": [
            ("赤インク濃", 355, 15, 0.22, 0.40, 14, 56),
            ("赤インク淡", 355, 15, 0.08, 0.22, 14, 56),
            ("セピア",      22, 45, 0.12, 0.40, 14, 56),
            ("墨",          22, 45, 0.00, 0.07, 14, 56),
            ("灰",         200, 230, 0.00, 0.07, 14, 56),
        ],
        "deviations": 0,
        "deviation_sat_max": 0.45,
        "deviation_l_range": (18, 62),
        "luma_max": None,
    },
    "tactical": {
        "bg": "#0c0c0b", "well": "#191712",
        "fixed": {},
        # 軍用装備の色。艶を出さないので彩度は抑えめに切る。
        # レーダーの燐光（緑）とハイビズのアンバーが主役で、
        # 残りは青灰・赤錆・砂（低彩度）で散らす
        "target_gray": 9.6,
        "hues": [
            ("レーダー緑", 75, 110, 0.30, 0.75, 44, 92),
            ("アンバー",   35,  55, 0.35, 0.75, 44, 92),
            ("青灰",      190, 225, 0.25, 0.70, 44, 92),
            ("赤錆",        0,  18, 0.30, 0.70, 44, 92),
            ("砂",         30,  45, 0.08, 0.28, 44, 92),
        ],
        "deviations": 0,
        "deviation_sat_max": 0.5,
        "deviation_l_range": (44, 92),
        "luma_max": None,
    },
    "dark": {
        "bg": "#17191c", "well": "#121417",
        "fixed": {},
        # 標準ダーク。世界観を持たない中立配色なので、色相は素直に5方向へ散らす。
        # 暗い地なので使える L* が広く（余裕 1.20 倍でも 46〜96）、
        # light のように明度の幅で詰まることがない
        "target_gray": 10.0,
        "hues": [
            # 上限を 96 より下げると解が無くなる。5系列を 10 ずつ離すには
            # このシーンでも幅が要り、いちばん明るい1本は白に寄る
            ("青",     205, 235, 0.35, 0.85, 46, 96),
            ("緑",     140, 175, 0.35, 0.85, 46, 96),
            ("琥珀",    30,  55, 0.40, 0.85, 46, 96),
            ("紫",     275, 315, 0.30, 0.85, 46, 96),
            ("朱",       0,  20, 0.35, 0.85, 46, 96),
        ],
        "deviations": 0,
        "deviation_sat_max": 0.6,
        "deviation_l_range": (0, 100),
        "luma_max": None,
    },
    "light": {
        "bg": "#f7f8f9", "well": "#ffffff",
        "fixed": {},
        # 白地で対比 3.5:1 を満たす上限が L* 約 57。5色を 12 ずつ離すと
        # 57/45/33/21/9 の階段になり、いちばん暗い系列は紙の上でほぼ黒になる。
        # 線グラフ前提なら問題ないが、面・積み上げで使うなら見直しが要る
        # 使える L* は 25〜61。
        #   上限 61 … 白（--surface-well）に対して 3:1 を満たす限界。
        #             これより明るい系列は作れない（L* 62 で 2.97:1）
        #   下限 25 … 本文 #1d2126（L* 19）より暗い系列は、線ではなく
        #             罫線に見えるため
        # 幅 36 に5色なので、白黒の明度差は 8 が実質の上限になる
        "target_gray": 8.0,
        "hues": [
            ("緑",     140, 175, 0.30, 0.85, 25, 61),
            ("朱",      10,  40, 0.40, 0.85, 25, 61),
            ("青",     195, 235, 0.30, 0.85, 25, 61),
            ("紫",     275, 320, 0.25, 0.85, 25, 61),
            ("茶",     340, 360, 0.25, 0.85, 25, 61),
        ],
        "deviations": 0,
        "deviation_sat_max": 0.6,
        "deviation_l_range": (0, 100),
        "luma_max": None,
    },
}

_LAB: dict[str, list[tuple[float, float, float]]] = {}


def labs(color: str):
    if color not in _LAB:
        _LAB[color] = [C.lab(C.simulate(color, v)) for v in C.VISIONS]
    return _LAB[color]


def separation(a: str, b: str) -> float:
    la, lb = labs(a), labs(b)
    return min(math.dist(la[i], lb[i]) for i in range(3))


def gray_gap(a: str, b: str) -> float:
    return abs(labs(a)[0][0] - labs(b)[0][0])


def target_gray(spec: dict) -> float:
    return spec.get("target_gray", DEFAULT_TARGET_GRAY)


def candidates(spec: dict, hue_lo: int, hue_hi: int, sat_lo: float, sat_hi: float,
               l_lo: float = 0.0, l_hi: float = 100.0, step: float = 1.0) -> list[str]:
    """明度をきざんで、各段でいちばん彩度の高いものを代表にする。"""
    best: dict[int, tuple[str, float]] = {}
    lo, hi = (hue_lo, hue_hi) if hue_lo <= hue_hi else (hue_lo, hue_hi + 360)
    top = min(sat_hi, SATURATION_MAX)
    for h in range(lo, hi + 1, 3):
        # HSV で刻む。上限をかけるのと同じ空間で候補を作らないと、
        # 生成時と検査時で彩度の値がずれる
        for s in [x / 100 for x in range(int(sat_lo * 100), int(top * 100) + 1, 3)]:
            for v in [x / 100 for x in range(8, 101, 1)]:
                r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, s, v)
                hexv = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
                if saturation(hexv) > SATURATION_MAX + 0.005:
                    continue
                if C.ratio(hexv, spec["bg"]) < floors()["contrast"]:
                    continue
                if C.ratio(hexv, spec["well"]) < floors()["contrast"]:
                    continue
                if spec["luma_max"] and C.ratio(hexv, spec["bg"]) > spec["luma_max"]:
                    continue
                lightness = labs(hexv)[0][0]
                if not (l_lo <= lightness <= l_hi):
                    continue
                # 明度だけで代表を選ぶと、同じ明度の色は彩度がいちばん高い1つに
                # 潰れて色相が消える。家族が色相の帯を持つ以上、帯の中の色相差も
                # 残さないと組み合わせが見つからない（実際に、手で確かめた
                # 成立する配色を探索が見つけられなくなっていた）
                key = (round(lightness / step), round(h / 12))
                if key not in best or s > best[key][1]:
                    best[key] = (hexv, s)
    out = [v[0] for v in best.values()]
    # 候補が増えすぎると総当たりが終わらない。明度順に並べて間引き、
    # 帯の端から端までが残るように均等に取る
    cap = 90
    if len(out) > cap:
        out.sort(key=lambda c: labs(c)[0][0])
        stride = len(out) / cap
        out = [out[min(len(out) - 1, int(i * stride))] for i in range(cap)]
    return out


def saturation(hex_value: str) -> float:
    """HSV（HSB）の彩度。デザインツールが表示する値と同じ定義。"""
    h = hex_value.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)[1]


def floors() -> dict[str, float]:
    """足切り。

    コントラストは下限 × MARGIN。レポートが「下限すれすれ」と印を付ける
    ものを探索が通さないようにするため。
    ΔE と白黒の明度差は下限そのもの。ここにも MARGIN をかけると、
    明度の幅が足りなくなって解が消えるシーンが出る（light がそれ）。
    余裕は目的関数側（balance）で稼ぐ。
    """
    return {
        "delta_e": C.DELTA_E_MIN * MARGIN,
        "gray": C.GRAY_MIN * MARGIN,
        "contrast": C.UI_MIN * MARGIN,
    }


def margins(colors: list[str], spec: dict) -> dict[str, float]:
    """各指標が下限の何倍かを返す。1.0 ちょうどなら下限に張り付いている。"""
    pairs = list(itertools.combinations(colors, 2))
    return {
        "delta_e": min(separation(a, b) for a, b in pairs) / C.DELTA_E_MIN,
        "gray": min(gray_gap(a, b) for a, b in pairs) / C.GRAY_MIN,
        "contrast_bg": min(C.ratio(c, spec["bg"]) for c in colors) / C.UI_MIN,
        "contrast_well": min(C.ratio(c, spec["well"]) for c in colors) / C.UI_MIN,
    }


def balance(colors: list[str], spec: dict) -> float:
    return min(margins(colors, spec).values())


def search(name: str) -> tuple[list[str], dict] | None:
    spec = SCENES[name]
    families = [candidates(spec, lo, hi, slo, shi, llo, lhi)
                for _n, lo, hi, slo, shi, llo, lhi in spec["hues"]]
    # 逸脱枠は色相を問わず、彩度と明度だけ抑えた候補から選ぶ
    dlo, dhi = spec["deviation_l_range"]
    free = (candidates(spec, 0, 359, 0.0, spec["deviation_sat_max"], dlo, dhi)
            if spec["deviations"] else [])

    if "-v" in sys.argv:
        for (n, *_), fam in zip(spec["hues"], families):
            print(f"    候補 {n}: {len(fam)}件")
        if spec["deviations"]:
            print(f"    候補 逸脱枠: {len(free)}件")

    slots: list[list[str]] = []
    for i in range(len(C.CHARTS)):
        if i in spec["fixed"]:
            slots.append([spec["fixed"][i]])
        elif i - len(spec["fixed"]) < len(families):
            slots.append(families[i - len(spec["fixed"])])
        else:
            slots.append(free)
    if spec["deviations"] and len(slots) == len(C.CHARTS) and not free:
        return None

    best = (-1.0, None)

    def walk(idx: int, chosen: list[str]) -> None:
        nonlocal best
        if idx == len(C.CHARTS):
            score = balance(chosen, spec)
            if score > best[0]:
                best = (score, list(chosen))
            return
        for color in slots[idx]:
            if color in chosen:
                continue
            if any(separation(color, p) < floors()["delta_e"]
                   or gray_gap(color, p) < max(target_gray(spec), floors()["gray"])
                   for p in chosen):
                continue
            walk(idx + 1, chosen + [color])

    walk(0, [])
    if best[1] is None:
        return None
    colors = best[1]
    stats = {
        "balance": best[0],
        "margins": margins(colors, spec),
        "min_delta_e": min(separation(a, b) for a, b in itertools.combinations(colors, 2)),
        "min_gray": min(gray_gap(a, b) for a, b in itertools.combinations(colors, 2)),
        "contrast_bg": [C.ratio(c, spec["bg"]) for c in colors],
        "contrast_well": [C.ratio(c, spec["well"]) for c in colors],
        "lightness": [labs(c)[0][0] for c in colors],
    }
    return colors, stats


def report(name: str) -> None:
    global MARGIN
    spec = SCENES[name]
    print(f"\n=== {name} ===")
    print(f"  色相の家族: " + " / ".join(h[0] for h in spec["hues"])
          + f"　逸脱枠 {spec['deviations']}色"
          + (f"（彩度 {spec['deviation_sat_max']} 以下）" if spec["deviations"] else ""))
    f = floors()
    print(f"  足切り（下限の {MARGIN} 倍）: ΔE>={f['delta_e']:.0f} "
          f"ΔL*>={max(target_gray(spec), f['gray']):.1f} 対比>={f['contrast']:.1f} "
          f"彩度<={SATURATION_MAX}")
    found = search(name)
    if not found:
        # 「解なし」だけでは、どこまでなら届くのかが分からない。
        # 足切りを下げながら再探索して、このシーンの上限を示す
        keep = MARGIN
        for trial in (1.15, 1.10, 1.05, 1.00):
            MARGIN = trial
            _LAB.clear()
            found = search(name)
            if found:
                print(f"  余裕 {keep} 倍では解がありません。"
                      f"このシーンの上限は {found[1]['balance']:.2f} 倍です。")
                break
        MARGIN = keep
        if not found:
            print("  下限そのもの（1.00倍）でも解がありません。制約を見直してください。")
            return
    colors, st = found
    m = st["margins"]
    print(f"  下限に対する余裕（最小 {st['balance']:.2f}倍）: "
          + " ".join(f"{k} {v:.2f}倍" for k, v in m.items()))
    print(f"  最小ΔE {st['min_delta_e']:.1f}（下限 {C.DELTA_E_MIN:.0f}） ／ "
          f"最小ΔL* {st['min_gray']:.1f}（下限 {C.GRAY_MIN:.0f}）")
    for i, c in enumerate(colors, 1):
        print(f"    chart-{i} {c}  L*{st['lightness'][i-1]:5.1f}  "
              f"対bg {st['contrast_bg'][i-1]:5.2f}:1  対raised {st['contrast_raised'][i-1]:5.2f}:1")


def main() -> int:
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--all" in sys.argv or not names:
        names = list(SCENES)
    for n in names:
        if n not in SCENES:
            print(f"未定義のシーン: {n}（定義済み: {', '.join(SCENES)}）")
            return 1
        report(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
