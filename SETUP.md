# セットアップ手順

このファイルは、別のマシンの Claude Code（または人）が読んで同じ仕組みを組み立てるための手順書。
仕組みの説明は [README.md](README.md)。作者の環境は Linux（KDE Plasma 6 Wayland）。macOS は試験的に対応している（手順と、使えるもの・使えないものは [macOS での導入](#macos-での導入)）。

## 1. 前提

| もの | 用途 | 確認のしかた |
|---|---|---|
| kitty（作者は 0.51 で確認。`kitten @ ls` が `layout_state.pairs` を出す版が要る） | ペインを並べる・`kitten @` で外から操作する | `kitty --version` |
| Python 3.9 以降（作者は 3.14。3.9 でもテストが通ることを確認している。macOS の `/usr/bin/python3` は 3.9） | `bin/` の道具とテスト。標準ライブラリだけを使う | `python3 --version` |
| Node.js 22 以降 | `lib/cdp.mjs`（組み込みの `WebSocket` と `fetch` を使う CDP クライアント） | `node --version` |
| Claude Code（`claude`） | 並べる中身 | `claude --version` |
| Google Chrome か Chromium（ネイティブでも Flatpak でもよい。探す順と `browser.command` は [docs/config.md](docs/config.md#browserclaude-browserclaude-show-重ねる-chrome)） | `claude-browser` | `claude-kitty doctor` の「Chrome」の行 |
| KDE Plasma 6（Wayland）の `qdbus`（`qdbus6`・`qdbus-qt6` の名前でもよい）・`journalctl --user`（Linux だけ。macOS は要らない） | KWin スクリプトで窓を動かし、結果を journal から受け取る | `qdbus org.kde.KWin /Scripting` |
| `pgrep` | `claude-view` の「保護する窓」の確認・`claude-guard` の働く条件 | `command -v pgrep` |

kitty は公式のインストーラ（`curl -L https://sw.kovidgoyal.net/kitty/installer.sh | sh /dev/stdin`）で `~/.local/kitty.app` に入れる想定（無ければ PATH の kitty を使う）。別の場所なら `CLAUDE_KITTY_KITTY`（kitty）と `CLAUDE_KITTY_KITTEN`（kitten）で指す。kitten は PATH からも探す。

`claude-view` と `claude-wait` だけなら、KDE・Chrome・Node は要らない（`claude-view` は `view.guard` を設定しなければ KWin を呼ばない）。macOS の入れ方は [macOS での導入](#macos-での導入)。

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
| `--hooks[=state,lang,guard,relay,suffix]` | `~/.claude/settings.json`（`$CLAUDE_CONFIG_DIR`）に hook を足す（中身は 5. と docs）。既定は `state,guard`。`lang`（claude-lang-check）は `--hooks=lang` と明示したときだけで、言語は設定の `lang.language` で決める。`relay`（claude-relay。7.）と `suffix`（claude-prompt-suffix。5. の終わり）も `--hooks=relay` などと明示したときだけ。既存のキーと hook は残し、同じ道具・同じ引数の hook が既にあれば足さない（`claude-state` と `~/.local/bin/claude-state` は同じと数える）。書く前に `settings.json.bak-<日時>`、JSON が壊れていれば何もせず止める | 同じ道具の hook を外す（手で入れたものも）。`--all --uninstall` は lang・relay・suffix も外す |
| `--kitty` | kitty.conf の末尾に `include $REPO/kitty/claude-kitty.conf`（macOS は `claude-kitty-macos.conf`）を足し（末尾なので、自分の `listen_on`・`enabled_layouts`・`allow_remote_control`・`filter_notification` は上書きされる。あれば先に「!」で見せる）、`claude_state_watcher.py` と `claude3.session` のリンクを張る。kitty.conf がこのリポジトリの `kitty/kitty.conf` へのリンクなら触らない | 足した行とリンクだけ外す |
| `--systemd` | `contrib/systemd/` のユニットを `~/.config/systemd/user/` に、パスを置き換えて写し、`daemon-reload` と `enable --now`（Linux だけ）。手で入れた中身の違うユニットは上書きしない | 自分が入れたユニットを止めて消す |
| `--launchd` | `contrib/launchd/` の LaunchAgent を `~/Library/LaunchAgents/`（`$CLAUDE_KITTY_LAUNCH_AGENTS`）に、パス・PATH・HOME を置き換えて写し、`launchctl bootstrap gui/<uid>`（macOS だけ）。中身が変わったら `bootout` してから読み込み直す。手で入れた中身の違うものは上書きしない | 自分が入れたものを `launchctl bootout` して消す |
| `--statusline` | 任意（`--all` には入らない）。`~/.claude/settings.json` の `statusLine` に `bin/claude-statusline` を入れる（5. の終わり、[docs/statusline.md](docs/statusline.md)）。`statusLine` が無いときだけで、別のものがあれば置き換えずに知らせる | claude-statusline の `statusLine` だけ外す（`--all --uninstall` でも） |

- 引数なしは `--all` と同じで、変えるものがあれば確認を取る（端末でなければ `--yes` が要る）。`--all` の常駐は OS に合う方だけ（Linux は `--systemd`、macOS は `--launchd`）。
- python3 が 3.9 より古ければ、何もせずに止まる（macOS は `brew install python` を案内する）。
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

## 4. 起動と確認のしかた

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
claude-browser open https://example.com --mode show   # 7. 自分以外のペインの場所に縦 100% で Chrome
claude-browser shot /tmp/shot.png && claude-browser close
```

- 5〜6 は example.session のように `role` の付いたペインが要る（claude3.session には `slot` しか無いので、並べ替えの対象外。`status` は role が無ければ window id を出す）。
- 環境変数（`CLAUDE_VIEW_*` など）は、シェルの rc か設定ファイルで与える。`claude-kitty` は `CLAUDE_KITTY_*` と `CLAUDE_VIEW_*` 以外の `CLAUDE_*` を消して kitty を起動する。
- `claude-wait` の確認のしかた: 別の端末で `claude` を開き、そのセッション ID で `claude-wait test <ID>` を kitty のペインで実行する。元の端末で `/exit` すると、kitty 側で resume される。

## 5. 状態の色（claude-state）

`~/.claude/settings.json` の `hooks` に足す（既存のキーは残す）。下の例は短く書くため素の名前だが、**command は絶対パスで書くのがよい**（`tools/install.sh --hooks` は `<リポジトリ>/bin/claude-state` の絶対パスで書く）。Claude Code を Dock・GUI・別のシェルから起動すると PATH に `~/.local/bin` が無いことがあり、そのとき素の名前の hook は黙って失敗する（`claude-kitty doctor` は、hook の道具が絶対パスでも PATH でも見つからなければ NG にする）。素の名前・`~/.local/bin/…`・絶対パスはどれも同じ hook と数えるので、`install.sh` が二重に足すことは無い。

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
- 手で確認する: `echo '{"session_id":"t1"}' | KITTY_WINDOW_ID=<テスト用のペイン> claude-state hook Notification` → `claude-state list` → `... hook SessionEnd`。動いているほかのペインで試さない。

ステータス行（`statusLine` の command）で名前に色を付ける例（stdin の JSON の `session_id` を使う）:

```sh
input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty')
name=$(printf '%s' "$input" | jq -r '.session_name // "?"')
command -v claude-state >/dev/null 2>&1 && [ -n "$sid" ] && name=$(claude-state paint "$sid" "$name")
# 動いているサブエージェントの数（sub: N）。無ければ空
subs=$(command -v claude-state >/dev/null 2>&1 && [ -n "$sid" ] && claude-state subagents "$sid")
# 別の端末から届いていれば「端末 mac-a から 1 通」（claude-relay を使うときだけ。7.）
relay=$(command -v claude-relay >/dev/null 2>&1 && [ -n "$sid" ] && claude-relay status "$sid")
printf '%s%s%s' "$name" "${subs:+ | $subs}" "${relay:+ | $relay}"
```

### ステータス行に claude-statusline を使う（任意）

上の例を自分で書く代わりに、同梱の `bin/claude-statusline`（python3 だけ。jq は要らない）を使えます。名前（状態の色）・モデル・いまの会話の長さ（`ctx 380k`）・`sub: N`・統括だけ週の使用量（残り時間と %）を 1 行で出します。中身・色・設定は [docs/statusline.md](docs/statusline.md)。

`~/.claude/settings.json` に足す（`command` は絶対パス）:

```json
{
  "statusLine": {"type": "command", "command": "/path/to/claude-kitty/bin/claude-statusline", "refreshInterval": 30}
}
```

- **`statusLine` は 1 つだけ。** 既に自分の `statusLine` があるなら、上書きする前にその `command` と中身を控える（戻せるように）。自分の項目を残したいなら、自分のスクリプトから `claude-statusline` を呼んで後ろに足す形でもよい（stdin をそのまま渡す）。
- `tools/install.sh --statusline` でも入れられる（見せてから確認を取る。別の `statusLine` があれば置き換えない）。外すのは `tools/install.sh --uninstall --statusline`。
- 週の使用量を出す役割は設定 `statusline.weekly_roles`（既定は `view.left` の統括だけ。`["*"]` で全員）。しきい値と色も同じ節（[docs/config.md](docs/config.md#statuslineclaude-statusline-ステータス行)）。

### 文の最後の略号を効かせる claude-prompt-suffix（任意）

指示の文の最後に `wdt`（意見だけ）・`opt`（案を出す）・`pre`（見せる）・`desc 2`（2 番を解説）・`next`（決めることだけ）・`ss`（最新のスクリーンショットを見る）・`aq`（始める前に質問する）を付けると、その意味を UserPromptSubmit hook がモデルに渡し、画面に「略号 wdt を適用」と出します。CLAUDE.md に書いてモデルに任せるより確実に効きます。

- 入れる: `tools/install.sh --hooks=suffix`（`--all` には入らない）。手で入れるなら `hooks.UserPromptSubmit` に `{"type": "command", "command": "/path/to/claude-kitty/bin/claude-prompt-suffix", "timeout": 10}`。
- 拾うのは最後の行の最後の単独の語だけ（貼り付けた文・コードブロック・`$ ` や `--` のあるコマンドの行・引用は見ない）。略号の足し方・変え方・消し方、スクリーンショットの置き場は [docs/prompt-suffix.md](docs/prompt-suffix.md)。
- `desc 2` で指せるよう、一覧に番号を振らせるなら CLAUDE.md に「案・判断・指摘の一覧には番号を振る」と 1 行書く（設定 `prompt_suffix.number_lists: true` でも足せるが、毎ターン増える）。

## 6. 守るべき決まり

- **close と send-text を使わない。** `kitten @ close-window` などのペインやタブを閉じる操作と、`kitten @ send-text` は、中で動いている Claude Code の作業や入力を壊す。`claude-view` も使わない（テストで確認している）。並べ替えは `detach-window`・`focus-window`・`layout_action` だけで行う。例外は `claude-roster sleep` の `close-window` だけで、そのペインの claude（と claude-wait）が終わり、前面にシェルしか残っていないのを確かめてから閉じる。
- **同じセッション ID を 2 つのプロセスで同時に開かない。** 会話記録が壊れうる。別の場所から移すときは必ず `claude-wait` を通す（相手が確実に終わってから resume する。同じ ID の `claude-wait` 同士も flock で 1 つだけにする）。
- **配置（並べ替え）は 1 つのセッションだけが行う。** 統括（`view.left`）だけが `claude-view` を呼ぶ。担当が自分で動かしてよいのは、自分のペインの中の `claude-browser --mode self` くらいにとどめる。同時に 2 か所から並べ替えると形が崩れる（`claude-view` は flock で 1 つずつにするが、意図がぶつかるのは防げない）。
- kitty を Claude Code の Bash ツールから直接起動しない。`CLAUDE_*` を引き継いで、中の claude が子セッション扱いになり会話記録を残さない。`claude-kitty` がそれらを消してから起動する。
- kitty を縮めたり、窓を動かしたりしない（全ペインの claude-code が再描画で崩れることがある）。`claude-browser` は kitty の上に重ねるだけ。
- `bin/` の道具は動いているプロセスが読んでいることがある。更新するときは別のファイルに書いてから `mv` で差し替える（シェルスクリプトは実行中に読み進めるため）。

## 7. 端末をまたぐ連絡（claude-relay）

別の端末（別の人・別の claude.ai アカウントでもよい）の Claude Code セッションと、LAN の中でメッセージをやりとりする。使う人だけが入れる。仕組みと安全の考え方は [docs/relay.md](docs/relay.md)。ここでは、こちら（例: Linux の `linux-desk`、ユーザー `taro`）と先方（例: mac の `mac-a`、ユーザー `hanako`）の両方で受け取れるようにする手順を書く。片方だけが受け取ればよいなら、その側だけ 1.〜2. と、相手の 3. を行う。

**sshd をインターネットに出さないこと**（ルーターのポート転送をしない）。LAN の中だけで使う。

### 1. 受け取る側の sshd を動かす

- **macOS**: 「システム設定」→「一般」→「共有」→「リモートログイン」を入れる。右の (i) で「アクセスを許可」を自分のユーザーだけにしてよい（「すべてのユーザー」でなくてよい）。古い macOS では「システム環境設定」→「共有」→「リモートログイン」。入ったかは別の端末から `ssh hanako@mac-a.local true`、または `claude-kitty doctor` の「relay: sshd」（root なしで分かる範囲を調べる。分からなければ「注意」になる）。ターミナルから入れるなら `sudo systemsetup -setremotelogin on`（新しい macOS では、ターミナルに「フルディスクアクセス」が要ることがある）。
- **Linux**: `sudo systemctl enable --now sshd`（Debian・Ubuntu はサービス名が `ssh`。無ければ `openssh-server` を入れる）。ファイアウォールがあれば LAN からの 22 番を通す（Fedora: `sudo firewall-cmd --add-service=ssh --permanent && sudo firewall-cmd --reload`）。
- ホスト名: mac は `mac-a.local` のような名前で届く（「共有」の画面の上に出る）。Linux も avahi（mDNS）があれば `<ホスト名>.local`。届かなければ LAN の IP でもよい（変わらないようにルーターで固定する）。

### 2. 両方の端末で鍵を作り、相手に渡す 1 行を出す

```sh
claude-relay pair --name linux-desk               # 先方は claude-relay pair --name mac-a
```

`claude-relay trust linux-desk linux-desk.local taro 'ssh-ed25519 AAAA…'` のような 1 行が出る。ホスト名が相手から届くものでなければ `--host 192.168.1.20` などで出し直す。この 1 行を相手に渡す（公開鍵なので、チャットやメールで渡してよい）。設定の `relay.name` に名前を書いておくと、次から `--name` は要らない。

### 3. 相手の 1 行を実行する

```sh
claude-relay trust mac-a mac-a.local hanako 'ssh-ed25519 AAAA…'   # 先方から受け取った 1 行
```

- 何をするか（authorized_keys に足す受け取り専用の行・設定の `relay.peers`・相手の host key の指紋）を見せて、確認を取る。前に登録した host key と違えば止まる（相手に確かめてから `--replace-host-key`）。
- **host key の指紋を相手と見比べる**: 相手の端末で `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`（mac も同じ場所）を出してもらい、trust が見せた `ssh-ed25519 SHA256:…` と同じなら y。違えば止める（別の機械に繋がっているかもしれない）。
- 相手の sshd がまだ動いていなければ、host key を取れないと出る。残りは登録されるので、相手の sshd が動いてから同じ 1 行をもう一度（同じ名前は置き換えるので二重にならない）。

### 4. hook を入れ、確かめる

```sh
~/claude-kitty/tools/install.sh --hooks=relay    # 受け取る側。UserPromptSubmit・Stop・SessionStart に claude-relay hook を足す
claude-kitty doctor                             # relay: sshd・鍵・送る相手・受け取りの登録・hook
claude-relay ping mac-a                         # 「届く: mac-a/main は受け取れる」
claude-relay send mac-a "テストです"              # 先方の統括のペインが待受の色になり、統括が次のターンで読む
claude-relay log                                # 送受信の記録
```

- 受け取るのは統括（`view.left`、既定 `main`）の役割のセッションだけ。統括が kitty の中にいなければ受信箱に置かれ、統括が動いたときに渡る。
- 統括が止まっていても、hook が記録したセッションの socket に知らせが届いて起きる（統括が hook を 1 度通ったあと。受け手の `crossSessionInbound` の設定などで保留になることがある。[docs/relay.md](docs/relay.md#止まっているセッションを起こすsocket)）。0.7.0 で hook を入れたなら、SessionStart を足すため `--hooks=relay` をもう一度。
- ステータス行に「端末 mac-a から 1 通」を出すなら、5. の例に `claude-relay status "$sid"` を足す（[docs/relay.md の「ステータス行」](docs/relay.md#ステータス行)）。
- 止めるとき: 設定の `relay.enabled` を `false`（受け取らない）。相手を外すとき: `claude-relay untrust mac-a`。hook を外すとき: `tools/install.sh --hooks=relay --uninstall`。

## macOS での導入

**試験的。** 作者は Linux（KDE Plasma）で使っていて、macOS の実機では動かしていない。macOS の分岐は Linux の上の単体テスト（`tests/macos_test.py`。`sys.platform`・`ps`・`sysctl`・CoreGraphics・`launchctl` を偽物にする）で確認しただけ。動かしてみて、下の「[確認してほしいこと](#確認してほしいこと)」の結果を issue か作者に返してほしい。

### 手順

ターミナル.app（または iTerm2）で順に行う。

1. **Command Line Tools**（git と `/usr/bin/python3` 3.9 が入る）:
   ```sh
   xcode-select --install
   python3 --version        # 3.9 以降なら OK（3.8 以前なら brew install python）
   ```
2. **Homebrew**（[brew.sh](https://brew.sh) の 1 行。Apple Silicon は `/opt/homebrew`）。入れたあとに出る `eval "$(/opt/homebrew/bin/brew shellenv)"` を `~/.zprofile` に足す指示に従う。
3. **kitty・Node.js・Chrome**:
   ```sh
   brew install --cask kitty            # /Applications/kitty.app（kitty と kitten）
   brew install node                    # 22 以降（claude-browser の CDP）
   brew install --cask google-chrome    # claude-browser を使うなら（/Applications/Google Chrome.app）
   kitty --version                      # 0.51 以降を推奨（kitten @ ls が layout_state.pairs を出す版）
   ```
   kitty の公式インストーラ（`curl -L https://sw.kovidgoyal.net/kitty/installer.sh | sh /dev/stdin`）でもよい（`/Applications/kitty.app` に入る）。
4. **Claude Code** を公式の手順（[docs](https://docs.claude.com/en/docs/claude-code)）で入れ、`claude --version` が通ること。
5. **PATH**: `~/.local/bin`（コマンドのリンクの置き場）を、zsh と bash の両方のログインシェルの PATH に入れる。kitty のセッションファイルは `bash -lic '…'` でペインを開く（bash のログインシェルは `~/.zprofile` を読まない）ので、`~/.bash_profile` にも書く:
   ```sh
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zprofile
   echo 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"' >> ~/.bash_profile
   ```
   開き直したシェルで `command -v claude node` がどちらも見つかること。
6. **claude-kitty を入れる**:
   ```sh
   git clone https://github.com/hummer98/claude-kitty ~/claude-kitty
   ~/claude-kitty/tools/install.sh --dry-run --all   # 何をするかを見るだけ
   ~/claude-kitty/tools/install.sh --all             # 確認してから入れる
   ```
   macOS の `--all` は `--links`・`--hooks`・`--kitty`・`--launchd`（systemd の代わり）。行うこと:
   - `bin/` のコマンドを `~/.local/bin` にリンクする。
   - `~/.claude/settings.json` に hook を足す（command はリポジトリの `bin/` の絶対パス。Claude Code を Dock などから起動して PATH に `~/.local/bin` が無くても動くように）。
   - `~/.config/kitty/kitty.conf` の末尾に `include ~/claude-kitty/kitty/claude-kitty-macos.conf` を足す（ソケットは `/tmp/kitty-<pid>`）。kitty の設定の場所は macOS でも既定で `~/.config/kitty`。
   - `contrib/launchd/` の 3 つの LaunchAgent を `~/Library/LaunchAgents` に写し、`launchctl bootstrap gui/<uid>` で読み込む（`claude-state watch` の常駐、`claude-roster save --auto` の 5 分ごと、`claude-session-stats` の毎週月曜 7:30）。ログは `~/Library/Logs/claude-kitty/`。PATH（`~/.local/bin`・`/opt/homebrew/bin`・システム）と HOME を plist で渡す。入れたときのシェルに `XDG_RUNTIME_DIR` があれば、それも渡す。
7. **kitty を起動し直す**: 動いている kitty を全部終わらせ（⌘Q）、ターミナル.app から `claude-kitty` で開く（`allow_remote_control` と `listen_on` は kitty の起動時にしか読まれない）。`claude-kitty` は macOS では `--detach` の代わりに `nohup` で kitty を切り離して起動する（kitty の案内は `open -n -a kitty --args …` だが、それでは環境変数が渡らないため。ターミナル.app を閉じて kitty が消えるなら知らせてほしい）。
8. **確認**: kitty のペインで
   ```sh
   claude-kitty doctor          # NG が無ければ終了コード 0。macOS では systemd の代わりに launchd の行が出る
   kitten @ ls | head           # リモート操作が通る
   claude-view status           # タブごとの役割
   ```
9. 外すとき: `~/claude-kitty/tools/install.sh --all --uninstall`（LaunchAgent は `launchctl bootout` してから消す）。

### macOS で使えるもの・試験的なもの・使えないもの

| 機能 | macOS | 中身・違い |
|---|---|---|
| `claude-view` の並べ替え（`kitten @` の detach-window・focus-window・layout_action） | 使える | kitty の機能だけ。OS に依存しない |
| `claude-view` の `view.guard`（保護する窓が前面なら並べ替えない） | 試験的 | 前面のアプリの pid を `lsappinfo`（OS に付いている。許可は要らない）で読む。読めないときは、当たるプロセスが動いているだけで「前面にある」とみなして並べ替えない（動かさない側） |
| `claude-state` の色・ステータス行 | 試験的 | 入れ子の claude の判定は、親のプロセスを `sysctl`（KERN_PROCARGS2）と `proc_pidinfo` で辿る（ctypes。使えなければ `ps`） |
| `claude-state watch`（見た完了を idle に戻す常駐） | 試験的 | launchd の LaunchAgent（`install.sh --launchd`）。kitty の socket は `/tmp/kitty-<pid>` を探す |
| `claude-wait` | 試験的 | pid の再利用の見分け（Claude Code の `procStart` との比較）は Linux だけ。macOS では比べず、その pid が生きている間は待つ（待ちすぎる側に倒す） |
| `claude-roster` | 試験的 | save・restore・status は `kitten @` と `~/.claude/sessions` だけ。restart は上の `claude-wait` と同じ見分け方 |
| `claude-browser` の open・shot・url・close・profiles | 使える | Chrome は `/Applications`（か `~/Applications`）の `Google Chrome.app` を直接起動する（`open -a` は使わない）。プロファイルは `~/Library/Application Support/claude-kitty/browser-profiles` |
| `claude-browser` の重ね置き（ペインの上に Chrome） | 試験的 | kitty の窓の位置を CoreGraphics（`CGWindowListCopyWindowInfo`。画面収録の許可は要らない）で読み、タイトルバー（28 ポイント。`hide_window_decorations titlebar-only` なら 0、`$CLAUDE_BROWSER_MAC_TITLEBAR` で変えられる）を除いて、Chrome の窓を CDP の `Browser.setWindowBounds` で置く。**「常に最前面」は無い**: Chrome を前に出す（入力先も Chrome に移る）のは `--focus browser` のときだけ。既定（`kitty`）と `none` は入力先を動かさないので、Chrome が kitty の後ろに隠れることがある。新しく起動した Chrome は、macOS がアプリを前に出して入力先が移ることがある（確認してほしいことの 9.）。位置が読めない・置けないときは、止めずに普通の窓のまま開く |
| `claude-show` | HTML は試験的（上の重ね置き）・画像は使える | 画像は kitty の overlay（`kitten icat`）。kitty の外では `open` で開く |
| `claude-guard`・`claude-lang-check`・`claude-prompt-suffix`・`claude-session-stats` | 使える | OS に依存しない（`claude-guard` の働く条件は `pgrep -f`。macOS の pgrep でも同じ書き方） |
| `claude-kitty` の起動 | 使える | `--detach` の代わりに `nohup`。設定の `kitty.maximize`・`kitty.desktop` は使わない（あれば注意を出して飛ばす） |
| `claude-kitty doctor` | 使える | systemd の代わりに launchd、KWin の代わりに `lsappinfo` の行を出す。Chrome は `claude-browser` と同じ探し方 |
| 仮想デスクトップ（Space）への移動・最大化・窓の「常に最前面」 | 使えない | KWin（KDE Plasma）専用。macOS に許可の要らない公式の方法が無い |
| systemd のユーザーユニット（`install.sh --systemd`） | 使えない | launchd（`install.sh --launchd`）で代わりにする |

アクセシビリティ・画面収録の許可が要る方法（System Events・AXUIElement・窓の題の読み取り）は使っていない。

### 確認してほしいこと

macOS で一度通して、結果（出力のコピーか、OK / NG）を返してほしい。`<…>` は自分の環境の値。

1. `python3 tests/run.py` が全部通る（`/usr/bin/python3` の 3.9 と Homebrew の python3 の両方で）。
2. `tools/install.sh --all` が止まらずに終わり、もう一度流すと「変えるものは無い」になる。`launchctl print gui/$(id -u)/claude-kitty.state-watch` が動いている（`state = running`）。
3. `claude-kitty` で kitty が開き、ターミナル.app を閉じても kitty が残る。`echo $KITTY_LISTEN_ON` が `unix:/tmp/kitty-<pid>` の形（`/private/tmp` になっていないか）。
4. `claude-kitty doctor` に NG が無い（あれば、その行）。
5. ペインの色: claude で何か頼むと作業中の色、終わると薄い緑（完了・未読）、そのペインに 2 秒フォーカスすると元の色に戻る。kitty の外で `claude-state list` に、そのセッションが出る。
6. 入れ子の判定: ペインの claude の Bash ツールで `claude -p hi` を動かしても、そのペインの色が変わらない。
7. `claude-view status`・`claude-view --dry-run restore`・`claude-view equalize` が動く。
8. `claude-wait`: 別の端末で `claude` を開き、その ID で kitty のペインから `claude-wait test <ID>`。元の端末で `/exit` すると kitty 側で再開する（再開しないなら、`~/.claude/sessions/<pid>.json` の `procStart` の値を教えてほしい）。
9. `claude-browser open https://example.com` で Chrome が自分のペインの上に重なる（ずれるなら、`claude-browser status` の `rect` と、実際の位置・kitty のタイトルバーの有無）。`claude-browser shot /tmp/x.png`・`claude-browser close` が動く。`--mode show` で他のペインの場所に出る。初めて開くとき・2 回目に開くときに、入力先が Chrome に移るか（kitty で打っていた文字が Chrome に入らないか）。`--focus browser` で Chrome が前に出るか。
10. `view.guard` に Keynote などのプロセスを書き、そのアプリを前面にして `claude-view equalize` が「保護する窓」で止まる。`lsappinfo info -only pid "$(lsappinfo front)"` の出力。
11. kitty の外で `claude-show <画像>` が「プレビュー」で開く。
12. 再起動（ログインし直し）のあとも LaunchAgent が動いている。
13. OS の層の生の出力（リポジトリで）: `python3 -c 'import sys, os; sys.path.insert(0, "lib"); import proc, macwin; print(proc.argv(os.getpid()), proc.ppid(os.getpid()), proc.start_time(os.getpid()), len(proc.processes())); print(macwin.window_list()[:3])'`

### OS に依存する場所

| ファイル | 中身 |
|---|---|
| `lib/proc.py` | プロセスの情報（生きているか・引数・起動時の環境・親・起動時刻・一覧）。Linux は `/proc`、macOS は ctypes で `sysctl` と `proc_pidinfo`（使えなければ `ps`）。`/proc` を直接読むのはここだけ |
| `lib/rundir.py` | 記録の置き場（`$XDG_RUNTIME_DIR`、無ければ `/tmp/claude-kitty-<uid>`）と kitty の socket を探す場所（`$XDG_RUNTIME_DIR` か `/tmp`。macOS は両方）。kitty の watcher は同じ決まりを写して持つ |
| `lib/macwin.py` | macOS の kitty の窓の位置（CoreGraphics） |
| `lib/guard.py` | 前面の窓の pid（KWin / `lsappinfo`） |
| `lib/guardhook.py`・`lib/langcheck.py` | claude-guard の許可と claude-lang-check の記録は、前から `$XDG_RUNTIME_DIR` が無ければ `$TMPDIR`（macOS は `/var/folders/…`）の `claude-guard-<uid>`・`claude-lang-check-<uid>` に置く（`lib/rundir.py` には寄せていない。どちらも hook と同じシェルの子から使うので、`$TMPDIR` がそろう） |
| `lib/kwin.py`・`lib/kitty_to_desktop.js` | KWin スクリプト（Linux / KDE だけ） |
| `bin/claude-browser` | 窓の配置（KWin / macOS は CoreGraphics と CDP の `window`・`place`・`front`。`lib/cdp.mjs`） |
| `bin/claude-kitty` | 起動（`--detach` / `nohup`）、KWin のデスクトップ移動 |
| `bin/claude-show` | kitty の外で開くコマンド（`xdg-open` / `open`） |
| `tools/install.sh` | 常駐（`--systemd` / `--launchd`）、include する kitty の設定（`claude-kitty.conf` / `claude-kitty-macos.conf`） |

### kitty.conf の差

- `XDG_RUNTIME_DIR` は macOS には無い。`listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}` は正しいソケットの場所にならないので、`kitty/claude-kitty-macos.conf` は `listen_on unix:/tmp/kitty-{kitty_pid}` にしている。道具は `XDG_RUNTIME_DIR` が無ければ `/tmp` を探す（macOS では `XDG_RUNTIME_DIR` があっても `/tmp` も探す）。`tools/install.sh --kitty` は macOS では、この `claude-kitty-macos.conf` を include する。
- 道具の記録（`claude-state` など）は `XDG_RUNTIME_DIR` が無ければ `/tmp/claude-kitty-<uid>`（0700）に置く。hook・LaunchAgent・kitty の watcher のどこから見ても同じ場所になるよう、起動のしかたで変わる `$TMPDIR` は使わない。シェルの rc で `XDG_RUNTIME_DIR` を決めているなら、`install.sh --launchd` を流すシェルでも同じ値にしておく（plist に写す）。
- リポジトリの `kitty/kitty.conf` のフォント（`Adwaita Mono`・`Noto Sans Mono CJK JP`）は Linux の fontconfig の事情による作者の設定。macOS では使わない（`install.sh --kitty` は自分の kitty.conf に include の 1 行を足すだけ）。
