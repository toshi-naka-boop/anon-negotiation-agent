#!/usr/bin/env bash
# 画面(static/)が、ブラウザの保存領域に書かないことを確かめる(design.md §1.2・§12.1 AC-02。台帳 X-6)。
#
#   bash scripts/check_no_web_storage.sh [ディレクトリ]    # 省くと、プロジェクト直下の static/
#
# 生の値(面談の入力)は、フォームの中にだけ置き、送信したら消す。ブラウザの保存領域には書かない。そのため、static/ の JS・HTML に、
# 保存領域の名前(Web Storage の 2 つ・IndexedDB・WebSQL・Cookie Store・Cache Storage の入口)と、クッキーへの書き込み
# (`.cookie =`)が 1 つもないことを、grep で確かめる。読み出しだけの使い方も、この画面には要らないので、名前が出たら不合格にする。
#
# 終了コード: 見つからなければ 0、見つかれば 1(見つけた行を標準エラーに出す)。ディレクトリがない・調べるファイル(JS・HTML)がないときも 1
# (何も調べずに通ってしまわないように)。
set -u

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:-$root/static}"

if [ ! -d "$target" ]; then
  echo "check_no_web_storage: ディレクトリがありません: $target" >&2
  exit 1
fi

files=()
while IFS= read -r file; do
  files+=("$file")
done < <(find "$target" -type f \( -name '*.js' -o -name '*.mjs' -o -name '*.html' \) | sort)

if [ "${#files[@]}" -eq 0 ]; then
  echo "check_no_web_storage: 調べるファイル(JS・HTML)がありません: $target" >&2
  exit 1
fi

found=$(grep -nHE \
  -e 'localStorage' \
  -e 'sessionStorage' \
  -e 'indexedDB' \
  -e 'openDatabase' \
  -e 'cookieStore' \
  -e 'caches\.open' \
  -e '\.cookie[[:space:]]*=([^=]|$)' \
  "${files[@]}")

if [ -n "$found" ]; then
  echo "check_no_web_storage: ブラウザの保存領域に書く(または、その入口になる)コードがあります:" >&2
  echo "$found" >&2
  exit 1
fi

echo "check_no_web_storage: OK(${#files[@]} ファイルを調べました。保存領域への書き込みはありません)"
