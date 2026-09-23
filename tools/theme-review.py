#!/usr/bin/env python3
"""theme.css からレビュー資料（1枚のHTML）を作る。

手で書いた資料は、調色を1回変えた時点で嘘になる。レビューのたびに
これを実行して、いま theme.css に入っている値そのものを見せる。

    python3 tools/theme-review.py                     # docs/theme-review.html へ出力
    python3 tools/theme-review.py -o /tmp/r.html      # 出力先を指定
    python3 tools/theme-review.py path/to/theme.css   # 別のCSSを見る

資料に載るもの：
  - シーンごとの合否（tools/check-contrast.py と同じ判定を通した結果）
  - 実際の色で描いた小さな画面見本（数値だけでは伝わらないため）
  - トークン一覧（色見本・値・用途コメント）
  - コントラスト表（下限つき・合否つき）
  - 系列色のP型/D型シミュレーションと白黒変換、全ペアのΔE76
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import importlib.util
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def shown_path(path: Path) -> str:
    """資料に出すパス。リポジトリからの相対にする。

    絶対パスを焼き付けると、ディレクトリ名を変えたときやワークツリーで
    生成したときに、資料が置き場所を漏らしたうえで嘘になる。
    """
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return path.name


def _load_checker():
    spec = importlib.util.spec_from_file_location("checker", HERE / "check-contrast.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load_checker()


def fingerprint(source: Path, text: str) -> tuple[str, str]:
    """このページがどのファイルを見たのかを、後から突き合わせられるようにする。

    生成時刻だけでは、レビューで見た資料と手元の theme.css が同じものか
    確かめられない。内容そのもののハッシュと、あれば git の short hash を出す。
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    rev = ""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", source.name],
            cwd=source.parent, capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            rev = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return digest, rev


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def diff_section(prev: dict, now: dict) -> str:
    """前回の生成から何が変わったかを出す。

    同じURLに再公開すると前の版は消えるので、資料自身が差分を持っていないと
    「前回と比べてどうか」がレビューの場で分からない。
    """
    if not prev:
        return ('<div class="todo">レビュー済み版の記録がありません。'
                'レビューが通ったら <code>--mark-reviewed</code> を付けて実行してください。</div>')
    head = (f'<p class="cap">基準＝レビュー済み版 {html.escape(prev.get("stamp", "—"))}'
            f'（sha256 <code>{html.escape(prev.get("digest") or "記録なし")}</code>）'
            f'　→　今回 {html.escape(now.get("stamp", "—"))}'
            f'（sha256 <code>{html.escape(now.get("digest") or "—")}</code>）</p>')
    rows = []
    if prev.get("digest") != now.get("digest"):
        rows.append(f'<tr><td>theme.css の sha256</td>'
                    f'<td><code>{html.escape(prev.get("digest") or "記録なし")}</code></td>'
                    f'<td><code>{html.escape(now.get("digest") or "—")}</code></td></tr>')
    for scene in C.SCENES:
        p = prev.get("scenes", {}).get(scene, {})
        n = now.get("scenes", {}).get(scene, {})
        if p.get("status") != n.get("status"):
            rows.append(f'<tr><td>{scene} の判定</td><td>{html.escape(p.get("status", "—"))}</td>'
                        f'<td>{html.escape(n.get("status", "—"))}</td></tr>')
        pt, nt = p.get("tokens", {}), n.get("tokens", {})
        for key in sorted(set(pt) | set(nt)):
            if pt.get(key) != nt.get(key):
                rows.append(
                    f'<tr><td>{scene} <code>--{html.escape(key)}</code></td>'
                    f'<td>{swatch(pt[key]) if key in pt else "（無し）"}</td>'
                    f'<td>{swatch(nt[key]) if key in nt else "（削除）"}</td></tr>')
    if not rows:
        return head + '<p>レビュー済み版から変わっていません。</p>'
    return (head
            + '<div class="tw"><table><thead><tr><th>項目</th><th>レビュー済み版</th><th>今回</th></tr>'
              f'</thead><tbody>{"".join(rows)}</tbody></table></div>')


def comments(text: str) -> dict[str, dict[str, str]]:
    """シーンごとの「トークン → 用途コメント」。資料の説明欄になる。"""
    out: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        selector = " ".join(selector.split())
        name = "light" if selector == ":root" else None
        m = re.fullmatch(r'\[data-scene="([a-z]+)"\]', selector)
        if m:
            name = m.group(1)
        if not name:
            continue
        found = {}
        for tok, _v, note in re.findall(
                r"--([a-z0-9-]+)\s*:\s*([^;]+);\s*(?:/\*(.*?)\*/)?", body, re.S):
            note = " ".join((note or "").split())
            note = re.sub(r"\s*\d+\.\d+:1.*$", "", note).strip()
            if note:
                found[tok] = note
        out.setdefault(name, {}).update(found)
    return out


def polarity(text: str, scene: str) -> str:
    sel = r":root" if scene == "light" else rf'\[data-scene="{scene}"\]'
    m = re.search(sel + r"\s*\{(.*?)\n\}", text, re.S)
    if not m:
        return "—"
    p = re.search(r"color-scheme:\s*([a-z]+)", m.group(1))
    return p.group(1) if p else "未宣言"


def todo_text(text: str, scene: str) -> str:
    m = re.search(rf'\[data-scene="{scene}"\]\s*\{{(.*?)\n\}}', text, re.S)
    if not m:
        return ""
    notes = re.findall(r"/\*(.*?)\*/", m.group(1), re.S)
    return " ".join(" ".join(n.split()) for n in notes)


def grayscale(hex_value: str) -> str:
    y = C.luminance(hex_value)
    return C.to_hex((y, y, y))


def is_color(value: str) -> bool:
    try:
        C.channels(value)
        return True
    except Exception:
        return False


def expand(value: str, tokens: dict[str, str]) -> str:
    """値の中の var(--x) を実際の値に置き換える。

    見本はシーンの :root の外に描くので、var() のままだと解決されず
    影やグローが丸ごと消える（最初に書いたときは実際に消えていた）。
    """
    for _ in range(8):
        new = re.sub(r"var\(--([a-z0-9-]+)\)",
                     lambda m: tokens.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def swatch(value: str, label: str = "") -> str:
    if is_color(value):
        return (f'<span class="sw" style="background:{html.escape(value)}"></span>'
                f'<code>{html.escape(value)}</code>{html.escape(label)}')
    return f'<code>{html.escape(value)}</code>{html.escape(label)}'


def mock(scene: str, t: dict[str, str]) -> str:
    """実際の色で描いた画面見本。数値の表だけでは「読めるか」が分からない。"""
    def g(key: str, default: str = "") -> str:
        return expand(t.get(key, default), t)
    return f'''
<div class="mock" style="background:{g('bg')};color:{g('text')};
     font-family:{html.escape(g('font-ui',''))};border-color:{g('border-strong')}">
  <div class="mock-card" style="background:{g('surface')};border:{g('border-width')} solid {g('border-strong')};
       border-radius:{g('radius')};box-shadow:{html.escape(g('shadow',''))}">
    <div class="mock-h" style="font-family:{html.escape(g('font-display',''))}">送出先</div>
    <div class="mock-in" style="background:{g('surface-well')};border:{g('border-width')} solid {g('border-strong')};
         border-radius:{g('radius')};color:{g('text-muted')};font-family:{html.escape(g('font-mono',''))}">
      rtmp://ingest.example.com/live/■■■■
    </div>
    <div class="mock-row">
      <span class="mock-btn" style="background:{g('accent')};color:{g('on-accent')};
            border-radius:{g('radius')}">送出開始</span>
      <span class="mock-btn" style="background:{g('warning')};color:{g('on-warning')};
            border-radius:{g('radius')}">警告1件あり</span>
      <span class="mock-btn" style="background:{g('danger')};color:{g('on-danger')};
            border-radius:{g('radius')}">停止</span>
      <span class="mock-btn" style="background:{g('success')};color:{g('on-success')};
            border-radius:{g('radius')}">running</span>
    </div>
    <div class="mock-p" style="color:{g('text')}">本文。送出中の絵を毎秒1枚だけ取りに行きます。</div>
    <div class="mock-p" style="color:{g('text-muted')}">補助文字。受信側の絵ではありません。</div>
    <div class="mock-p" style="color:{g('text-disabled')}">操作できない状態の文字。</div>
    <div class="mock-p"><span style="background:{g('selection-bg')};color:{g('selection-text')}">選択したところ</span>
      <span class="mock-focus" style="outline:2px solid {g('focus-ring')};border-radius:{g('radius')};
            color:{g('text')}">フォーカス</span></div>
    <div class="mock-rule" style="border-top:1px solid {g('border')}"></div>
    <div class="mock-charts">{''.join(
        f'<span style="background:{g(k)}"></span>' for k in C.CHARTS if k in t)}</div>
  </div>
</div>'''


def margin_cell(value: float, floor: float) -> str:
    """下限をどれだけ上回っているか。下限すれすれの項目を見つけるための列。"""
    mult = value / floor if floor else 0.0
    # 1.20 未満に印を付ける。前は 1.15 未満にしていたが、いちばん薄い値が
    # ちょうど 1.15 倍で、境界条件のせいで一度も点かなかった
    tight = " tight" if mult < 1.20 else ""
    return f'<span class="mg{tight}">{value - floor:+.2f}<small>（{mult:.2f}倍）</small></span>'


def contrast_rows(t: dict[str, str]) -> str:
    rows = []
    for fg, bg, floor, label in C.PAIRS:
        if fg not in t or bg not in t or not (is_color(t[fg]) and is_color(t[bg])):
            continue
        r = C.ratio(t[fg], t[bg])
        ok = r >= floor
        rows.append(
            f'<tr class="{"ok" if ok else "ng"}"><td>{html.escape(label)}</td>'
            f'<td class="num">{r:.2f}:1</td><td class="num">{floor}</td>'
            f'<td class="num">{margin_cell(r, floor)}</td>'
            f'<td>{"合格" if ok else "不合格"}</td></tr>')
    for fg, bg, label in C.INFO_PAIRS:
        if fg in t and bg in t:
            r = C.ratio(t[fg], t[bg])
            rows.append(
                f'<tr class="info"><td>{html.escape(label)}</td>'
                f'<td class="num">{r:.2f}:1</td><td class="num">参考</td>'
                f'<td class="num">—</td><td>判定対象外</td></tr>')

    for backdrop in C.CHART_BACKDROPS:
        for key in C.CHARTS:
            if key not in t or backdrop not in t:
                continue
            r = C.ratio(t[key], t[backdrop])
            ok = r >= C.UI_MIN
            rows.append(
                f'<tr class="{"ok" if ok else "ng"}"><td>{key} / {backdrop}</td>'
                f'<td class="num">{r:.2f}:1</td><td class="num">{C.UI_MIN}</td>'
                f'<td class="num">{margin_cell(r, C.UI_MIN)}</td>'
                f'<td>{"合格" if ok else "不合格"}</td></tr>')
    return "".join(rows)


def chart_block(t: dict[str, str], scene: str = "") -> str:
    # 下限はシーン別。typewriter は ΔL* を上げ、ΔE は判定しない
    E_MIN = C.delta_e_floor(scene)
    G_MIN = C.gray_floor(scene)
    judged = C.delta_e_judged(scene)
    if not all(k in t for k in C.CHARTS):
        return ""
    bands = []
    for vision, jp in (("normal", "通常"), ("protan", "P型"), ("deutan", "D型")):
        cells = "".join(
            f'<span style="background:{C.simulate(t[k], vision)}"></span>' for k in C.CHARTS)
        worst = min(
            (C.delta_e(C.simulate(t[a], vision), C.simulate(t[b], vision)), a, b)
            for a, b in itertools.combinations(C.CHARTS, 2))
        cls = "info" if not judged else ("ok" if worst[0] >= E_MIN else "ng")
        bands.append(
            f'<div class="band"><span class="band-l">{jp}</span><div class="band-c">{cells}</div>'
            f'<span class="band-n {cls}">最小ΔE {worst[0]:.1f}'
            f'<small>（{worst[1]}×{worst[2]}'
            f'{"・参考・判定対象外" if not judged else ""}）</small></span></div>')
    gray = "".join(f'<span style="background:{grayscale(t[k])}"></span>' for k in C.CHARTS)
    gworst = min((abs(C.lab(t[a])[0] - C.lab(t[b])[0]), a, b)
                 for a, b in itertools.combinations(C.CHARTS, 2))
    gcls = "ok" if gworst[0] >= G_MIN else "ng"
    bands.append(f'<div class="band"><span class="band-l">白黒</span>'
                 f'<div class="band-c">{gray}</div>'
                 f'<span class="band-n {gcls}">最小ΔL* {gworst[0]:.1f}'
                 f'<small>（{gworst[1]}×{gworst[2]}・下限 {G_MIN:.0f}）</small></span></div>')

    head = "".join(f"<th>{k[-1]}</th>" for k in C.CHARTS)
    grid = []
    for a in C.CHARTS:
        cells = []
        for b in C.CHARTS:
            if a == b:
                cells.append('<td class="self">—</td>')
                continue
            d = min(C.delta_e(C.simulate(t[a], v), C.simulate(t[b], v)) for v in C.VISIONS)
            cls = "info" if not judged else ("ok" if d >= E_MIN else "ng")
            cells.append(f'<td class="{cls} num">{d:.0f}</td>')
        grid.append(f'<tr><th>{a[-1]}</th>{"".join(cells)}</tr>')
    return (f'<div class="bands">{"".join(bands)}</div>'
            f'<p class="cap">白黒は色相が落ちて明度だけが残る状態。ΔL* は CIE Lab の明度差で、'
            f'これが小さいと紙の上では同じ線になる。'
            f'下の表は全ペアのΔE76（通常・P型・D型のうち最小値）、'
            + (f'下限 {E_MIN:.0f}。</p>' if judged else
               '<b>このシーンでは判定しません（参考値）</b>。'
               '彩度を持たない設計では色相成分がほぼゼロになり、ΔE76 の定義から '
               'ΔE ≥ |ΔL*| が常に成り立つため、ΔL* 10 を課している時点で '
               'ΔE は ΔL* の別名でしかありません。識別は ΔL* 10 と'
               '線種の併用で担保します。</p>')
            + f'<table class="grid"><tr><th></th>{head}</tr>{"".join(grid)}</table>')


CSS = """
:root{--pg:#fbfbfa;--sf:#fff;--ink:#17181a;--ink2:#3a3d40;--mut:#76797d;--ln:#dedfe1;--lns:#b4b7ba;--fill:#eeefef}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--pg:#131416;--sf:#1a1c1e;--ink:#edeeef;--ink2:#c6c8ca;--mut:#8e9195;--ln:#2e3134;--lns:#4c5053;--fill:#26292b}}
:root[data-theme=dark]{--pg:#131416;--sf:#1a1c1e;--ink:#edeeef;--ink2:#c6c8ca;--mut:#8e9195;--ln:#2e3134;--lns:#4c5053;--fill:#26292b}
*{box-sizing:border-box}
body{margin:0;background:var(--pg);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif}
.page{max-width:980px;margin:0 auto;padding-inline:20px;padding-block:40px 80px}
h1{font-size:30px;margin:0 0 8px;letter-spacing:-.01em}
.sub{margin:0;color:var(--ink2);max-width:62ch}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 0}
.tag{font:11px/1.6 ui-monospace,Menlo,monospace;border:1px solid var(--lns);color:var(--mut);padding:2px 8px;border-radius:2px}
.tag.pass{background:var(--ink);color:var(--pg);border-color:var(--ink)}
h2{font-size:22px;margin:44px 0 4px;padding-top:20px;border-top:2px solid var(--ink)}
h3{font-size:14px;margin:26px 0 8px;font-family:ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);font-weight:500}
p{margin:0 0 12px}.cap{font-size:12.5px;color:var(--mut)}
.badge{font:12px/1.5 ui-monospace,Menlo,monospace;padding:2px 9px;border-radius:2px;border:1px solid var(--lns);color:var(--mut)}
.badge.pass{border-color:var(--ink);color:var(--ink);font-weight:600}
.badge.fail{border-width:2px;border-style:double;border-color:var(--ink);color:var(--ink);font-weight:600}
.scene-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.mock{padding:18px;border:1px dashed var(--lns);margin:8px 0 6px}
.mock-card{padding:16px;max-width:520px}
.mock-h{font-size:15px;font-weight:600;margin-bottom:10px}
.mock-in{padding:9px 11px;font-size:12px;margin-bottom:12px;overflow:hidden;white-space:nowrap}
.mock-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.mock-btn{padding:6px 12px;font-size:12px;font-weight:600}
.mock-p{font-size:13px;margin:0 0 4px}
.mock-focus{display:inline-block;padding:1px 6px;margin-left:8px;font-size:13px}
.mock-rule{margin:12px 0}
.mock-charts{display:flex;gap:6px}
.mock-charts span{width:44px;height:14px;display:block}
.tw{overflow-x:auto;border:1px solid var(--ln);margin-bottom:8px}
table{border-collapse:collapse;width:100%;background:var(--sf);font-size:13px;min-width:460px}
th,td{text-align:left;padding:7px 11px;border-bottom:1px solid var(--ln);vertical-align:top}
thead th{font:11px/1.6 ui-monospace,Menlo,monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--mut);background:var(--fill);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
.num{font-variant-numeric:tabular-nums;white-space:nowrap}
tr.ok td:last-child{color:var(--mut)}
tr.ng{background:repeating-linear-gradient(-45deg,transparent 0 6px,var(--fill) 6px 12px)}
tr.ng td:last-child{font-weight:700;text-decoration:underline double}
.sw{display:inline-block;width:15px;height:15px;border:1px solid var(--lns);vertical-align:-3px;margin-right:7px}
code{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--ink2);word-break:break-all}
.bands{display:grid;gap:7px;margin:10px 0}
.band{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.band-l{font:11px/1.5 ui-monospace,Menlo,monospace;color:var(--mut);width:36px;flex:0 0 36px}
.band-c{display:flex;gap:5px}
.band-c span{width:56px;height:20px;display:block;border:1px solid var(--lns)}
.band-n{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--mut)}
.band-n.ng{color:var(--ink);font-weight:700;text-decoration:underline double}
.band-n small{opacity:.75}
table.grid{min-width:0;width:auto}
table.grid td,table.grid th{text-align:center;padding:5px 12px;font-size:12px}
table.grid td.self{color:var(--mut)}
table.grid td.ng{font-weight:700;text-decoration:underline double}
table.grid td.info{color:var(--mut);font-style:italic}
.band-n.info{color:var(--mut);font-style:italic}
tr.info td{color:var(--mut);font-style:italic}
.mg{color:var(--mut)}
.mg small{opacity:.8}
.mg.tight{color:var(--ink);font-weight:600;border-bottom:1px dotted var(--ink)}
.todo{border-left:5px double var(--lns);padding:10px 14px;background:var(--sf);font-size:13px;color:var(--ink2)}
footer{margin-top:56px;border-top:1px solid var(--ln);padding-top:14px;font-size:12px;color:var(--mut)}
"""


def build(css_text: str, source: Path, state_path: Path):
    scenes = C.parse(css_text)
    own = C.declared(css_text)
    notes = comments(css_text)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    digest, rev = fingerprint(source, css_text)
    # シミュレーションが壊れていると、配色の良し悪し以前に全部の数字が嘘になる。
    # 過去に「変換後の R と G が全色一致する」実装を出荷しているので、
    # 資料にも自己診断の結果を出す
    sim_problems = C.self_test()
    sim_note = ("この実装は毎回自己診断を通しています。見るのは4点 —"
                "ゴールデン値（#ff0000→P型 #5e4c00 ／ D型 #939500、"
                "#00ff00→#f2f700 ／ #dbd900、#0000ff と #808080 は不変）との一致、"
                "変換後の R と G が全色一致していないこと、青チャンネルが動かないこと、"
                "無彩色が色づかないこと。ゴールデン値を見ているので、"
                "P型とD型の行列を取り違えた実装もここで落ちます。"
                if not sim_problems else
                "<b>自己診断に失敗しています：" + html.escape("／".join(sim_problems))
                + "。以下の数値は信用できません。</b>")
    prev = load_state(state_path)
    now_state = {"digest": digest, "rev": rev, "stamp": stamp, "scenes": {}}

    passed, failed, untuned = [], [], []
    bodies = []
    for name in C.SCENES:
        if own.get(name, 0) == 0 and name != "light":
            untuned.append(name)
            now_state["scenes"][name] = {"status": "未調色", "tokens": {}}
            bodies.append(
                f'<h2>{name}</h2><div class="scene-head">'
                f'<span class="badge">未調色</span>'
                f'<span class="cap">極性 {polarity(css_text, name)} / トークン0個</span></div>'
                f'<p class="cap">まだ値が入っていないので検証していません。'
                f'この状態のシーンに合格印は付きません。</p>'
                f'<div class="todo">{html.escape(todo_text(css_text, name))}</div>')
            continue

        t = scenes[name]
        fails, _ = C.check_scene(name, t)
        (failed if fails else passed).append(name)
        now_state["scenes"][name] = {
            "status": "不合格" if fails else "合格",
            "tokens": {k: t[k] for k in C.MUST_EXIST if k in t},
        }
        badge = ('<span class="badge fail">不合格</span>' if fails
                 else '<span class="badge pass">合格</span>')
        fail_html = ""
        if fails:
            items = "".join(f"<li>{html.escape(f)}</li>" for f in fails)
            fail_html = f'<h3>基準に届いていない項目</h3><div class="todo"><ul>{items}</ul></div>'

        rows = []
        for tok in C.MUST_EXIST:
            if tok not in t:
                continue
            raw = t[tok]
            rows.append(
                f'<tr><td><code>--{tok}</code></td><td>{swatch(raw)}</td>'
                f'<td>{html.escape(notes.get(name, {}).get(tok, ""))}</td></tr>')

        bodies.append(f'''
<h2>{name}</h2>
<div class="scene-head">{badge}
  <span class="cap">極性 {polarity(css_text, name)} ／ 自前のトークン {own.get(name, 0)}個</span></div>
{fail_html}
<h3>画面見本（実際の値で描画）</h3>
{mock(name, t)}
<p class="cap">この資料の地色ではなく、シーンの <code>--bg</code> 上に描いています。</p>
<h3>系列色と色覚シミュレーション</h3>
{chart_block(t, name)}
<h3>コントラスト</h3>
<div class="tw"><table><thead><tr><th>組み合わせ</th><th>実測</th><th>下限</th><th>余裕</th>
<th>判定</th></tr></thead><tbody>{contrast_rows(t)}</tbody></table></div>
<p class="cap">「参考」の2行は判定しません。<code>--focus-ring</code> は地とアクセントに挟まれ、
両方から 3:1 離れる色がどのシーンにも存在しないためです。
<code>outline-offset</code> を正の値にしてリングを地の上に浮かせる運用が前提で、
その前提が崩れたときに効いてくる数字として出しています。<br>
「余裕」は下限との差と、下限の何倍か。1.20倍を下回るものに印を付けています
（下限すれすれの項目を見つけるため。印が付いていても不合格ではありません）。</p>
<h3>トークン</h3>
<div class="tw"><table><thead><tr><th>トークン</th><th>値</th><th>用途</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>''')

    tags = "".join(
        [f'<span class="tag pass">合格 {len(passed)}</span>'] +
        ([f'<span class="tag">不合格 {len(failed)}</span>'] if failed else []) +
        ([f'<span class="tag">未調色 {len(untuned)}</span>'] if untuned else []) +
        [f'<span class="tag">{html.escape(source.name)}</span>',
         f'<span class="tag">sha256 {digest}</span>'] +
        ([f'<span class="tag">git {html.escape(rev)}</span>'] if rev else []) +
        [f'<span class="tag">{stamp}</span>'])

    page = f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StreamForge 配色レビュー</title><style>{CSS}</style></head><body>
<div class="page">
<h1>StreamForge 配色レビュー</h1>
<p class="sub">画面の配色・書体・形状は <code>{html.escape(shown_path(source))}</code> の
セマンティックトークンだけで決まります。この資料はそのファイルから自動生成したもので、
載っている値は生成時点の中身そのものです。</p>
<div class="meta">{tags}</div>

<h2>レビュー済み版からの変更</h2>
{diff_section(prev, now_state)}

<h2>見かた</h2>
<p>シーンごとに、<b>画面見本 → 系列色 → コントラスト → トークン</b> の順に並べています。
基準を割った行には斜線の地と二重下線が付きます（色だけに頼らないため）。</p>
<div class="tw"><table><thead><tr><th>項目</th><th>下限</th><th>理由</th></tr></thead><tbody>
<tr><td>本文と背景</td><td class="num">{C.TEXT_MIN}:1</td><td>WCAG 2.1 AA の通常サイズ本文</td></tr>
<tr><td>大きい文字・UI部品・境界・系列色</td><td class="num">{C.UI_MIN}:1</td><td>同 AA の非テキスト要素</td></tr>
<tr><td>系列色どうしの隔たり</td><td class="num">ΔE76 {C.DELTA_E_MIN:.0f}</td>
<td>コントラスト比では「別の色に見えるか」は分からない。青と紫は比が同じでもP型では潰れる</td></tr>
<tr><td>系列色の白黒での明度差</td><td class="num">ΔL* {C.GRAY_MIN:.0f}</td>
<td>白黒印刷では色相が落ちる。ΔE が足りていても明度が並べば同じ線になる</td></tr>
<tr><td>系列色どうしの隔たり（typewriter のみ）</td><td class="num">判定対象外</td>
<td>彩度を持たない設計では色相成分がほぼゼロになり、ΔE が ΔL* と一致するため。
識別は ΔL* 10 と線種の併用で担保する</td></tr>
<tr><td>系列色の白黒での明度差（typewriter のみ）</td><td class="num">ΔL* 10</td>
<td>色相での分離を諦めるぶん、明度差を厚くして白黒での識別を主役にする。
このシーンで<b>唯一の、色による担保</b></td></tr>
</tbody></table></div>

<h3>系列の識別を色だけに頼らない要件</h3>
<div class="tw"><table><thead><tr><th>シーン</th><th>併用する手がかり</th><th>理由</th></tr></thead><tbody>
<tr><td>typewriter</td><td><b>線種の併用を必須</b>（下表のとおり固定）</td>
<td>原稿用紙とタイプライターは一色のインクで書き分けるメディアなので、
これは埋め合わせではなく世界観そのもの。
色を外しても5本が読み分けられる状態を保つ</td></tr>
<tr><td>その他4シーン</td><td>任意（色で ΔE 25 を満たしている）</td>
<td>色覚シミュレーション下でも分離できているため、線種は表現の自由</td></tr>
</tbody></table></div>

<h3>typewriter の線種の割り当て（固定）</h3>
<p>要件として書くだけでは実装時に落ちるので、系列ごとの線種をここで固定します。
<code>app/static/style.css</code> の <code>.series-1</code>〜<code>.series-5</code> が
この表と一対一で対応します（実装済み。<code>--series-color</code> /
<code>--series-dash</code> / <code>--series-border-style</code> /
<code>--series-marker</code> の4つを持ちます）。片方だけ変えないでください。</p>
<div class="tw"><table><thead><tr><th>系列</th><th>色</th><th>線種</th>
<th><code>stroke-dasharray</code> / <code>border-style</code></th><th>マーカー</th></tr></thead><tbody>
<tr><td><code>--chart-1</code></td><td><span class="sw" style="background:#292222"></span><code>#292222</code></td>
<td>実線</td><td class="num"><code>none</code> / <code>solid</code></td><td>●（丸・塗り）</td></tr>
<tr><td><code>--chart-2</code></td><td><span class="sw" style="background:#3d3838"></span><code>#3d3838</code></td>
<td>破線</td><td class="num"><code>6 4</code> / <code>dashed</code></td><td>■（四角・塗り）</td></tr>
<tr><td><code>--chart-3</code></td><td><span class="sw" style="background:#5c4d46"></span><code>#5c4d46</code></td>
<td>点線</td><td class="num"><code>1 3</code> / <code>dotted</code></td><td>▲（三角・塗り）</td></tr>
<tr><td><code>--chart-4</code></td><td><span class="sw" style="background:#7a6458"></span><code>#7a6458</code></td>
<td>一点鎖線</td><td class="num"><code>10 3 1 3</code> / <code>dashed</code></td><td>◇（菱形・抜き）</td></tr>
<tr><td><code>--chart-5</code></td><td><span class="sw" style="background:#8f8061"></span><code>#8f8061</code></td>
<td>二点鎖線</td><td class="num"><code>10 3 1 3 1 3</code> / <code>dashed</code></td><td>○（丸・抜き）</td></tr>
</tbody></table></div>
<p class="cap">HTML/CSS の <code>border-style</code> には一点鎖線・二点鎖線がないため、
罫線として描く場合は <code>repeating-linear-gradient</code> か SVG の
<code>stroke-dasharray</code> を使います。マーカーは凡例と折れ線の頂点の両方に付けます。
線種だけでは細線が潰れる環境があるので、マーカーは省略しません。</p>

<h3>下限を変えた記録</h3>
<p>シーンを足すたびに「通らないから下げる」が横行しないための台帳です。
下げた場合は、色以外の手がかりをあわせて要件にします。</p>
<div class="tw"><table><thead><tr><th>#</th><th>日付</th><th>シーン</th><th>項目</th>
<th>変更</th><th>理由</th><th>埋め合わせ</th></tr></thead><tbody>
<tr><td class="num">1</td><td>2026-09-21</td><td>typewriter</td><td>系列色の ΔE76</td>
<td class="num">25 → 判定対象外</td>
<td>彩度 40% 以下・赤インクと無彩色のみという世界観では、二色覚下で色相による分離ができない。
総当たりで測った到達可能な上限は <b>minΔE 9.97</b>（ΔL* 10 以上・対比 3:1 以上のもとで）。
効いているのは彩度ではなく<b>使える色相の方向数</b>で、色相を自由にすれば彩度 40% のままでも
ΔE 33.7 まで伸びる。
さらに、この配色では色相成分がほぼゼロなので ΔE76 の定義から ΔE ≒ |ΔL*| となり、
ΔL* 10 を課した時点で ΔE の下限は自動的に満たされる（実測 通常 10.09 / P型 9.97 / D型 10.23）。
発火しない下限を置いて「合格」と出すと合格印の意味が薄まるため、下げずに判定から外した</td>
<td>ΔL* の下限を 10 に引き上げ（下記2件目）＋線種の併用を必須化</td></tr>
<tr><td class="num">2</td><td>2026-09-21</td><td>typewriter</td><td>系列色の白黒での明度差 ΔL*</td>
<td class="num">8 → 10（引き上げ）</td>
<td>色相での分離を諦めるぶん、明度差が識別の主役になるため厚くする。
<b>この値は typewriter で唯一の、色による担保</b>。
実測 10.01 に対し、この条件での上限は 10.5（生成りの紙に対比 3:1 を保つと
L* は 14〜56 の 42 しか使えず、それを4つの間隔に割った値）。
<b>ほぼ限界であり、系列を1本でも増やすと満たせなくなる</b>（6本なら間隔は 8.4 まで縮む）</td>
<td>—（厳しくした側）</td></tr>
</tbody></table></div>

<h3>typewriter の系列色について（制約の内訳）</h3>
<p>このシーンだけ ΔE を判定から外しています。総当たりで測った結果は次のとおりです。</p>
<div class="tw"><table><thead><tr><th>条件</th><th>到達できる minΔE</th><th>備考</th></tr></thead><tbody>
<tr><td>赤インクと無彩色のみ・彩度≤40%・ΔL*≥10</td><td class="num">9.97</td>
<td>現状。ΔL* も対比も同時に上限に張り付いている</td></tr>
<tr><td>色相の制限なし・彩度≤40%・ΔL* の制約なし</td><td class="num">33.73</td>
<td>彩度 40% という数字自体は効いていない。効いているのは色相の方向数</td></tr>
<tr><td>色相の制限なし・彩度≤85%・ΔL* の制約なし</td><td class="num">45.03</td>
<td>参考。ここまでやると他シーンと見分けがつかなくなる</td></tr>
</tbody></table></div>
<p>効いている制約は2つです。<b>①使える色相が赤〜セピアの一方向しかないこと</b>
（二色覚下ではこの範囲がさらに縮む）。
<b>②生成りの紙に対比 3:1 を保つと L* が 14〜56 の 42 しか使えず、
4つの間隔に割ると ΔL* は 10.5 が上限になること</b>。
色相を捨てた配色では ΔE ≒ |ΔL*| になるので、ΔE を下限として置いても
ΔL* の下限に吸収されて発火しません。だから下げるのではなく判定から外しました。</p>
<p class="cap">色覚シミュレーションは Viénot, Brettel &amp; Mollon (1999) の二色覚モデル
（線形sRGBに行列を直接かける正典の係数）。{sim_note}
<code>--text-disabled</code> は「操作できない」ことを示す色なので下限の対象外です。</p>

{"".join(bodies)}

<footer>
<p><b>この資料が見ている範囲</b>：theme.css に書かれた<b>トークンの値</b>だけです。
次のものは対象外で、ここが合格でも壊れていることがあります。</p>
<ul>
<li><b>CSS の文法</b> — 値が構文として妥当かは見ていません。実際
<code>--shadow-inset: none</code> は <code>box-shadow</code> のリスト要素になれない文法バグでしたが、
値としては読めてしまうため、このページでは原理的に検出できませんでした。</li>
<li><b>トークンの参照関係</b> — 未定義のトークンを <code>var()</code> で参照していても分かりません。</li>
<li><b>コンポーネント側の使われ方</b> — style.css がどのトークンをどこに使ったかは見ていません。
<code>--border</code> を押せる要素の輪郭に使う類の誤りは、この資料では見つかりません。</li>
<li><b>実際の描画</b> — ブラウザ差、フォントの実在、重なり、アニメーションは対象外です。</li>
<li><b>世界観との整合</b> — 数値が良くても、そのシーンらしさが壊れていることがあります。
ターミナルに鮮やかなマゼンタが入っても、このページは合格と出します。
色相の制約は <code>tools/palette-search.py</code> に書いてありますが、
守られたかどうかをこのページは検証していません。</li>
</ul>
<p>生成 {stamp} ／ <code>tools/theme-review.py</code>
／ 判定は <code>tools/check-contrast.py</code> と同一のロジック
／ 対象 <code>{html.escape(shown_path(source))}</code>
sha256 <code>{digest}</code>{" ／ git " + html.escape(rev) if rev else ""}</p>
</footer>
</div></body></html>'''
    return page, now_state


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    out = None
    if "-o" in sys.argv:
        out = Path(sys.argv[sys.argv.index("-o") + 1])
        args = [a for a in args if str(out) != a]
    source = Path(args[0]) if args else C.DEFAULT_CSS
    out = out or (HERE.parent / "docs" / "theme-review.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    # 基準は「レビューが通った版」。公開しただけの版や、試しに生成しただけの
    # 版で基準がずれると、レビュー相手が見たものとの差分が出せなくなる
    state_path = Path(str(out) + ".reviewed.json")
    page, state = build(source.read_text(encoding="utf-8"), source, state_path)
    out.write_text(page, encoding="utf-8")
    print(f"書き出しました: {out}")
    # 基準は「前回公開した版」。試しに生成しただけでずらさない
    if "--mark-reviewed" in sys.argv:
        save_state(state_path, state)
        print(f"この版を「レビュー済み」の基準にしました: sha256 {state['digest']}")
    else:
        print("基準は据え置き（レビューが通ったら --mark-reviewed を付けて実行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
