# セットアップ手順

このファイルは、別のマシンの Claude Code（または人）が読んで同じ仕組みを組み立てるための手順書。
仕組みの説明は [README.md](README.md)。作者の環境は Linux（KDE Plasma 6 Wayland）で、macOS では一部を置き換える（[macOS への移植](#macos-への移植)）。

## 1. 前提

| もの | 用途 | 確かめ方 |
|---|---|---|
| kitty（作者は 0.51 で確認。`kitten @ ls` が `layout_state.pairs` を出す版が要る） | ペインを並べる・`kitten @` で外から操作する | `kitty --version` |
| Python 3（3.10 以降を推奨、作者は 3.14） | `bin/` の道具とテスト。標準ライブラリだけを使う | `python3 --version` |
| Node.js 22 以降 | `lib/cdp.mjs`（組み込みの `WebSocket` と `fetch` を使う CDP クライアント） | `node --version` |
| Claude Code（`claude`） | 並べる中身 | `claude --version` |
| Google Chrome（Linux は Flatpak の `com.google.Chrome`） | `claude-browser` | `flatpak info com.google.Chrome` |
| KDE Plasma 6（Wayland）の `qdbus`・`journalctl --user` | KWin スクリプトで窓を動かし、結果を journal から受け取る | `qdbus org.kde.KWin /Scripting` |
| `pgrep`・`notify-send` | `claude-view` の「保護する窓」の確認と通知 | `command -v pgrep notify-send` |

kitty は公式のインストーラ（`curl -L https://sw.kovidgoyal.net/kitty/installer.sh | sh /dev/stdin`）で `~/.local/kitty.app` に入れる想定。別の場所なら `CLAUDE_KITTY_KITTY`（kitty）と `CLAUDE_KITTY_KITTEN`（kitten）で指す。kitten は PATH からも探す。

`claude-view` と `claude-wait` だけなら、KDE・Chrome・Node は要らない（`claude-view` は `view.guard` を設定しなければ KWin を呼ばない）。

## 2. インストール

ふつうは `tools/install.sh` で入れる（下の手作業と同じことを、既にあるものを壊さずに行う）。

```sh
REPO=~/claude-kitty
git clone https://github.com/hummer98/claude-kitty "$REPO"
"$REPO/tools/install.sh" --dry-run --all   # 何をするかを見るだけ
"$REPO/tools/install.sh" --all             # 確認してから入れる（--yes で確認を省く）
claude-kitty doctor                        # 足りないものと直し方
```

| 部品 | 行うこと | 外す（`--uninstall`） |
|---|---|---|
| `--links` | `bin/` の全コマンドを `~/.local/bin`（`$CLAUDE_KITTY_BIN_DIR`）にリンク。既にある別のファイルは上書きしない | このリポジトリを指すリンクだけ消す |
| `--hooks[=state,lang,guard]` | `~/.claude/settings.json`（`$CLAUDE_CONFIG_DIR`）に hook を足す（中身は 5. と docs）。既定は `state,guard`。`lang`（claude-lang-check）は `--hooks=lang` と明示したときだけで、言語は設定の `lang.language` で決める。既存のキーと hook は残し、同じ道具・同じ引数の hook が既にあれば足さない（`claude-state` と `~/.local/bin/claude-state` は同じと数える）。書く前に `settings.json.bak-<日時>`、JSON が壊れていれば何もせず止める | 同じ道具の hook を外す（手で入れたものも）。`--all --uninstall` は lang も外す |
| `--kitty` | kitty.conf の末尾に `include $REPO/kitty/claude-kitty.conf`（macOS は `claude-kitty-macos.conf`）を足し（末尾なので、自分の `listen_on`・`enabled_layouts`・`allow_remote_control`・`filter_notification` は上書きされる。あれば先に「!」で見せる）、`claude_state_watcher.py` と `claude3.session` のリンクを張る。kitty.conf がこのリポジトリの `kitty/kitty.conf` へのリンクなら触らない | 足した行とリンクだけ外す |
| `--systemd` | `contrib/systemd/` のユニットを `~/.config/systemd/user/` に、パスを置き換えて写し、`daemon-reload` と `enable --now`（Linux だけ）。手で入れた中身の違うユニットは上書きしない | 自分が入れたユニットを止めて消す |

- 引数なしは `--all` と同じで、変えるものがあれば確認を取る（端末でなければ `--yes` が要る）。
- `claude-kitty doctor` は読むだけ（`kitten @ ls`・`systemctl --user is-enabled` / `is-active`・設定の読み込み）。`--json` も出せる。
- 版を上げたときは `git pull` のあと `tools/install.sh --all` を流し直せば、増えたコマンドや hook だけを足す（CHANGELOG の「上げるときに手で行うこと」も見る）。

### 手作業で入れる

リポジトリをどこかに置き（以下 `$REPO`）、`~/.local/bin` と `~/.config/kitty` へシンボリックリンクを張る。リンクなので、リポジトリを更新すればそのまま反映される。

```sh
REPO=~/claude-kitty
git clone https://github.com/hummer98/claude-kitty "$REPO"
mkdir -p ~/.local/bin ~/.config/kitty
for f in claude-kitty claude-view claude-wait claude-browser claude-state claude-show \
         claude-guard claude-lang-check claude-session-stats claude-roster; do ln -sfn "$REPO/bin/$f" ~/.local/bin/$f; done
# 既に ~/.config/kitty/kitty.conf があるなら、先に退避するか、中身を自分の kitty.conf に取り込む
ln -sfn "$REPO/kitty/kitty.conf"       ~/.config/kitty/kitty.conf
ln -sfn "$REPO/kitty/claude3.session"  ~/.config/kitty/claude3.session
ln -sfn "$REPO/kitty/claude_state_watcher.py" ~/.config/kitty/claude_state_watcher.py   # 状態の色の「完了（未読）」を戻す
```

- `~/.local/bin` が PATH に入っていること（`claude-wait` はセッションファイルの `bash -lic` から呼ばれるので、ログインシェルの PATH）。
- `bin/` の道具は `Path(__file__).resolve()` でリポジトリの `lib/` を見つけるので、リンク経由で動く。

kitty.conf を差し替えたくなければ、リンクの代わりに自分の kitty.conf の末尾に `include $REPO/kitty/claude-kitty.conf` の 1 行を足す（要る設定だけが入っている。macOS は `claude-kitty-macos.conf`）。

### 自分のセッション配置

`kitty/example.session` を写して `kitty/<名前>.session` を作る（`.gitignore` で `kitty/*.session` は example と claude3 以外 git に入らない）。

```sh
cp "$REPO/kitty/example.session" "$REPO/kitty/mine.session"
ln -sfn "$REPO/kitty/mine.session" ~/.config/kitty/mine.session
```

- `<SESSION-ID-*>` を実在のセッション ID に置き換える。新しく始めるペインなら `bash -lic 'claude; exec bash -l'`（`claude3.session` と同じ）にする。
- `--var role=...` が `claude-view` と `claude-browser` で使う役割名。`--var slot=...` は `claude-browser --over` で使う席の名前。

### 設定ファイル（役割名を変えたとき）

`~/.config/claude-kitty/config.json`（リポジトリの外。全部の節の例は [config.example.json](config.example.json)、各項目の意味と既定値は [docs/config.md](docs/config.md)）:

```json
{
  "view": {
    "left": "main",
    "restore": [["dev-a"], ["dev-b"]],
    "guard": []
  }
}
```

- `left`: 左の列に固定する統括の role。`claude-view` は、この role がいる kitty を操作の対象に選ぶ。
- `restore`: `claude-view restore` の配置（段のリスト、または `"a,b/c"`）。
- `guard`: 前面にあったら並べ替えを控える窓のプロセス（`pgrep -f` の正規表現）。全画面のアプリや発表中の画面など、キーやフォーカスを奪われたくない窓。空なら確認しない。
- 環境変数 `CLAUDE_VIEW_LEFT` / `CLAUDE_VIEW_RESTORE` / `CLAUDE_VIEW_GUARD`（`none` で止める）が設定ファイルより、引数 `--left` がさらに優先。空文字の環境変数は未設定と同じ。

## 3. kitty.conf の要点

`kitty/kitty.conf` のうち、仕組みに要るのは次の 3 行だけ。ほかはフォントと色の好み。

```conf
# Claude Code の Bash ツールから kitten @ を使うため、ソケット経由のリモート操作だけを許す
# （tty 経由は Bash ツールから使えないことがある。yes にするとペインの中の何でも操作できてしまう）
allow_remote_control socket-only
# kitty ごとに別のソケット。道具は $KITTY_LISTEN_ON か、$XDG_RUNTIME_DIR/kitty-<pid> を探す
listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}
# 任意の位置に分割・移動できる splits（claude-view は splits の分割の木を前提にする）
enabled_layouts splits,stack
```

- `allow_remote_control` と `listen_on` は kitty の起動時にしか読まれない。変えたら kitty を起動し直す。
- ソケット名は `kitty-<pid>` の形のままにする（`lib/core.py` の `kitty_pid_from_socket` がこの名前から pid を取る）。
- `background` は claude-state が idle のときに戻す色の基準になる（`state.base_background` で別に指定もできる）。
- `watcher claude_state_watcher.py`（リポジトリの kitty.conf にある）: claude-state の done（緑）を、ペインにフォーカスが来たら idle（元の色）に戻す。相対パスは kitty の設定ディレクトリから解かれるので、`~/.config/kitty/` にリンクを張る（2. の手順）。
  - 動いている kitty で `kitten @ load-config` しても、前からあるペインには watcher が付かない（kitty はペインを作るときに watcher を写し取る）。`on_tab_bar_dirty` の経路でも効くはずだったが、実物（0.51）では前面の done のペインに 3.5 秒フォーカスしても消えなかった。そのため done → idle は常駐の `claude-state watch` を正とする（下）。watcher は、新しいペインと kitty の再起動後に効くので残してよい（二重に動いても害は無い）。
  - `claude-state watch` を systemd のユーザーサービスで動かす:
    ```sh
    mkdir -p ~/.config/systemd/user
    sed "s#%h/claude-kitty#$REPO#" "$REPO/contrib/systemd/claude-state-watch.service" > ~/.config/systemd/user/claude-state-watch.service
    systemctl --user daemon-reload && systemctl --user enable --now claude-state-watch.service
    ```
  - **設定の読み直しは、`set-colors` で付けた全ペインの背景を kitty.conf の色に戻す**（0.51 で確認）。watcher が読み込まれていれば、読み直しに気づいて記録の色で塗り直す（`on_tab_bar_dirty` で `get_options()` が変わったのを見て、0.3 秒後に塗る）。`claude-state watch` も、ペインごとに記録の色と実際の色が 2 回続けて違えば塗り直す。どちらも無いまま読み直したときは `claude-state repaint` で塗り直す。
- 任意: `filter_notification all` で、claude-code が入力待ちのたびに出すデスクトップ通知を止められる（待受中はペインの色で分かるため）。

## 4. 起動と確かめ方

```sh
python3 "$REPO/tests/run.py"            # 1. テスト（kitty も Chrome も使わない。終了コード 0 なら OK）
claude-kitty                            # 2. kitty を claude3.session で起動（自分の配置なら claude-kitty ~/.config/kitty/mine.session）
```

kitty の中のペインから:

```sh
kitten @ ls | head                      # 3. リモート操作が通る（読むだけ）
claude-view status                      # 4. タブごとの役割の一覧
claude-view --dry-run restore           # 5. 並べ替えの操作を表示するだけ（kitty には ls しかしない）
claude-view restore                     # 6. 実際に並べる
claude-browser open https://example.com --mode show --ratio 0.6   # 7. 自分のペインの上 6 割に Chrome
claude-browser shot /tmp/shot.png && claude-browser close
```

- 5〜6 は example.session のように `role` の付いたペインが要る（claude3.session には `slot` しか無いので、並べ替えの対象外。`status` は role が無ければ window id を出す）。
- 環境変数（`CLAUDE_VIEW_*` など）は、シェルの rc か設定ファイルで与える。`claude-kitty` は `CLAUDE_KITTY_*` と `CLAUDE_VIEW_*` 以外の `CLAUDE_*` を消して kitty を起動する。
- `claude-wait` の確かめ方: 別の端末で `claude` を開き、そのセッション ID で `claude-wait test <ID>` を kitty のペインで実行する。元の端末で `/exit` すると、kitty 側で resume される。

## 5. 状態の色（claude-state）

`~/.claude/settings.json` の `hooks` に足す（既存のキーは残す）。`claude-state` は PATH にある前提（無ければ絶対パスで書く）。

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "claude-state hook UserPromptSubmit", "timeout": 10}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "claude-state hook Stop", "timeout": 10}]}],
    "StopFailure":      [{"hooks": [{"type": "command", "command": "claude-state hook StopFailure", "timeout": 10}]}],
    "Notification": [{"matcher": "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input|idle_prompt",
                      "hooks": [{"type": "command", "command": "claude-state hook Notification", "timeout": 10}]}],
    "PreToolUse":         [{"matcher": "*", "hooks": [{"type": "command", "command": "claude-state hook PreToolUse", "timeout": 10}]}],
    "PermissionRequest":  [{"matcher": "*", "hooks": [{"type": "command", "command": "claude-state hook PermissionRequest", "timeout": 10}]}],
    "SubagentStart":      [{"hooks": [{"type": "command", "command": "claude-state hook SubagentStart", "timeout": 10}]}],
    "SubagentStop":       [{"hooks": [{"type": "command", "command": "claude-state hook SubagentStop", "timeout": 10}]}],
    "PostToolUse":        [{"matcher": "*", "hooks": [{"type": "command", "command": "claude-state hook PostToolUse", "timeout": 10}]}],
    "PostToolUseFailure": [{"matcher": "*", "hooks": [{"type": "command", "command": "claude-state hook PostToolUseFailure", "timeout": 10}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "claude-state hook SessionStart", "timeout": 10}]}],
    "SessionEnd":   [{"hooks": [{"type": "command", "command": "claude-state hook SessionEnd", "timeout": 10}]}]
  }
}
```

- `idle_prompt`（入力欄のまま 60 秒）は待ちではなく、Esc で止めた（Stop が来ない）ときに本体を止まったことにするために受ける。
- PreToolUse・PostToolUse は全部のツールで呼ぶ: 許可・質問の待ちを id ごとに外す、Stop のあとに本体やサブエージェントが動いたのを知る、のため（変わらなければ記録を読むだけで 30ms ほどで終わる）。
- PermissionRequest は許可の画面が出る直前に来る（Notification の `permission_prompt` は約 6 秒あと）。何も出力しないので、許可の判断には加わらない。無くても動くが、そのときは「どのツールの許可か」が分からず、どれかのツールが終われば外れる。
- SubagentStart / SubagentStop は、サブエージェントが動いている間を作業中にするため。
- 手で確かめる: `echo '{"session_id":"t1"}' | KITTY_WINDOW_ID=<テスト用のペイン> claude-state hook Notification` → `claude-state list` → `... hook SessionEnd`。動いているほかのペインで試さない。

ステータス行（`statusLine` の command）で名前に色を付ける例（stdin の JSON の `session_id` を使う）:

```sh
input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty')
name=$(printf '%s' "$input" | jq -r '.session_name // "?"')
command -v claude-state >/dev/null 2>&1 && [ -n "$sid" ] && name=$(claude-state paint "$sid" "$name")
# サブエージェントの要約（動いている数 / 終わったまま残っている数と、いちばん古いものからの時間）。無ければ空
subs=$(command -v claude-state >/dev/null 2>&1 && [ -n "$sid" ] && claude-state subagents "$sid")
printf '%s%s' "$name" "${subs:+ | $subs}"
```

## 6. 守るべき決まり

- **close と send-text を使わない。** `kitten @ close-window` などのペインやタブを閉じる操作と、`kitten @ send-text` は、中で動いている Claude Code の作業や入力を壊す。`claude-view` も使わない（テストで確かめている）。並べ替えは `detach-window`・`focus-window`・`layout_action` だけで行う。
- **同じセッション ID を 2 つのプロセスで同時に開かない。** 会話記録が壊れうる。別の場所から移すときは必ず `claude-wait` を通す（相手が確実に終わってから resume する。同じ ID の `claude-wait` 同士も flock で 1 つだけにする）。
- **配置（並べ替え）は 1 つのセッションだけが行う。** 統括（`view.left`）だけが `claude-view` を呼ぶ。担当が自分で動かしてよいのは、自分のペインの中の `claude-browser --mode self` くらいにとどめる。同時に 2 か所から並べ替えると形が崩れる（`claude-view` は flock で 1 つずつにするが、意図がぶつかるのは防げない）。
- kitty を Claude Code の Bash ツールから直接起動しない。`CLAUDE_*` を引き継いで、中の claude が子セッション扱いになり会話記録を残さない。`claude-kitty` がそれらを消してから起動する。
- kitty を縮めたり、窓を動かしたりしない（全ペインの claude-code が再描画で崩れることがある）。`claude-browser` は kitty の上に重ねるだけ。
- `bin/` の道具は動いているプロセスが読んでいることがある。更新するときは別のファイルに書いてから `mv` で差し替える（シェルスクリプトは実行中に読み進めるため）。

## macOS への移植

kitty と `kitten @` の部分（ペインの一覧・移動・分割比・フォーカス）は macOS でもそのまま動く見込み。置き換えが要るのは、窓の操作（KWin）・プロセスの情報（`/proc`）・Chrome の入れ方・通知の 4 つ。

### OS に依存する場所の一覧

| ファイル | 関数・箇所 | Linux で使っているもの | macOS での置き換え案 |
|---|---|---|---|
| `bin/claude-kitty` | 末尾の `qdbus ... loadScript`（`lib/kitty_to_desktop.js`） | 設定の `kitty.desktop` があれば、KWin スクリプトでその仮想デスクトップへ移す（既定は移さない） | 省く（Space の指定は公式 API が無い）か、Hammerspoon で最大化。`--detach` は macOS では使えないので `open -n -a kitty --args --session "$session"` などで起動する |
| `bin/claude-kitty` | `kitty=${CLAUDE_KITTY_KITTY:-~/.local/kitty.app/bin/kitty}` | 公式インストーラの場所 | `CLAUDE_KITTY_KITTY=/Applications/kitty.app/Contents/MacOS/kitty` |
| `bin/claude-kitty` | `readlink -f "$0"` | GNU coreutils | macOS 12.3 より前には `-f` が無い。`python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$0"` などに置き換える |
| `lib/kitty_to_desktop.js` | 全体 | KWin スクリプト | 不要（上と同じ） |
| `lib/kwin.py` | `kwin()` | `qdbus` で KWin スクリプトを読み込み、`journalctl --user` から `console.info` の出力を受け取る | Hammerspoon の `hs` コマンド（`hs -c 'lua'`、`hs.ipc` を有効にする）か `osascript`（AppleScript / JXA）。どちらも結果を標準出力で直接返せるので、journal を読む仕組み（`core.parse_journal`）は要らなくなる |
| `lib/kwin.py` | `active_window_pid()` | `workspace.activeWindow.pid` | `hs -c 'print(hs.window.frontmostWindow():application():pid())'`、または `osascript -e 'tell application "System Events" to unix id of first process whose frontmost is true'` |
| `bin/claude-view` | `guarded_in_front()` | `pgrep -f` + `kwin.active_window_pid()` | `pgrep -f` はそのまま。前面の pid だけ上の置き換え |
| `bin/claude-view` | `kitty_socket()` の `Path(f"/proc/{pid}").exists()` | `/proc` で kitty の生存確認 | `os.kill(pid, 0)`（`ProcessLookupError` なら無い）。Linux でも動くので共通化してよい |
| `bin/claude-view` | `main()` の `notify-send` | デスクトップ通知 | `osascript -e 'display notification "..." with title "claude-view"'`、または `terminal-notifier` |
| `bin/claude-wait` | `start_time()` | `/proc/<pid>/stat` の 22 番目（起動時刻）で pid の再利用を見分ける | `ps -o lstart= -p <pid>`（無ければ空）。Claude Code の sessions JSON の `procStart` が macOS で何を入れるかを確かめて比較方法を合わせる（Linux では `/proc/<pid>/stat` の値と同じ文字列） |
| `bin/claude-browser` | `kitty_socket()` | `/proc` で kitty の生存確認 | `os.kill(pid, 0)` |
| `bin/claude-browser` | `scan_procs()` / `running()` | `/proc/*/cmdline` で Chrome のプロセスと引数を読む | `ps -axww -o pid=,args=`（引数の区切りが空白になるので、`--user-data-dir=` と `--remote-debugging-port=` は正規表現で取り出す。プロファイルのパスに空白を入れないこと）か `psutil`（引数をリストで返す） |
| `bin/claude-browser` | `CHROME`・`PROFILE_ROOT`・`LEDGER` | `flatpak run com.google.Chrome`、`~/.var/app/com.google.Chrome/claude-profiles`（Flatpak の Chrome が書ける場所） | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` を直接起動する（`open -a` だと既存の Chrome に吸収されるので、`--user-data-dir` 付きで実行ファイルを呼ぶ）。プロファイルは `~/.claude-browser/profiles/<key>` など空白の無い場所に置く（`ps` の出力から引数を取り出すとき、`Application Support` の空白で切れるため）。サンドボックスが無いので file:// の許可は要らない |
| `bin/claude-state` | `ancestors()` / `read_environ()` | `/proc/<pid>/stat`・`cmdline`・`environ` で、入れ子の claude かを見分ける | `ps -o ppid=,args= -p <pid>` で親をたどる。起動時の環境は `ps -E` などで読めないことがあるので、読めなければ「入れ子でない」と見なす（既定の動き） |
| `bin/claude-browser` | `cmd_open()` の `--class=claude-browser-<key>` | X11/Wayland の窓のクラスで Chrome の窓を見分ける | macOS では効かない。窓は pid（`running()` で分かる）で見分ける |
| `bin/claude-browser` | `find_kitty()` | KWin で kitty の窓（pid とタイトル）とクライアント領域を取る | Hammerspoon: `hs.application.applicationForPID(pid):allWindows()` と `win:frame()`。AppleScript: `System Events` の `position` / `size`（タイトルバーを含むので、kitty の `hide_window_decorations` か高さの差し引きが要る） |
| `bin/claude-browser` | `browser_windows()` / `main_browser_window()` | KWin で窓のクラスごとに窓を列挙 | Hammerspoon で Chrome の pid の窓を列挙。または CDP の `Browser.getWindowForTarget` で窓 ID を取る |
| `bin/claude-browser` | `place_browser()` | KWin で位置・大きさ・デスクトップ・「常に最前面」（`keepAbove`）・フォーカスを設定 | 位置と大きさは CDP の `Browser.setWindowBounds`（OS を問わず効く）か Hammerspoon の `win:setFrame()`。フォーカスは `win:focus()` / `hs.application.launchOrFocusByBundleID`。**「常に最前面」は macOS に公式の方法が無い**（Hammerspoon でも `hs.window` に公式の always-on-top は無い）。代わりに、配置のたびに Chrome を前に出す・kitty にフォーカスを戻すときは Chrome を下げない、程度にとどまる |
| `lib/geometry.py` | `target_rect()` | KWin の論理座標 | macOS のポイント座標でも同じ計算でよい（Retina の倍率は OS が吸収する）。メニューバーと kitty のタイトルバーの分だけずれないかを確かめる |
| `tests/run.py` | `proc_start()` と `ClaudeWait` のテスト | `/proc/<pid>/stat` | `claude-wait` の `start_time()` を置き換えたら、テストの `proc_start()` も同じ関数を使うようにそろえる |

OS を問わず動くもの（そのまま使える見込み）: `lib/view.py`・`lib/geometry.py`・`lib/core.py`（`parse_journal` は KWin 専用だが副作用はない）・`lib/config.py`・`lib/cdp.mjs`・`bin/claude-view` の並べ替え（`kitten @ ls` / `detach-window` / `focus-window` / `layout_action`）。

### macOS: launchd で常駐させる

`tools/install.sh --systemd` は macOS では何もしない。`claude-state watch`（完了のペインを見たら idle に戻す常駐）は LaunchAgent で動かす。例（`$REPO` は実際のパスに書き換える）:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- ~/Library/LaunchAgents/com.example.claude-state-watch.plist -->
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.claude-state-watch</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$REPO/bin/claude-state</string><string>watch</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

`/usr/bin/python3`（Command Line Tools のもの）は 3.10 より古いことがあるので、`python3 --version` を確かめ、古ければ Homebrew などの python3 の絶対パスを書く。`launchctl load ~/Library/LaunchAgents/com.example.claude-state-watch.plist` で始める。`claude-roster save --auto`（5 分ごと）と `claude-session-stats`（週 1 回）は、同じ形で `StartInterval`（秒）か `StartCalendarInterval` を使う。

### kitty.conf の差

- `XDG_RUNTIME_DIR` は macOS には無い。`listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}` は正しいソケットの場所にならない（未定義の変数のまま残るか空になる）ので、`listen_on unix:/tmp/kitty-{kitty_pid}` にする。道具は `XDG_RUNTIME_DIR` が無ければ `/tmp` を探すので、これでそろう。`tools/install.sh --kitty` は macOS では、この形の `kitty/claude-kitty-macos.conf` を include する。
- フォント（`Adwaita Mono`・`Noto Sans Mono CJK JP`）は Linux の fontconfig の事情による設定。macOS では `font_family Menlo` などに変え、`symbol_map` の行は消してよい。

### 移植の進め方（おすすめの順）

1. `python3 tests/run.py` を通す（`/proc` を使う `ClaudeWait` のテストは、`start_time()` を置き換えるまで落ちる）。
2. `claude-wait` の `start_time()` を `ps -o lstart=` などに置き換え、`ClaudeWait` のテストを通す。
3. kitty.conf の `listen_on` を直し、`claude-view status` と `claude-view --dry-run restore` が動くことを確かめる（KWin は使わない）。
4. `claude-kitty` を macOS の起動方法に直す。
5. `claude-browser` の窓の操作を、`lib/kwin.py` と同じ形（`find_kitty` / `browser_windows` / `place_browser` を差し替える層）で Hammerspoon か AppleScript に置き換える。座標の計算（`lib/geometry.py`）と CDP（`lib/cdp.mjs`）はそのまま使う。
