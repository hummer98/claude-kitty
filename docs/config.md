# 設定ファイル（config.json）

claude-kitty の道具は、1 つの設定ファイルの節（`view`・`kitty`・`browser`・`state`・`lang`・`guard_hook`・`session_stats`・`roster`・`relay`・`model_policy`）から読みます。
**どの節も、どの項目も省けます。** ファイルが無ければ、すべて下の表の既定値で動きます。

- 場所: `$CLAUDE_KITTY_CONFIG` > `$XDG_CONFIG_HOME/claude-kitty/config.json` > `~/.config/claude-kitty/config.json`。リポジトリの外に置きます（手元の役割名やパスが入るため）。
- 全項目の例: リポジトリの [config.example.json](../config.example.json)。JSON はコメントを書けないので、意味はこのファイルに書きます。
  - 使う節だけを写して、値を自分のものに変えてください。例をまるごと写しても動きますが、例のガード（`guard_hook.guards.presenting`）は `enabled: false` にしてあるので何も止めません。例の `lang.language` は `null`（何もしない）なので、使うなら `"ja"` などに変えます。
- 書いたあとは `claude-kitty doctor` で、各道具がその設定を読めるかを確認できます（「設定: <節>」の行）。
- 読み直すのは道具を呼ぶたび（hook は毎回）。常駐の `claude-state watch` だけは起動時に読むので、`state` を変えたら `systemctl --user restart claude-state-watch`（macOS は `launchctl kickstart -k gui/$(id -u)/claude-kitty.state-watch`）。

役割名（role）は、kitty のセッションファイルの `--var role=<名前>` でペインに付けた名前です（[SETUP.md](../SETUP.md#自分のセッション配置)）。例の `main`・`dev-a`・`dev-b` は [kitty/example.session](../kitty/example.session) の名前です。

## view（claude-view・claude-show・claude-roster）

| 項目 | 既定 | 意味 |
|---|---|---|
| `left` | `"main"` | 左の列に固定する統括の役割名。`claude-view` はこの役割のいる kitty を操作し、`claude-browser --area right` / `--area others`（`--mode show` の既定）はこのペインを除いた右側を範囲にする |
| `restore` | `[["dev-a"], ["dev-b"]]` | `claude-view restore` の配置。段（上から）のリストで、各段は左から並べる役割名。`"dev-a,dev-c/dev-b"` の文字列でも書ける |
| `guard` | `[]` | 前面にあったら画面を並べ替えない窓のプロセス（`pgrep -f` の正規表現。文字列 1 つでもよい）。全画面のアプリや発表中の画面など。空なら前面の窓を調べない。前面の窓は KDE Plasma なら KWin、macOS なら `lsappinfo` で読む（macOS で読めないときは、当たるプロセスが動いているだけで並べ替えを控える） |

環境変数が設定より優先します: `CLAUDE_VIEW_LEFT`、`CLAUDE_VIEW_RESTORE`（`"a,b/c"`）、`CLAUDE_VIEW_GUARD`（`none` で止める）。空文字は未設定と同じです。

## kitty（claude-kitty: kitty の起動）

| 項目 | 既定 | 意味 |
|---|---|---|
| `desktop` | `null` | 起動した kitty の窓を移す仮想デスクトップの番号（1 から。KDE Plasma の KWin だけ）。`null` なら移さない |
| `maximize` | `false` | `true` なら最大化して起動する（`--start-as maximized`。`desktop` があれば KWin でも最大化する）。macOS では使わない（注意を出して、最大化しないで起動する。`desktop` も同じ） |

環境変数 `CLAUDE_KITTY_START_AS` が `maximize` より優先します。

## browser（claude-browser・claude-show: 重ねる Chrome）

| 項目 | 既定 | 意味 |
|---|---|---|
| `enabled` | `true` | `false` でブラウザの機能を止める（ブラウザの使い方を自分で決めているとき）。`claude-browser` は `close` のほか（`open`・`move`・`shot`・`url`・`status`・`list`・`profiles`・`note`）を何もしないで、stderr に「browser.enabled が false なのでオフ」と 1 行出し、**終了コード 3** で終わる（`--help`・`--version` は今まで通り）。`close` だけは、前に開いた claude-browser の Chrome を片づけられるよう動く（Chrome を探し、動いていれば CDP で閉じる・応答しなければそのプロファイルのプロセスに SIGTERM。新しく起動はしない）。`claude-show` は HTML・画像・URL（ほかの種類のファイルも、kitty の外と同じく）を OS の既定のアプリ（`xdg-open`、macOS は `open`）で開き、結果の JSON の `shown` がそのコマンド、`via` が `"browser.enabled: false"` になる。`claude-kitty doctor` は Chrome を探さず「browser: オフ（browser.enabled: false）」と出す（node も調べない）。`true` / `false` 以外は設定の誤り（`claude-kitty doctor` の「設定: browser」が NG。claude-browser の `open`・`move` は止まり、ほかは今まで通り知らせて既定で続ける） |
| `command` | `null`（自動で探す） | Chrome（Chromium）の起動のコマンド。文字列のリストで、引数も足せる（`["google-chrome", "--ozone-platform=wayland"]`、`["flatpak", "run", "com.google.Chrome"]` など）。プロファイル・CDP のポート・`--class` などの引数は claude-browser が後ろに足す |
| `profile_root` | `null`（下の置き場） | プロファイルと台帳（`ledger.json`）を置くフォルダ。`~` を使える |

`command` が `null` のときに探す順（最初に見つかったもの）:

1. 下の 2〜5 のうち、入っていて、その置き場（下の表）が既にあるもの（後から別の Chrome を入れても、ログイン済みのプロファイルから黙って移らない。0.4 までの Flatpak の Chrome の `~/.var/app/com.google.Chrome/claude-profiles` もこれで引き継ぐ）。複数あれば下の順
2. ネイティブの Chrome: `google-chrome-stable`・`google-chrome`（PATH から。macOS は `/Applications` か `~/Applications` の `Google Chrome.app`）
3. Flatpak の Chrome（`com.google.Chrome`）
4. ネイティブの Chromium: `chromium`・`chromium-browser`（macOS は `Chromium.app`）
5. Flatpak の Chromium（`org.chromium.Chromium`）

`profile_root` が `null` のときの置き場:

| 起動のしかた | 置き場 |
|---|---|
| Flatpak（`flatpak run <アプリ ID>`。`command` に書いたときも） | `~/.var/app/<アプリ ID>/claude-profiles`（サンドボックスから書ける場所。Chrome は 0.4 までと同じ） |
| Snap（`/snap/bin/…`） | `~/snap/<名前>/common/claude-kitty-profiles`（隠しフォルダはサンドボックスから書けない） |
| それ以外 | `$XDG_DATA_HOME/claude-kitty/browser-profiles`（既定 `~/.local/share/claude-kitty/browser-profiles`。macOS は `$XDG_DATA_HOME` が無ければ `~/Library/Application Support/claude-kitty/browser-profiles`）。ネイティブの Chromium は同じ所の `chromium-profiles`（Chrome とは cookie の鍵が違うので共有しない） |

どれを使っているかは `claude-browser status`（`browser` と `profile_root`）と `claude-kitty doctor`（「Chrome」の行）で分かります。
Flatpak / Snap の Chrome（`command` に `flatpak run …` を書いたときも。`--filesystem=` の引数も判定に入る）から見えない場所の file:// は、表示用のコピーをアプリから見える場所に写して開きます（[README の claude-browser の節](../README.md#claude-browser)）。
置き場を変えると、前のプロファイル（ログイン）と台帳は引き継がれません。窓の配置は KDE Plasma の KWin で、Chrome の窓は `--class=claude-browser-<名前>`（X11 では WM_CLASS、Wayland では app_id になる）で見分けます。macOS は試験的に、kitty の窓の位置を CoreGraphics で読み、Chrome の窓を CDP（`Browser.setWindowBounds`）で置きます（「常に最前面」は無い。置けなければ普通の窓のまま開く。[SETUP.md の macOS の節](../SETUP.md#macos-での導入)）。

## state（claude-state: 状態の色）

| 項目 | 既定 | 意味 |
|---|---|---|
| `base_background` | kitty.conf の `background`（無ければ `#000000`） | idle のときに戻すペインの背景（`#rrggbb`） |
| `colors.<状態>.background` | 下の表 | その状態のペインの背景（`#rrggbb`）。`null` は元の色 |
| `colors.<状態>.name` | 下の表 | ステータス行の名前の色（256 色の番号 0〜255）。`null` は色を付けない |
| `done_dwell_seconds` | `2` | 完了（未読）のペインに、これだけ続けてフォーカスしたら idle に戻す（0〜3600 秒） |
| `watch_sockets` | `null` | `claude-state watch` が見る kitty のソケット（`"unix:/…"` のリスト）。`null` なら `$XDG_RUNTIME_DIR/kitty-*` の動いているもの全部 |
| `notice.contains` | `[]` | 他のセッションからのメッセージの本文にこれのどれかがあれば「通知」とみて、色を変えない |
| `notice.prefix` | `[]` | 本文がこれのどれかで始まれば「通知」 |
| `subagent_colors.running` / `.ended` | `110` / `245` | `claude-state subagents`（ステータス行のサブエージェントの要約）の色。`null` は色なし |

`notice` の既定は空で、他のセッションからのメッセージはどれも普通の依頼と同じに色を変えます。セッション同士で「返事も作業も要らない知らせ」の決まった言い回しを使うなら、それを書きます。日本語で連絡するチームの例（0.4 までの既定）:

```json
{"state": {"notice": {"contains": ["返事は不要", "返事不要"], "prefix": ["【通知】"]}}}
```

状態の色の既定（`colors`）:

| 状態 | `background` | `name` |
|---|---|---|
| `working`（作業中） | `#22282e` | `null` |
| `waiting`（待受中） | `#2e2a22` | `208` |
| `done`（完了・未読） | `#222b25` | `108` |
| `idle` | `null`（元の色） | `245` |

`colors` は書いた状態・書いた項目だけを既定に重ねます。止めるのは環境変数 `CLAUDE_KITTY_STATE=off`。

## lang（claude-lang-check: 返答の言語）

| 項目 | 既定 | 意味 |
|---|---|---|
| `enabled` | `true` | `false` で調べない |
| `language` | `null` | 返答に求める言語: `ja`・`zh`・`ko`。**決めていなければ何もしない**（hook を入れただけでは書き直させない） |
| `threshold` | `0.3` | 返答の頭の部分で、その言語の字の割合がこれ未満なら書き直させる（0〜1） |
| `head` | `200` | 割合を数える頭の字数 |
| `min` | `20` | これより短い返答は調べない |

hook は `tools/install.sh --hooks=lang` と明示したときだけ入ります（`--all` には入りません）。環境変数が優先します: `CLAUDE_LANG_CHECK=off`、`CLAUDE_LANG_CHECK_LANGUAGE`、`CLAUDE_LANG_CHECK_THRESHOLD`、`CLAUDE_LANG_CHECK_HEAD`、`CLAUDE_LANG_CHECK_MIN`。判定の詳細は [lang-check.md](lang-check.md)。

## guard_hook（claude-guard: 状態で働く安全装置）

`guard_hook.guards` に、ガードの名前（英数字と `_ . -`、`all` は不可）ごとに書きます。**既定ではガードは 1 つも無く、何も止めません。**

| 項目 | 既定 | 意味 |
|---|---|---|
| `enabled` | `true` | `false` でこのガードを使わない |
| `process` | なし | 働く条件: `pgrep -f` の正規表現（リスト可。どれか 1 つが動いていれば働く） |
| `command` | なし | 働く条件: 判定のコマンド（文字列は `sh -c`、リストはそのまま）。終了コード 0 なら働く |
| `command_timeout` | `2` | `command` の打ち切り（秒、30 まで）。時間切れは働かないと数える |
| `paths` | `[]` | 働いている間、書き込みを止めるパス（絶対パスか `~` で始める。その下も含む） |
| `commands` | `[]` | 働いている間、止める Bash のコマンドの形（文字列 = 語の並びの頭、`{"regex": …}`、`{"program": …, "block": […]}`）。`{"program"}` の項目には `allow_owner`（起動の記録のファイルの owner と同じ担当だけ通す。[guard-hook.md](guard-hook.md#起動した担当だけ通すallow_owner)）を足せる |
| `allow_commands` | `[]` | 例外として通すコマンドの形（書き方は `commands` と同じ） |
| `message` | `""` | 止めたときの理由の文に足す一文。`{name}`・`{reason}`・`{condition}`・`{what}` を差し込める |

働く条件は `process`・`command`・旗（`claude-guard on` / `acquire`）のどれか 1 つ。`paths` と `commands` のどちらかは要ります。書き方と判定の詳細は [guard-hook.md](guard-hook.md)。

## session_stats（claude-session-stats: 会話の記録の数字）

| 項目 | 既定 | 意味 |
|---|---|---|
| `master` | `""` | 統括のセッション名（`/rename` の名前）。空なら統括を分けない |
| `roles` | `[]` | 担当のセッション名。空なら、名前のある記録を全部その名前で数える |
| `role_dir_prefix` | `""` | 担当ごとのフォルダで claude を起動しているとき、記録のフォルダ名（ホームのフォルダ名に続く部分）の頭。空なら使わない |
| `rephrase` | `[]` | ユーザーの言い直しの数え方: `[key, 表に出す名前, 正規表現]` のリスト |
| `echo` | Claude Code のシステム文 1 つ | 返答の冒頭に写ってしまう文: `[key, 表に出す名前, 正規表現]` のリスト |
| `redact` | `[]` | 書き出しで伏せる語（ユーザー名とホームのフォルダ名は必ず伏せる） |
| `glossary` | `null` | 用語集の道具 `{"module": ".py", "file": "用語集", "scene": "reply"}`。`null` なら使わない（例でも `null`。写すときは実在のファイルを書く） |
| `projects` | `"~/.claude/projects"` | Claude Code の会話の記録の場所 |
| `home_project` | `""`（ホームのパスから作る） | 統括の記録があるフォルダの名前 |
| `out_dir` | `""`（`~/.local/share/claude-kitty/stats`） | 書き出すフォルダ |
| `notes` | `[]` | 書き出しの「数え方」の頭に足す文 |
| `timezone` | `""`（このマシンのローカルの時刻） | 日付の区切りのタイムゾーン（`"Asia/Tokyo"` など、IANA の名前） |

知らない項目があると読み込みで止まります（打ち間違いに気づくため）。詳細は [session-stats.md](session-stats.md)。

## roster（claude-roster: セッションの名簿）

| 項目 | 既定 | 意味 |
|---|---|---|
| `models` | `{}` | 役割 → モデル名（`"opus"` など）。開き直すときにこのモデルで開く |
| `ignore` | `[]` | 名簿に入れない役割 |
| `generations` | `5` | 残す名簿の世代の数（1〜50） |
| `keep_absent_hours` | `24` | ペインが無くなった役割を、自動の保存で名簿に残す時間 |

名簿の場所は `$CLAUDE_ROSTER_DIR`（既定 `~/.local/share/claude-kitty`）。詳細は [roster.md](roster.md)。

## relay（claude-relay: 端末をまたぐ連絡）

この節があると `claude-kitty doctor` が relay の行（sshd・鍵・相手・受け取りの登録・hook）を出す。`peers` は `claude-relay trust` / `untrust` が書き換える（手で書いてもよい）。詳細は [relay.md](relay.md)。

| 項目 | 既定 | 意味 |
|---|---|---|
| `enabled` | `true` | `false` で受け取りを全部止める（recv が拒否し、hook も渡さない）。送ることはできる |
| `name` | `null` | 自分の端末名（`claude-relay pair` が相手に渡す 1 行に使う）。`null` ならホスト名の頭 |
| `receivers` | `["left"]` | 受け取ってよい役割。`"left"` は設定ファイルの `view.left`（統括）の役割を指す（`$CLAUDE_VIEW_LEFT` は見ない。sshd から動く受け取りの側と食い違わないよう）。宛先を省いた送信は最初の役割へ |
| `max_bytes` | `16384` | 1 通の本文の上限（UTF-8 のバイト数。1〜1048576） |
| `rate` | `{"count": 30, "per_seconds": 600}` | 送り元ごとの通数の上限（`per_seconds` 秒に `count` 通） |
| `peers` | `{}` | 送る相手: 名前 → `{"host", "user", "port"}`（`port` の既定は 22）。名前は英数字と `. _ -`（64 字まで） |

## model_policy（claude-presence: 離席中のサブエージェントのモデル）

人がいないとき（離席中）の作業はゆっくりでよくトークンの少ない方を、人がいるときは呼び出しどおり（速さ優先）にする。**既定は無効**。使うには `enabled: true` と、`tools/install.sh --hooks=model`（Agent ツールの PreToolUse の hook。`--all` には入らない）が要る。人が打った時刻は claude-state の UserPromptSubmit の hook（`--hooks=state`）が記録する。

| 項目 | 既定 | 意味 |
|---|---|---|
| `enabled` | `false` | `true` で切り替える。`false` なら hook は何もしない |
| `away_minutes` | `20` | 最後に人が打ってからこれだけ（分。1〜10080）たったら離席とみなす |
| `away_model` | `"haiku"` | 離席中の実装のサブエージェントの `model`。Agent ツールの `model` に書ける別名（`haiku`・`sonnet`・`opus` など）を書く |
| `keep` | `["review", "レビュー", "評価", "verify", "検証", "調べ", "guide"]` | Agent の呼び出しの `description` か `prompt` にこのどれかがあれば変えない（レビュー・評価・調べもの）。英数字だけの語は単語として探す（大文字・小文字を区別しない。`guide` は `guideline` に当たらない）。ほかは部分一致。prompt の全体を見るので、実装の依頼に「検証してからコミット」などと書くと変わらない |

- 人の入力の記録: `<置き場>/claude-presence/last.json`（置き場は `$XDG_RUNTIME_DIR`、無ければ `/tmp/claude-kitty-<uid>`）。全セッション共通の 1 つで、時刻・セッション・役割（claude-state の記録の `role`）・cwd。人の入力でないものは書かない: 頭が `<task-notification`（背景の作業の完了）・`<agent-message`（サブエージェントの報告）・`<system-reminder` のもの、`<cross-session-message`（Claude Code のセッション間メッセージ）か `[relay-wake ids=`（claude-relay の起こす知らせ）を含むもの、`state.notice` に当たるもの、`CLAUDE_KITTY_STATE=off` のセッション、入れ子の claude（ツールから動かした `claude -p` など）。印の一覧は `lib/presence.py` の `MACHINE_PREFIXES`・`MACHINE_MARKS`（claude-kitty の道具が socket に直接送る文には、このどれかを入れる）。
- 止めるのは離席中の実装の呼び出しだけ。変えないもの（呼び出しどおりに通す）: 在席中、記録が無い（一度も人が打っていない）、`keep` の語を含む、`subagent_type` が `claude-code-guide`・`Explore`・`Plan`・`web-fetch`・`fork`、`model` が `fable` か既に `away_model`、設定が壊れている、hook の途中で何かが失敗した。
- 切り替えの仕方: 入力は書き換えない。PreToolUse の `hookSpecificOutput.permissionDecision: "deny"` で呼び出しを断り、`permissionDecisionReason` で「離席中なので model を `away_model` にして同じ呼び出しを出し直してください（ほかの項目はそのまま）」と頼む。Claude が `model: "haiku"` で出し直したものは、既に `away_model` なので通る。`updatedInput` で書き換えないのは、auto mode では hook が入力を変えると判定が「a hook changed this call's input after auto mode reviewed it」で無効になって断られ、同じ返答のほかの呼び出し（Bash など）まで「another call in this response was changed after auto mode reviewed it」で断られたため（実測）。deny は判定なので入力の書き換えにはならず、ほかの呼び出しを巻き込まない見込み（実機の auto mode ではまだ確かめていない）。
- 2 度は断らない: 同じセッションの同じ `prompt` を 300 秒以内に断っていたら、元の model のまま（または model を外して）出し直したものも通す（Claude が従わなかったとき・人が model を指定したときに、断りをくり返して作業を止めないため）。断りの文ではこのことを知らせない（毎回そう出し直されると切り替わらないため）。
- 記録: 断ったら `<置き場>/claude-presence/denials.jsonl` に、時刻・セッション・役割・元と新しい model・description・prompt の hash を残す。出し直しが `away_model` で通ったら `<置き場>/claude-presence/switches.jsonl` に、同じ項目と断った時刻（`denied_at`）を残す。切り替えの記録は、実際に軽い model で動いた呼び出しだけにするため、断ったときでなく出し直しが通ったときに書く（従わずに元の model で通ったもの、`prompt` を変えて出し直したものは載らない）。断った記録が書けないときは、2 度目を見分けられないので断らずに通す。
- `claude-presence [--json] [--recent N]` で、最後に人が打ってからの分数・在席/離席・どのセッションか・設定・hook・直近の切り替えを見る。`claude-kitty doctor` の「モデルの切り替え」の行が、有効か・hook が入っているかを出す。

## 設定ファイルの外のもの

| 環境変数 | 意味 |
|---|---|
| `CLAUDE_KITTY_KITTY` / `CLAUDE_KITTY_KITTEN` | kitty / kitten の場所（既定は公式インストーラの `~/.local/kitty.app/bin/`、無ければ PATH、macOS は `/Applications/kitty.app/Contents/MacOS/`） |
| `CLAUDE_KITTY_QDBUS` | KWin を呼ぶ qdbus の場所（既定は PATH の `qdbus`・`qdbus6`・`qdbus-qt6` の最初のもの） |
| `CLAUDE_KITTY_CLASS` / `CLAUDE_KITTY_START_AS` | `claude-kitty` が起動する kitty の窓のクラス（既定 `kitty-claude`。Linux だけ。macOS の kitty には `--class` が無い）と起動の形（既定は `kitty.maximize` で `maximized` か `normal`） |
| `CLAUDE_KITTY_UNSET` | `claude-kitty` が kitty を起動する前に消す環境変数を足す（空白区切りで、名前か `FOO_*` の形の接頭辞）。`CLAUDE*`（`CLAUDE_KITTY_*`・`CLAUDE_VIEW_*` を除く）と、呼んだ側の端末のもの（`CMUX_*`・`C11_*`・`GHOSTTY_*`・`TMUX` など）は既定で消す |
| `CLAUDE_KITTY_WAIT` | macOS で、起動した kitty がすぐに失敗していないかを見る秒数（既定 2） |
| `CLAUDE_KITTY_BIN_DIR` | `tools/install.sh` と `claude-kitty doctor` が見るコマンドのリンクの場所（既定 `~/.local/bin`） |
| `CLAUDE_CONFIG_DIR` | Claude Code の設定のフォルダ（既定 `~/.claude`。`settings.json` の hook をここで探す） |
| `CLAUDE_GUARD_ALLOW` | claude-guard のガードを名前で通す（[guard-hook.md](guard-hook.md#通す道)） |
| `CLAUDE_KITTY_LAUNCH_AGENTS` | macOS: `tools/install.sh --launchd` と `claude-kitty doctor` が見る LaunchAgent の場所（既定 `~/Library/LaunchAgents`） |
| `CLAUDE_BROWSER_MAC_TITLEBAR` | macOS: claude-browser が kitty の窓から差し引くタイトルバーの高さ（ポイント。既定は kitty.conf の `hide_window_decorations` が `titlebar-only` などなら 0、それ以外は 28） |
| `XDG_RUNTIME_DIR` | 道具の記録（claude-state・claude-browser・claude-wait など）と kitty の socket の置き場。無ければ（macOS など）記録は `/tmp/claude-kitty-<uid>`、socket は `/tmp/kitty-<pid>`（macOS は両方を探す） |
