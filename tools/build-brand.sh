#!/usr/bin/env bash
# 支給された原画（assets/brand/）から、配信用と掲載用のブランド素材を生成する。
#
#   app/static/brand/   画面が読むもの（ファビコン・ヘッダーのマーク）
#   docs/images/brand/  README とドキュメントに貼るもの（ロックアップ・アイコン）
#
# 生成物はコミットする。このスクリプトは「どう作ったか」の記録であって、
# 毎回走らせる必要はない。原画を差し替えたときだけ走らせる。
#
# 要 ImageMagick 7（`brew install imagemagick`）。
#
# 踏んだ落とし穴を5つ、ここに残す。
#
#   1. 白地の抜き方を2通り使い分ける。エンブレムは**四隅からの floodfill**。
#      全体置換にすると、顔のクリーム色の毛まで巻き込まれて穴だらけになる。
#      ワードマークとタグラインは逆に**全体置換**。文字の囲まれた部分（o や e の
#      内側）は四隅から到達できず、白いまま残ってしまう。
#   2. floodfill の fuzz は 8% では足りない。縁のアンチエイリアスが白いまま残り、
#      暗い地に置いたとき輪郭が光る。28% まで上げると縁まで抜けて、オレンジの
#      輪郭線で止まる。
#   3. `-draw 'alpha ... floodfill'` の前に **`-fill none` が要る**。塗る色の
#      アルファが使われるので、既定の黒（不透明）のままだと何も透過しない。
#      エラーにも警告にもならず、白地のまま「成功」する。明るい地に置くと
#      気づけないので、必ず暗い地か赤地に載せて確かめること。
#   4. 書き出しには $PNGOPT を付ける。ImageMagick は既定で保存時刻（tIME）を
#      PNG に埋めるので、同じ原画から作り直しても毎回バイト列が変わり、生成物を
#      コミットしている以上、走らせるたびに差分が出る。画素は一致している。
#   5. 中間ファイルには `PNG32:` を付ける。付けないと ImageMagick が
#      「透明だけの画像」をグレースケールPNGとして書き出し、そこに色を合成した
#      時点で全部グレーになる。原因が分かりにくいので必ず明示する。

set -euo pipefail

cd "$(dirname "$0")/.."
SRC=assets/brand
WEB=app/static/brand
DOC=docs/images/brand
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

command -v magick >/dev/null || { echo "ImageMagick が要る: brew install imagemagick" >&2; exit 1; }

# 保存時刻を埋めない。同じ入力からは同じバイト列が出るようにする
PNGOPT=(-strip -define png:exclude-chunk=date,time)

mkdir -p "$WEB" "$DOC"

w() { magick identify -format '%w' "$1"; }
h() { magick identify -format '%h' "$1"; }

# --------------------------------------------------------- エンブレムの白地を抜く
# 四隅から floodfill。原画に触れずに済むよう、結果は原本として $SRC にも残す。
magick "$SRC/icon-master-1254.webp" -alpha set -fill none -fuzz 28% \
  -draw 'alpha 0,0 floodfill'       -draw 'alpha 1253,0 floodfill' \
  -draw 'alpha 0,1253 floodfill'    -draw 'alpha 1253,1253 floodfill' \
  -trim +repage "${PNGOPT[@]}" "PNG32:$SRC/icon-master-transparent.png"

ICON="$SRC/icon-master-transparent.png"

# ------------------------------------------------- ファビコンとヘッダーのマーク
# 顔の部分だけを正方形に切り出す。全身は 32px 以下で潰れて読めない。
# 白く残る背景は navy で埋める（透過にすると、明るいタブで顔だけが浮く）。
magick "$SRC/icon-master-1254.webp" -crop 560x560+400+75 +repage \
  -alpha off -fuzz 8% -fill '#0F172A' -opaque white \
  -filter Lanczos -resize 512x512 "${PNGOPT[@]}" "PNG24:$TMP/mark-512.png"

for s in 16 32 48; do
  magick "$TMP/mark-512.png" -filter Lanczos -resize ${s}x${s} "${PNGOPT[@]}" "PNG24:$WEB/favicon-$s.png"
done
magick "$TMP/mark-512.png" -filter Lanczos -resize 96x96  "${PNGOPT[@]}" "PNG24:$WEB/mark-96.png"
magick "$TMP/mark-512.png" -filter Lanczos -resize 180x180 "${PNGOPT[@]}" "PNG24:$WEB/apple-touch-icon.png"
cp "$TMP/mark-512.png" "$WEB/icon-512.png"

# ----------------------------------------------------------- 掲載用のアイコン
magick "$ICON" -filter Lanczos -resize 1024x1024 -background none "${PNGOPT[@]}" "PNG32:$DOC/icon-1024.png"

# ------------------------------------------------------------- ロックアップ
# 縦組み。エンブレムを大きく置き、その下にワードマーク、さらに下にタグライン。
# README では中央に置くので、横組みより縦組みのほうが収まりがよい。
# light は背景透過、dark は navy の角丸パネル（透過のままだと GitHub の暗い地で
# タグラインの黒が沈む。色を変えるのはタグラインだけなので、地ごと持たせる）。
WM_W=900     # ワードマークの幅
ICON_W=1000  # エンブレムの幅。ワードマークより一回り大きく見せる
PAD=56       # 外周の余白
GAP=54       # エンブレムとワードマークの間
STACK_GAP=26 # ワードマークとタグラインの間

build_lockup() {
  local variant=$1 panel=$2

  magick "$SRC/wordmark.png" -resize ${WM_W}x -background none "${PNGOPT[@]}" "PNG32:$TMP/wm.png"
  local ww wh; ww=$(w "$TMP/wm.png"); wh=$(h "$TMP/wm.png")

  if [ "$variant" = dark ]; then
    magick "$SRC/tagline.png" -fill white -colorize 100% "${PNGOPT[@]}" "PNG32:$TMP/tl0.png"
  else
    cp "$SRC/tagline.png" "$TMP/tl0.png"
  fi
  magick "$TMP/tl0.png" -resize $((WM_W * 70 / 100))x -background none "${PNGOPT[@]}" "PNG32:$TMP/tl.png"
  local tw th; tw=$(w "$TMP/tl.png"); th=$(h "$TMP/tl.png")

  magick "$ICON" -resize ${ICON_W}x -background none "${PNGOPT[@]}" "PNG32:$TMP/ic.png"
  local iw ih; iw=$(w "$TMP/ic.png"); ih=$(h "$TMP/ic.png")

  # 中身の幅は一番広い要素で決まる。3要素とも中央に揃える
  local inner=$((iw > ww ? iw : ww))
  local W=$((inner + PAD * 2))
  local H=$((ih + GAP + wh + STACK_GAP + th + PAD * 2))

  # set -u の下では空配列の展開が落ちるので、必ず1要素以上にしておく
  local draw=(-alpha set)
  [ -n "$panel" ] && draw+=(-fill "$panel" -draw "roundrectangle 0,0,$((W - 1)),$((H - 1)),40,40")

  magick -size ${W}x${H} xc:none "${draw[@]}" \
    "$TMP/ic.png" -geometry +$((PAD + (inner - iw) / 2))+${PAD} -composite \
    "$TMP/wm.png" -geometry +$((PAD + (inner - ww) / 2))+$((PAD + ih + GAP)) -composite \
    "$TMP/tl.png" -geometry +$((PAD + (inner - tw) / 2))+$((PAD + ih + GAP + wh + STACK_GAP)) -composite \
    "${PNGOPT[@]}" "PNG32:$TMP/lockup-$variant.png"

  # README では 360px 前後に置く。2倍解像度として 720px あれば足りる
  magick "$TMP/lockup-$variant.png" -resize 720x "${PNGOPT[@]}" "PNG32:$DOC/lockup-$variant.png"
}

build_lockup light ''
build_lockup dark  '#0F172A'

echo "生成しました:"
find "$WEB" "$DOC" -type f | sort | sed 's/^/  /'
