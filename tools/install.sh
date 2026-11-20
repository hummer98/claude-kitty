#!/bin/sh
# claude-kitty を入れる・外す（Linux と macOS。POSIX sh と python3 だけを使う）。
#
#   tools/install.sh [部品...] [--yes] [--dry-run] [--uninstall]
#
# 部品（何も書かなければ --all）:
#   --links            bin/ の全コマンドを ~/.local/bin にリンクする（既にある別のファイルは上書きしない）
#   --hooks[=LIST]     ~/.claude/settings.json に hook を足す。LIST は state,lang,guard から選ぶ（既定は state,guard）
#                      state = claude-state（状態の色）、guard = claude-guard、lang = claude-lang-check
#                      （返答の言語を調べる。--hooks=lang と明示したときだけ入れる。言語は設定の lang.language で決める）
#   --kitty            kitty.conf の末尾に「include <リポジトリ>/kitty/claude-kitty.conf」を足し、
#                      watcher と claude3.session のリンクを ~/.config/kitty に張る
#   --systemd          contrib/systemd のユニットを ~/.config/systemd/user に写して enable --now（Linux だけ）
#   --all              上の全部
#
# まず何をするかを見せ、変えるものがあれば確認してから行う（--yes で確認を省く。端末でなければ --yes が要る）。
# --dry-run は見せるだけ。何度流しても同じ結果になり、--uninstall で同じ部品を外せる。
# 書き換える設定ファイル（settings.json・kitty.conf）は、書く前に <名前>.bak-<日時> を作る。
#
# 出力の印: "+ " 足す、"- " 外す、"~ " 置き換える、"= " そのまま（入っている・入っていない）、"! " 注意（何もしない）。
# 終了コード: 0 = できた（または変えるものが無い）、1 = 止めた・失敗した、2 = 引数の誤り。
#
# 場所は環境変数で変えられる: CLAUDE_KITTY_BIN_DIR（既定 ~/.local/bin）、CLAUDE_CONFIG_DIR（Claude Code の
# 設定のフォルダ。既定 ~/.claude）、KITTY_CONFIG_DIRECTORY、XDG_CONFIG_HOME。

usage() {
	sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
	exit "${1:-2}"
}

REPO=$(cd "$(dirname "$0")/.." && pwd -P) || exit 1
OS=${CLAUDE_KITTY_UNAME:-$(uname -s)}          # テストで偽の OS にするため
SYSTEMCTL=${CLAUDE_KITTY_SYSTEMCTL:-systemctl}
BIN_DIR=${CLAUDE_KITTY_BIN_DIR:-$HOME/.local/bin}
SETTINGS=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json
CONF_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
KITTY_DIR=${KITTY_CONFIG_DIRECTORY:-$CONF_HOME/kitty}
UNIT_DIR=$CONF_HOME/systemd/user
UNITS_ENABLE="claude-state-watch.service claude-roster-save.timer claude-session-stats.timer"
KITTY_MARK="# claude-kitty: tools/install.sh が足した行（tools/install.sh --uninstall --kitty で外す）"
UNIT_MARK="# claude-kitty: tools/install.sh が contrib/systemd から作った（手で直すなら、この行を消すと install.sh は触らない）"

do_links=0 do_hooks=0 do_kitty=0 do_systemd=0 any=0
hook_parts="" hooks_named=0
yes=0 dry=0 uninstall=0
for a in "$@"; do
	case "$a" in
	--all) do_links=1 do_hooks=1 do_kitty=1 do_systemd=1 any=1 ;;
	--links) do_links=1 any=1 ;;
	--hooks) do_hooks=1 any=1 ;;
	--hooks=*) do_hooks=1 any=1 hook_parts=${a#--hooks=} hooks_named=1 ;;
	--kitty) do_kitty=1 any=1 ;;
	--systemd) do_systemd=1 any=1 ;;
	--yes|-y) yes=1 ;;
	--dry-run|-n) dry=1 ;;
	--uninstall) uninstall=1 ;;
	-h|--help) usage 0 ;;
	*) echo "install.sh: 知らない引数: $a" >&2; usage 2 ;;
	esac
done
[ "$any" = 1 ] || { do_links=1 do_hooks=1 do_kitty=1 do_systemd=1; }
# lang（返答の言語の検査）は使う人だけが --hooks=lang で入れる。外すときは、--all で全部の hook を外す
if [ -z "$hook_parts" ] && [ "$hooks_named" = 0 ]; then
	if [ "$uninstall" = 1 ]; then hook_parts=state,lang,guard; else hook_parts=state,guard; fi
fi

command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 が要る（3.10 以降）" >&2; exit 1; }
# 部品の名前は先に確かめる（見せたあとで知らない名前で止まらないように）
if [ "$do_hooks" = 1 ]; then
	python3 -c 'import sys; ok={"state","lang","guard"}; p=[x for x in sys.argv[1].split(",") if x]; sys.exit(0 if p and set(p)<=ok else 1)' "$hook_parts" ||
		{ echo "install.sh: --hooks= に使えるのは state,lang,guard: $hook_parts" >&2; exit 2; }
fi

TMPD=$(mktemp -d "${TMPDIR:-/tmp}/claude-kitty-install.XXXXXX") || exit 1
trap 'rm -rf "$TMPD"' EXIT
# sh の trap は INT / TERM を受けても続きを行うので、止めるときは exit する（途中の部品は、流し直せば残りだけを行う）
trap 'echo "# 止めた（流し直せば残りだけを行う）" >&2; exit 130' INT
trap 'exit 143' TERM

DRY=1 CHANGES=0 FAILED=0
note() {  # 1 行出し、変える行を数える。実行の回は「そのまま」の行を省く（見せる回に出したので）
	case "$DRY$1" in "0= "*) return ;; esac
	printf '%s\n' "$1"
	case "$1" in "+ "*|"- "*|"~ "*) CHANGES=$((CHANGES + 1)) ;; esac
}
fail() { note "! $1"; FAILED=1; }
run() { [ "$DRY" = 1 ] || "$@"; }        # 変える操作。見せる回は行わない
rp() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
stamp() { date +%Y%m%d-%H%M%S; }
backup_of() {  # 重ならないバックアップの名前（同じ秒に 2 回流しても前のものを消さない）
	b="$1.bak-$(stamp)" n=1
	while [ -e "$b" ]; do b="$1.bak-$(stamp)-$n"; n=$((n + 1)); done
	echo "$b"
}
has_line() { sed 's/^[[:space:]]*//; s/[[:space:]]*$//' "$1" | grep -q -x -F "$2"; }   # 前後の空白は問わない

# ---- links ------------------------------------------------------------------

link_one() {  # link_one SRC DEST
	if [ -L "$2" ] || [ -e "$2" ]; then
		if [ -L "$2" ] && [ "$(rp "$2")" = "$(rp "$1")" ]; then
			note "= $2（入っている）"
		else
			note "! $2 に別のファイルがある。上書きしない（使うなら自分で消すか、$1 を指すリンクにする）"
		fi
		return
	fi
	note "+ $2 → $1"
	if [ "$DRY" = 0 ]; then
		mkdir -p "$(dirname "$2")" && ln -s "$1" "$2" || fail "$2 を作れない"
	fi
}

unlink_one() {  # unlink_one SRC DEST: SRC を指すリンクだけ消す
	if [ -L "$2" ] && [ "$(rp "$2")" = "$(rp "$1")" ]; then
		note "- $2"
		run rm -f "$2" || fail "$2 を消せない"
	elif [ -L "$2" ] || [ -e "$2" ]; then
		note "= $2 はこのリポジトリへのリンクでない。残す"
	else
		note "= $2（入っていない）"
	fi
}

step_links() {
	echo "## コマンドのリンク（$BIN_DIR）"
	for src in "$REPO"/bin/*; do
		[ -f "$src" ] && [ -x "$src" ] || continue
		if [ "$uninstall" = 1 ]; then unlink_one "$src" "$BIN_DIR/${src##*/}"; else link_one "$src" "$BIN_DIR/${src##*/}"; fi
	done
	if [ "$uninstall" = 0 ]; then
		case ":$PATH:" in
		*":$BIN_DIR:"*) ;;
		*) note "! $BIN_DIR が PATH に無い。シェルの rc（ログインシェルの PATH）に足す: export PATH=\"$BIN_DIR:\$PATH\"" ;;
		esac
	fi
}

# ---- hooks ------------------------------------------------------------------

step_hooks() {
	echo "## Claude Code の hook（$SETTINGS: $hook_parts）"
	set -- --settings "$SETTINGS" --repo "$REPO" --parts "$hook_parts"
	[ "$uninstall" = 1 ] && set -- "$@" --uninstall
	[ "$DRY" = 1 ] && set -- "$@" --dry-run
	out=$(python3 "$REPO/tools/merge_hooks.py" "$@")
	rc=$?
	[ "$DRY" = 0 ] && out=$(printf '%s\n' "$out" | grep -v '^= ')
	[ -n "$out" ] && printf '%s\n' "$out"
	n=$(printf '%s\n' "$out" | grep -c '^[-+~] ')
	CHANGES=$((CHANGES + n))
	[ "$rc" = 0 ] || { FAILED=1; echo "! settings.json を直してから流し直す（何も書いていない）"; }
	case ",$hook_parts," in
	*,lang,*)
		# 言語を決めるまで claude-lang-check は何もしないので、入れるときに気づけるよう知らせる
		if [ "$uninstall" = 0 ] && [ "$DRY" = 1 ] && ! PYTHONPATH="$REPO/lib" python3 -c \
			'import os, sys, langcheck; c = langcheck.load_lang_config(os.environ)[0]; sys.exit(0 if c["language"] or not c["enabled"] else 1)'; then
			note "! 設定の lang.language が無いので、claude-lang-check は何もしない（ja などを書く。docs/config.md）"
		fi ;;
	esac
}

# ---- kitty ------------------------------------------------------------------

kitty_include() {
	case "$OS" in Darwin) echo "$REPO/kitty/claude-kitty-macos.conf" ;; *) echo "$REPO/kitty/claude-kitty.conf" ;; esac
}

# 一時ファイルに書いてから置き換える。$1 = 書き換える kitty.conf（リンクなら先）、$2 = 中身を作るコマンド
replace_file() {
	target=$1; shift
	tmp=$(mktemp "$(dirname "$target")/.kitty.conf.XXXXXX") || return 1
	if [ -e "$target" ]; then
		cp -p "$target" "$(backup_of "$target")" || { rm -f "$tmp"; return 1; }
		cp -p "$target" "$tmp" || { rm -f "$tmp"; return 1; }
	else
		chmod 644 "$tmp"                # mktemp は 0600 で作るので、ふつうの設定ファイルの形にする
	fi
	"$@" "$target" "$tmp" || { rm -f "$tmp"; return 1; }
	mv -f "$tmp" "$target"
}
_append_include() {  # _append_include SRC TMP
	if [ -s "$2" ] && [ -n "$(tail -c 1 "$2")" ]; then printf '\n' >>"$2"; fi
	printf '%s\n%s\n' "$KITTY_MARK" "include $(kitty_include)" >>"$2"
}
_drop_include() {
	# 足した 2 行だけを外す（grep は 1 行も残らないと 1 を返すので、2 以上だけを失敗とみる）
	awk -v m="$KITTY_MARK" -v l="include $(kitty_include)" '{ s = $0; gsub(/^[ \t]+|[ \t]+$/, "", s) } s != m && s != l' "$1" >"$2"
}

step_kitty() {
	conf=$KITTY_DIR/kitty.conf
	line="include $(kitty_include)"
	echo "## kitty（$KITTY_DIR）"
	if [ -L "$conf" ] && [ "$(rp "$conf")" = "$(rp "$REPO/kitty/kitty.conf")" ]; then
		note "= $conf はこのリポジトリの kitty/kitty.conf へのリンク（要る設定は入っている。触らない）"
	elif [ "$uninstall" = 1 ]; then
		if [ -f "$conf" ] && has_line "$conf" "$line"; then
			note "- $conf から「$line」"
			if [ "$DRY" = 0 ]; then
				replace_file "$(rp "$conf")" _drop_include || fail "$conf を書き換えられない"
			fi
		else
			note "= $conf に「$line」は無い"
		fi
	elif [ -f "$conf" ] && has_line "$conf" "$line"; then
		note "= $conf は「$line」を読み込んでいる"
	elif [ -f "$conf" ] && grep -E -q '^[[:space:]]*include[[:space:]].*claude-kitty(-macos)?\.conf[[:space:]]*$' "$conf"; then
		note "! $conf は別の場所の claude-kitty.conf を読み込んでいる。触らない（リポジトリを動かしたなら、その行を直す）"
	else
		note "+ $conf の末尾に「$line」"
		if [ -f "$conf" ]; then
			# 末尾の include が後から効くので、同じキーの自分の設定は上書きされる。黙って変えないよう、先に見せる
			for k in allow_remote_control listen_on enabled_layouts filter_notification; do
				v=$(sed -n "s/^[[:space:]]*$k[[:space:]][[:space:]]*//p" "$conf" | tail -n 1)
				[ -n "$v" ] && note "! いまの「$k $v」は claude-kitty.conf の値で上書きされる（残したいなら include の行の後ろに書き直す）"
			done
		fi
		note "! 動いている kitty には、起動し直すまで効かない（allow_remote_control と listen_on は起動時にだけ読まれる）"
		if [ "$DRY" = 0 ]; then
			mkdir -p "$KITTY_DIR" && replace_file "$(rp "$conf")" _append_include || fail "$conf を書き換えられない"
		fi
	fi
	for f in claude_state_watcher.py claude3.session; do
		if [ "$uninstall" = 1 ]; then unlink_one "$REPO/kitty/$f" "$KITTY_DIR/$f"; else link_one "$REPO/kitty/$f" "$KITTY_DIR/$f"; fi
	done
}

# ---- systemd ----------------------------------------------------------------

# ユニットの中身を作る: %h/claude-kitty をリポジトリの場所に置き換え、頭に印の行を付ける
gen_unit() {  # gen_unit SRC OUT
	python3 - "$1" "$REPO" "$HOME" "$UNIT_MARK" >"$2" <<'PY'
import os, sys
src, repo, home, mark = sys.argv[1:5]
text = open(src, encoding="utf-8").read()
repo_r, home_r = os.path.realpath(repo), os.path.realpath(home)
if repo_r.startswith(home_r.rstrip("/") + "/"):
    where = "%h/" + repo_r[len(home_r.rstrip("/")) + 1:]   # ホームの下なら %h のまま（ホームの別名に左右されない）
else:
    where = repo_r
sys.stdout.write(mark + "\n" + text.replace("%h/claude-kitty", where))
PY
}
body() { grep -v -x -F "$UNIT_MARK" "$1"; }   # 印の行を除いた中身（比べる用）

ours() {  # ours DEST GEN: install.sh が作ったもの（印がある）か、作るものと同じ中身か
	grep -q -x -F "$UNIT_MARK" "$1" && return 0
	[ "$(body "$1")" = "$(body "$2")" ]
}

step_systemd() {
	echo "## systemd のユーザーユニット（$UNIT_DIR）"
	case "$OS" in
	Linux) ;;
	Darwin)
		note "! macOS には systemd が無いので飛ばす。claude-state watch などは launchd の LaunchAgent で動かす（SETUP.md の「macOS: launchd で常駐させる」）"
		return ;;
	*) note "! $OS では systemd を使わないので飛ばす"; return ;;
	esac
	if ! command -v "$SYSTEMCTL" >/dev/null 2>&1; then
		note "! systemctl が無いので飛ばす（claude-state watch は手で常駐させる。SETUP.md の 3.）"; return
	fi
	if ! "$SYSTEMCTL" --user show-environment >/dev/null 2>&1; then
		note "! systemctl --user が使えない（ユーザーの systemd が動いていない）ので飛ばす"; return
	fi
	case "$REPO" in *[[:space:]%]*) fail "リポジトリのパスに空白か % がある（ユニットの ExecStart に書けない）。飛ばす: $REPO"; return ;; esac
	reload=0 restart=""
	for src in "$REPO"/contrib/systemd/*.service "$REPO"/contrib/systemd/*.timer; do
		[ -f "$src" ] || continue
		name=${src##*/} dest=$UNIT_DIR/${src##*/} gen=$TMPD/${src##*/}
		gen_unit "$src" "$gen" || { fail "$name を作れない"; continue; }
		if [ "$uninstall" = 1 ]; then
			if [ ! -e "$dest" ]; then
				note "= $dest（入っていない）"
			elif ours "$dest" "$gen"; then
				case " $UNITS_ENABLE " in
				*" $name "*)
					st=$("$SYSTEMCTL" --user is-enabled "$name" 2>/dev/null)
					if [ "$st" = enabled ] || "$SYSTEMCTL" --user is-active --quiet "$name" 2>/dev/null; then
						note "- systemctl --user disable --now $name"
						run "$SYSTEMCTL" --user disable --now "$name" >/dev/null 2>&1 || fail "$name を止められない"
					fi ;;
				esac
				note "- $dest"
				run rm -f "$dest" || fail "$dest を消せない"
				reload=1
			else
				note "! $dest は手で直したもの（作るものと中身が違う）。残す（外すなら systemctl --user disable --now $name のあと消す）"
			fi
			continue
		fi
		if [ ! -e "$dest" ]; then
			note "+ $dest"
		elif [ "$(body "$dest")" = "$(body "$gen")" ]; then
			note "= $dest（入っている）"; continue
		elif grep -q -x -F "$UNIT_MARK" "$dest"; then
			note "~ $dest（新しい中身にする）"
			case "$name" in *.service) restart="$restart $name" ;; esac
		else
			note "= $dest（手で入れたもの。中身が違うので上書きしない。新しい版にするなら $src と見比べる）"; continue
		fi
		if [ "$DRY" = 0 ]; then
			mkdir -p "$UNIT_DIR" && cp "$gen" "$dest.tmp.$$" && mv -f "$dest.tmp.$$" "$dest" || fail "$dest を書けない"
		fi
		reload=1
	done
	if [ "$reload" = 1 ]; then
		note "  systemctl --user daemon-reload"
		run "$SYSTEMCTL" --user daemon-reload || fail "daemon-reload に失敗した"
	fi
	[ "$uninstall" = 1 ] && return
	for u in $UNITS_ENABLE; do
		[ -e "$UNIT_DIR/$u" ] || [ "$DRY" = 1 ] || continue
		st=$("$SYSTEMCTL" --user is-enabled "$u" 2>/dev/null)
		if [ "$st" = enabled ]; then
			case " $restart " in
			*" ${u%.timer}.service "*|*" $u "*)
				if "$SYSTEMCTL" --user is-active --quiet "$u" 2>/dev/null; then
					note "~ systemctl --user restart $u（ユニットを置き換えたため）"
					run "$SYSTEMCTL" --user restart "$u" || fail "$u を起動し直せない"
				fi ;;
			esac
			note "= $u（enable 済み）"
		else
			note "+ systemctl --user enable --now $u"
			run "$SYSTEMCTL" --user enable --now "$u" >/dev/null 2>&1 || fail "$u を enable できない"
		fi
	done
}

# ---- 本体 -------------------------------------------------------------------

steps() {
	CHANGES=0
	[ "$do_links" = 1 ] && step_links
	[ "$do_hooks" = 1 ] && step_hooks
	[ "$do_kitty" = 1 ] && step_kitty
	[ "$do_systemd" = 1 ] && step_systemd
	return 0
}

action=入れる
[ "$uninstall" = 1 ] && action=外す
echo "# claude-kitty を$action（$REPO、$OS）"
DRY=1
steps
if [ "$FAILED" = 1 ]; then
	echo "# 止めた: ! の行を直してから流し直す（何も変えていない）"
	exit 1
fi
if [ "$CHANGES" = 0 ]; then
	echo "# 変えるものは無い（すでにこの状態）"
	exit 0
fi
if [ "$dry" = 1 ]; then
	echo "# --dry-run: 変えるものは $CHANGES 件（何も変えていない）"
	exit 0
fi
if [ "$yes" = 0 ]; then
	if [ ! -t 0 ]; then
		echo "# 端末でないので確認できない。よければ --yes を付けて流し直す（何も変えていない）" >&2
		exit 1
	fi
	printf '# 上の %s 件を行います。よいですか [y/N] ' "$CHANGES"
	read -r ans || ans=
	case "$ans" in y|Y|yes|YES) ;; *) echo "# やめた（何も変えていない）"; exit 1 ;; esac
fi
echo
echo "# 実行"
DRY=0
steps
if [ "$FAILED" = 1 ]; then
	echo "# 一部に失敗した（! の行）。直してから流し直せば、残りだけを行う"
	exit 1
fi
echo "# できた。確かめる: claude-kitty doctor"
