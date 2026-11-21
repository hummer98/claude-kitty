#!/bin/bash
# 版を上げる: 確認 → テスト → 軽量タグ v<版> → git push origin main → git push origin v<版>。
#
#   tools/release.sh 0.2.0 [--dry-run]            いまの main の先頭に v0.2.0 を付けて push する
#   tools/release.sh 0.1.0 --at 9505403 [--dry-run]   過去の版のタグを、その commit に付けてタグだけ push する
#       （VERSION とテストは見ず、main も push しない。その commit は origin/main にあり、origin/main の CHANGELOG に
#        節があること。release は gh workflow run release.yml -f tag=v0.1.0 で作る）
#
# 確認すること: main にいる・作業ツリーがきれい・VERSION が <版> と同じ（--at のときは見ない）・
# CHANGELOG.md に「## [<版>] - YYYY-MM-DD」の節があって空でない・v<版> のタグが手元にも origin にも無い。
# GitHub の release は、タグの push を受けて .github/workflows/release.yml が CHANGELOG の節から作る
# （main の CHANGELOG を読むので、main を先に push する）。
#
# push するのは main と v<版> の 2 つだけ（--at のときは v<版> だけ）。--all・--tags・--mirror は使わず、--no-follow-tags で
# 注釈付きのタグが一緒に出ていかないようにする。private-* のタグは決して push しない。
# タグは注釈なし（軽量）。注釈付きにすると git の identity が要るため。
set -euo pipefail

usage() { echo "usage: tools/release.sh <version> [--at COMMIT] [--dry-run]" >&2; exit 2; }
die() { echo "release.sh: $*" >&2; exit 1; }

ver="" at="" dry=0
while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run) dry=1 ;;
	--at) [ $# -ge 2 ] || usage; at="$2"; shift ;;
	--at=*) at="${1#--at=}" ;;
	-h|--help) usage ;;
	-*) usage ;;
	*) [ -z "$ver" ] || usage; ver="$1" ;;
	esac
	shift
done
[ -n "$ver" ] || usage
ver="${ver#v}"
# SemVer の形だけを通す（タグの名前に余計なもの、たとえば private- が紛れ込まないように）
[[ "$ver" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$ ]] || die "版は SemVer（例 0.2.0）: $ver"
tag="v$ver"
case "$tag" in private-*) die "private-* のタグは push しない: $tag" ;; esac

root="$(cd "$(dirname "$0")/.." && pwd -P)"   # readlink -f は古い macOS に無い
cd "$root"

run() {  # 書き換える操作。--dry-run では表示だけ
	if [ "$dry" = 1 ]; then echo "[dry-run] $*"; else echo "+ $*"; "$@"; fi
}

# --- 確認 ---
branch="$(git symbolic-ref --quiet --short HEAD || true)"
[ "$branch" = main ] || die "main にいない（いまは ${branch:-detached HEAD}）"
[ -z "$(git status --porcelain)" ] || die "作業ツリーに commit していない変更がある"
[ "$(git config --bool remote.origin.mirror || true)" != true ] || die "remote.origin.mirror が true（全部の ref を push してしまう）"

if [ -n "$at" ]; then
	target="$(git rev-parse --verify --quiet "$at^{commit}")" || die "commit が無い: $at"
	# --at では main を push しない（テストしていない main を出さないため）。代わりに、タグの commit と
	# release の本文（Actions が読む origin/main の CHANGELOG の節）が、もう origin にあることを確認する
	git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null || die "origin/main が無い（git fetch origin）"
	git merge-base --is-ancestor "$target" origin/main || die "$at は origin/main の祖先でない"
	git show origin/main:CHANGELOG.md 2>/dev/null | grep -qF "## [${ver#v}] - " \
		|| die "origin/main の CHANGELOG.md に [${ver#v}] の節が無い（先に main を push する）"
else
	target="$(git rev-parse HEAD)"
	file_ver="$(head -n 1 VERSION 2>/dev/null | tr -d '[:space:]')"
	[ "$file_ver" = "$ver" ] || die "VERSION（${file_ver:-無い}）が $ver でない"
fi

grep -qE "^## \[$(printf '%s' "$ver" | sed 's/[.]/\\./g')\] - [0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]*$" CHANGELOG.md \
	|| die "CHANGELOG.md に「## [$ver] - YYYY-MM-DD」の見出しが無い"
notes="$(python3 tools/changelog_section.py "$ver")" || die "CHANGELOG.md の [$ver] の節を取り出せない"

if git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
	die "タグ $tag は手元にもうある"
fi
if remote_tag="$(git ls-remote --tags origin "refs/tags/$tag")"; then
	[ -z "$remote_tag" ] || die "タグ $tag は origin にもうある"
else
	[ "$dry" = 1 ] || die "origin のタグを確認できない"
	echo "release.sh: origin のタグを確認できない（--dry-run なので続ける）" >&2
fi

echo "版: ${ver}（タグ $tag → $(git log -1 --format='%h %s' "$target")）"
echo "--- release の本文（CHANGELOG の [$ver] の節） ---"
printf '%s\n' "$notes"
echo "---"

# --- テスト ---
if [ -n "$at" ]; then
	echo "テスト: --at なので、いまのテストは走らせない（過去の commit のタグ）"
else
	echo "+ python3 tests/run.py"
	python3 tests/run.py || die "テストが通らない"
fi

# --- タグと push（main と、このタグだけ。--at のときはタグだけ） ---
run git tag "$tag" "$target"
# push に失敗したら手元のタグを消す（直してからそのまま再実行できるように）
undo_tag() { [ "$dry" = 1 ] || git tag -d "$tag" >/dev/null; die "$1（手元のタグ $tag は消した）"; }
if [ -z "$at" ]; then
	run git push --no-follow-tags origin refs/heads/main:refs/heads/main || undo_tag "main を push できない"
fi
run git push --no-follow-tags origin "refs/tags/$tag:refs/tags/$tag" || undo_tag "タグ $tag を push できない"
if [ "$dry" = 1 ]; then
	echo "[dry-run] 何も変えていない"
elif [ -n "$at" ]; then
	# 過去の commit には release.yml が無いので、タグの push では release の workflow が起動しない
	echo "push した。release は既定の枝の定義から手で走らせる: gh workflow run release.yml -f tag=$tag"
else
	echo "push した。GitHub Actions（release）が $tag の release を作る"
fi
