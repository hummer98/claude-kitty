# claude-kitty

[![version](https://img.shields.io/github/v/tag/hummer98/claude-kitty?label=version&sort=semver)](CHANGELOG.md)
[![test](https://github.com/hummer98/claude-kitty/actions/workflows/test.yml/badge.svg)](https://github.com/hummer98/claude-kitty/actions/workflows/test.yml)

版の番号は [VERSION](VERSION)（各コマンドの `--version` も同じ）。変更の履歴は [CHANGELOG.md](CHANGELOG.md)（上げるときに手で行うことも版ごとに書いている）、issue と PR の出し方は [CONTRIBUTING.md](CONTRIBUTING.md)。
運用の約束（体制・注意の守り方・安全・レビュー・略号）は [PRACTICES.md](PRACTICES.md)。
複数の [Claude Code](https://docs.claude.com/en/docs/claude-code) セッションを、[kitty](https://sw.kovidgoyal.net/kitty/) の 1 つの窓にペインとして並べて動かすための小さな道具の集まり。

- 1 つのセッション（たとえば左の列の「統括」）が、ほかのセッションのペインを `kitten @`（kitty のリモート操作）で並べ替える。人がマウスで直さなくても、「この 2 人を右に出して」「全員を等分に」ができる。1 人で使う・並べるだけでもよい（体制の例は [PRACTICES.md](PRACTICES.md)）。
- ほかの端末で動いているセッションを、kitty のペインへ安全に移す（同じセッションを 2 か所で同時に開かない）。
- 各セッションが自分専用の Chrome を自分のペインの上に重ね、Web アプリの動作を確認する（プロファイルが残るのでログインは一度だけ）。

作者の環境は Linux（KDE Plasma 6 Wayland）。窓の操作に KWin スクリプトを使う部分は Linux/KDE 専用で、kitty と `kitten @` の部分は OS を問わない。macOS は試験的に対応している（[下の節](#macos試験的)）。セットアップの手順は [SETUP.md](SETUP.md)。

## 最短の導入

前提は kitty・Python 3.9 以降・Claude Code（[SETUP.md の 1.](SETUP.md#1-前提)）。macOS は [下の節](#macos試験的) の手順から。

```sh
git clone https://github.com/hummer98/claude-kitty ~/claude-kitty
~/claude-kitty/tools/install.sh --all     # 何をするかを見せ、確認してから入れる（--dry-run で見るだけ）
claude-kitty doctor                       # 足りないものと直し方を 1 行ずつ出す（NG が無ければ終了コード 0）
```

- `install.sh` が行うこと: `bin/` のコマンドを `~/.local/bin` にリンク、`~/.claude/settings.json` に hook（`claude-state`・`claude-guard`。返答の言語を調べる `claude-lang-check` は `--hooks=lang` と明示したときだけ）を足す、kitty.conf の末尾に `include …/kitty/claude-kitty.conf`（macOS は `claude-kitty-macos.conf`）の 1 行を足す、常駐を入れる（Linux は systemd のユーザーユニット、macOS は launchd の LaunchAgent）。部品ごとに `--links` `--hooks[=state,lang,guard]` `--kitty` `--systemd` `--launchd` で選べる。hook の command はリポジトリの `bin/` の絶対パスで書く（PATH に頼らない）。
- 既にあるものは壊さない: 別のファイルは上書きせず、既存の hook とキーは残し、同じ hook は二重に足さない。書き換える設定は `<名前>.bak-<日時>` に残す。何度流しても同じで、`--uninstall` で同じ部品を外せる。
- kitty の設定（リモート操作とソケット）は kitty を起動し直してから効く。そのあと `claude-kitty` で kitty を開き、`claude-kitty doctor` をもう一度。
- 役割名などの設定は `~/.config/claude-kitty/config.json`（無くても既定値で動く）。全項目の例は [config.example.json](config.example.json)、意味と既定値は [docs/config.md](docs/config.md)。
- 手作業で入れる手順と、各部品の中身は [SETUP.md](SETUP.md)。

| 道具 | 何をするか | OS 依存 |
|---|---|---|
| `bin/claude-kitty` | kitty を `CLAUDE_*` 環境変数なしで起動し、セッションファイルどおりにペインを開く | 仮想デスクトップへの移動と最大化だけ KWin（macOS では行わない） |
| `bin/claude-view` | 統括用。ペインを役割名で並べ替える（等分割にそろえる） | 「保護する窓が前面か」の確認だけ KWin（macOS は `lsappinfo`） |
| `bin/claude-wait` | 別の場所で動いている同じセッションの終了を待ってから resume する | プロセスの情報（`lib/proc.py`。Linux は `/proc`、macOS は `sysctl`） |
| `bin/claude-browser` | ペインごとの Chrome を、そのペインに重ねる | 窓の配置は KWin（macOS は試験的に CoreGraphics と CDP）。Chrome はネイティブでも Flatpak でもよい |
| `bin/claude-state` | セッションの状態（作業中・待受中・完了（未読）・idle）をペインの背景色とステータス行の色で見せる（Claude Code の hook から呼ぶ）。`claude-state watch` は kitty の外の常駐で、見た完了のペインを idle に戻し、消えた色を塗り直す（[下の節](#claude-state状態の色)） | 入れ子の claude の判定にプロセスの情報（`lib/proc.py`） |
| `bin/claude-show` | 見てほしい HTML（他のペインの場所に縦 100%）・画像（自分のペインの上）を開く（[下の節](#claude-show見てほしいものを開く)） | 保護する窓の確認だけ KWin（macOS は `lsappinfo`）。kitty の外では `xdg-open`（macOS は `open`） |
| `bin/claude-guard` | PreToolUse hook。名前の付いたガードが働いている間、決めたパスへの書き込みと決めたコマンドを止める（[docs/guard-hook.md](docs/guard-hook.md)） | 働く条件の `pgrep` |
| `bin/claude-lang-check` | Stop hook で最後の返答の言語を調べ、設定の `lang.language` で決めた言語でなければ書き直させる（決めていなければ何もしない。[docs/lang-check.md](docs/lang-check.md)） | なし |
| `bin/claude-session-stats` | 会話の記録から、言い直し・英語の返答・返答の長さなどを期間ごとに数えて Markdown に書く（[docs/session-stats.md](docs/session-stats.md)） | なし |
| `bin/claude-roster` | kitty の中のセッションの名簿を保ち、kitty を起動し直したあとに元の配置・フォルダ・モデル・会話へ戻す（[docs/roster.md](docs/roster.md)） | プロセスの情報（`lib/proc.py`）、保護する窓の確認だけ KWin |

テスト: `python3 tests/run.py`（これ 1 つで全部。標準ライブラリだけで、kitty や Chrome は使わない。GitHub Actions も同じものを回す）

## macOS（試験的）

作者は macOS の実機で動かしていない（macOS の分岐は、Linux の上で OS の部分を偽物にした単体テストで確認している）。詳しい手順・確認してほしいことは [SETUP.md の macOS での導入](SETUP.md#macos-での導入)。

```sh
xcode-select --install                                   # git と python3（3.9）
# Homebrew を https://brew.sh の手順で入れてから
brew install --cask kitty google-chrome && brew install node
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zprofile
echo 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"' >> ~/.bash_profile   # セッションのペインは bash -lic で開く
git clone https://github.com/hummer98/claude-kitty ~/claude-kitty
~/claude-kitty/tools/install.sh --all     # リンク・hook・kitty.conf・launchd の常駐
# 動いている kitty を ⌘Q で全部終わらせ、ターミナル.app から
claude-kitty                              # kitty を開く
claude-kitty doctor                       # kitty のペインで。NG が無ければ OK
```

| 使える | 試験的（動くはずだが未確認） | 使えない |
|---|---|---|
| `claude-view` の並べ替え、`claude-kitty` の起動、`claude-browser` の open・shot・url・close、`claude-show` の画像、`claude-guard`・`claude-lang-check`・`claude-session-stats`、`claude-kitty doctor` | `claude-state` の色と `claude-state watch`（launchd）、`claude-wait`、`claude-roster`、`view.guard`（`lsappinfo`）、`claude-browser` の重ね置き（CoreGraphics + CDP。「常に最前面」は無く、前に出すのは `--focus browser` のときだけ。置けなければ普通の窓で開く） | 仮想デスクトップへの移動・最大化（`kitty.desktop`・`kitty.maximize`）、窓の「常に最前面」、systemd のユニット（代わりに launchd） |

## claude-kitty

`claude-kitty [session]` — kitty を CLAUDE_* 環境変数なしで起動し（Claude Code の中から起動すると、中の claude が子セッション扱いになり会話記録が残らなくなるのを避ける）。設定の `kitty.maximize` で最大化し、`kitty.desktop` で KDE Plasma の仮想デスクトップへ移す（どちらも既定はしない。[docs/config.md](docs/config.md#kittyclaude-kitty-kitty-の起動)）。既定の配置は `kitty/claude3.session`（左 1/4 + 右上下）。

- `claude-kitty doctor [--json]`: 入っているもの・動くもの（kitty の版とリモート操作、`layout_state.pairs`、python3・node、コマンドのリンク、hook とその道具が見つかるか、kitty.conf の watcher、systemd（macOS は launchd）、設定ファイル、KWin（macOS は `lsappinfo`）、Chrome）を 1 行ずつ調べ、足りなければ直し方を出す。読むだけ。NG があれば終了コード 1。
- `kitty/example.session`: 統括 1 + 担当 5 の例（各ペインが `claude-wait` で待ってから resume する）。手元の版は `kitty/<名前>.session` に置けば git に入らない（`.gitignore`）。

## claude-view（統括用の配置）

`claude-view all | focus ROLE | pair A B | restore | add ROLE [--row N] | remove ROLE | swap A B | equalize | rows "a,b/c" | status`

- ペインは kitty の user var `role`（セッションファイルの `--var role=...`）で呼ぶ。
- 左の列（`--left`、既定は設定の `view.left`、無ければ `main`）は固定。毎回いったん左以外を standby タブへ下げてから組み直し、最後に入力先を左へ戻す。
- どの操作も等分割にそろえる（段ごとに幅を等しく、左の列も 1 列分）。
- `restore` は設定の `view.restore`（既定は右上 `dev-a`、右下 `dev-b`）。
- 設定の `view.guard`（プロセスの正規表現）に当たる窓（全画面のアプリや発表中の画面など、キーやフォーカスを奪われたくない窓）が前面なら何もせず通知だけ（`--force` で実行）。close 系と send-text は使わない。
- ペインの位置は `kitten @ ls` の `layout_state.pairs`（分割の木）から計算する。`neighbors` の番号は window id ではなく group id で、ペインを移すとずれる。
- `--dry-run` は kitty に `ls` だけして、行う操作を表示する。

### 設定

`~/.config/claude-kitty/config.json`（`$CLAUDE_KITTY_CONFIG` か `$XDG_CONFIG_HOME` で場所を変えられる。リポジトリの外に置く）:

```json
{
  "view": {
    "left": "main",
    "restore": [["dev-a"], ["dev-b"]],
    "guard": ["^\\./fullscreen-app"]
  }
}
```

優先順は 引数 > 環境変数（`CLAUDE_VIEW_LEFT`、`CLAUDE_VIEW_RESTORE="a,b/c"`、`CLAUDE_VIEW_GUARD`。`none` で guard を止める。空文字は未設定と同じ）> 設定ファイル > 既定値。kitten の場所は `CLAUDE_KITTY_KITTEN`、kitty の場所は `CLAUDE_KITTY_KITTY` で変えられる。

## claude-wait

`claude-wait <名前> <セッションID> [--rename]` — 同じセッションが別の場所で動いている間は待ち、プロセスが確実に終わってから resume する（JSON の一瞬の空・後片付け中の削除・/clear・pid の再利用・同じ ID の二重待機に対応）。
- 待つ相手の JSON の `kind` が `"bg"`（daemon の中の背景セッション）なら、前面の claude を閉じても終わらないので、その旨と pid を表示する（自動では止めない。idle を確認してから人が止める）。
- `--rename` の `-n` は resume では効かないので、再開の直前に「再開したら /rename <名前> と打ってください」と表示する。

## claude-browser

ペインごとに独立した Chrome（専用プロファイル・専用 CDP ポート）を kitty のペインに「常に最前面」で重ねる。kitty は縮めない（縮めると全ペインの claude-code が再描画で崩れることがある）。

- プロファイル名は起動し直しても変わらないもの: `--key` > ペインの user var `role` > `slot` > セッション ID。ログインが次回も残る。
- 起動する Chrome は設定の `browser.command`、無ければ自動で探す: プロファイルの置き場が既にあるもの > ネイティブの Chrome（`google-chrome-stable`・`google-chrome`）> Flatpak の Chrome（`com.google.Chrome`）> ネイティブの Chromium（`chromium`・`chromium-browser`）> Flatpak の Chromium。どれを使うかは `status` と `claude-kitty doctor` に出る（[docs/config.md](docs/config.md#browserclaude-browserclaude-show-重ねる-chrome)）。
- プロファイルと台帳 `ledger.json`（`profiles` で表示、`note TEXT` でメモ）の置き場: Flatpak なら `~/.var/app/<アプリ ID>/claude-profiles`、それ以外は `~/.local/share/claude-kitty/browser-profiles`（`browser.profile_root` で変える）。
- 既存の Chrome はプロセスの引数（`--user-data-dir` と `--remote-debugging-port`）から見つけるので、state が消えても引き継ぎ・close できる。
- `open URL [--mode self|show] [--ratio R] [--area pane|others|right] [--over SLOT|ID] [--focus kitty|browser|none]`
  - self（既定）: 自分だけで確認するとき。自分のペイン全体を覆う（他のペインの邪魔をしない）。既定の area は `pane`
  - show: ユーザーに見せるとき。既定の area は `others` で、自分以外のペインの場所に縦 100% で開く（自分の会話は見せたまま）
  - `--area others`（show の既定）: 左の列（`view.left` の役割のペイン。いなければ分割の根の左側。左右に分かれていなければタブ全体）を除いた右側のうち、自分のペインの横の範囲を除いた左右の隙間の広い方を、右側の全高で使う。広い方でも右側の幅の 30% 未満なら（自分が右側をほぼ横断している）、右側を左右半分に分けて自分の中心が無い方を使う。自分が左の列にいれば右側全体。避けるのは常に呼び出した自分のペインで、`--over` の先ではない
  - `--area pane`: そのペイン（`--over` の先、既定は自分）。show と組み合わせると、前からのペインの上 ratio だけ覆う動き
  - `--area right`: 左の列を除いた kitty の右側全体
  - `--ratio`: show のとき、範囲の上から何割を覆うか（0.1〜1.0）。省いたときは others なら 1.0（全高）、pane・right なら 0.65
  - `--over`: 別のペイン（slot 名 left/right-top/right-bottom か window id）に重ねる。`--over` を指定して area を省くと `pane`（show でもその先のペインの上）
  - `--focus none`: 入力先を変えない（全画面のアプリを使っている最中など）
- `open` を呼び直しても、未指定の mode/over は前回の値を引き継ぐ（ペインの上の show のあとの open で、会話を隠す self に戻らないように）。ただし前回が他のペインの場所（`others`・`right`。claude-show の HTML など）なら mode は引き継がず self に戻す（そのあとの自分の作業用の `open` が他のペインを覆わないように）。focus と area は一回ごとの指定で、引き継がない（area は mode の既定に戻る）。ratio は前回と同じ area になったときだけ引き継ぐ（area が違えば「上から何割」の基準が違う）。
- 前回の over を引き継ぐのは、area が `pane` になったときだけ（`others`・`right` は自分のペインと左の列で決まり、前回の over は使わない）。引き継いだ over の先が見えていないタブに移った・もう無いときは、止めずに自分のペインに重ね、stderr に 1 行で知らせる（以後の状態も自分のペイン）。`--over` を明示したときは、先が見えないタブにあればエラーで止まる。
- `move` で mode も area も省けば、前回の area と ratio のまま置き直す。`--mode` を指定したときの area はその mode の既定。
- `move …` / `close` / `shot FILE`（CDP）/ `url` / `status` / `list` / `profiles` / `note TEXT`
- タブの切り替えや分割比の変更には追従しないので、そのときは `move` を呼ぶ。
- 置いたあと 0.4 秒待って実際の窓の矩形を KWin から読み、指定とずれていれば直す（1 度目は同じ矩形を置き直す: 最大化の解除などで Chrome が後から前の大きさに戻すため。それでもずれていれば、各辺 64px 以内のずれだけを一定のずれ（窓の装飾の分など）として打ち消し、悪くなったら指定の矩形に戻す）。最大化の解除は、最大化しているときだけ行う。直らなければ stderr に指定と実際を出す。
- Flatpak の Chrome はホームの任意の場所を読めない。file:// で手元の資料を開くなら `flatpak override --user --filesystem=~/docs:ro com.google.Chrome` のように許可する。
- 返り値の `cdp` URL に Playwright の `connectOverCDP` などで接続して操作できる。
- プロファイルは置き場の `<key>` に残るので、パスキー等のログインは一度人が済ませれば以後使える。
- 窓の配置は KDE Plasma の KWin（`qdbus`・`qdbus6`・`qdbus-qt6` のどれか）。Linux で KWin が無ければ、何が足りないかを出して止まる。macOS は試験的に、kitty の窓の位置を CoreGraphics で読み、Chrome の窓を CDP で置く（「常に最前面」は無い。置けなければ止めずに普通の窓のまま開く。[SETUP.md](SETUP.md#macos-で使えるもの試験的なもの使えないもの)）。

## claude-state（状態の色）

Claude Code の hook から呼び、セッションの状態をペインの背景色で見せる。目立たせるのは待受中だけ。フォーカスは動かさない。
「作業中」「終わった」「何かを待っている」を、サブエージェントも含めて見分ける。

ペインの状態は、上から順に当てはまるもの:

| 状態 | いつ | ペインの背景（既定） | ステータス行の名前（既定） |
|---|---|---|---|
| waiting（待受中） | 本体かサブエージェントのどれか 1 つでも、許可（PermissionRequest）・質問（AskUserQuestion）・ダイアログ（Notification）・`claude-state waiting` で待っている。待ちを 1 つずつ数え、全部片付くまで消さない | 少し暖色 `#2e2a22` | オレンジ（256 色の 208） |
| working（作業中） | 待ちは無いが、本体かサブエージェントのどれかが動いている（本体の Stop のあとでも、背景のサブエージェントが動いていれば） | ごく少し青み `#22282e` | そのまま |
| done（完了・未読） | 全部終わり、そのペインをまだ見ていない | ごく薄い緑 `#222b25` | 控えめな緑（108） |
| idle | done のペインにフォーカスが来て 2 秒続いたあと（Stop の時点で見ていれば、すぐ） | 元の色（kitty.conf の `background`） | 薄い灰色（245） |

数え方（hook の入力は Claude Code 2.1.328 で記録して確認した）:

- 待ち: 許可は PermissionRequest で足し、同じエージェント・同じツール名・同じ入力の PostToolUse / PostToolUseFailure で外す（PermissionRequest には tool_use_id が無いため）。質問は tool_use_id で照合する。手の `claude-state waiting` は次の UserPromptSubmit まで残る。UserPromptSubmit では本体の待ちを外す（手の待ちは、人が打ったときだけ。背景の作業の完了の通知や他のセッションからのメッセージでも UserPromptSubmit は来るため）。サブエージェントの待ちは、そのエージェントの hook で外す。本体の Stop は本体の待ちと、もう動いていないサブエージェントの待ちを、SubagentStop はそのサブエージェントの待ちを外す。
- サブエージェント: SubagentStart で足し、SubagentStop で外す（入力の `agent_id`）。本体の Stop の `background_tasks` に合わせ直すので、SubagentStop を取りこぼしても残らない。数えるのは subagent・workflow・teammate・cloud session で、shell と monitor は数えない（`tail -f` などがずっと作業中に見えてしまうため）。
- 本体: UserPromptSubmit で動き、Stop / StopFailure で止まる。Stop のあとにツールの hook が来たら（背景の作業の完了で本体が自分で再開したときなど）また動いているとする。自動の compact（SessionStart の `source` が `compact`）では状態を変えない。
- 他のセッションからのメッセージで、本文に「通知」の印（設定 `state.notice`。既定は空で、何も通知とみなさない。例は「返事は不要」を含む・「【通知】」で始まる。[docs/config.md](docs/config.md)）があるターンは、色を変えない（作業中にも見せず、終わっても done にしない）。UserPromptSubmit の `prompt` の `<cross-session-message>` で見分け、分からなければ Stop のときに会話記録（`transcript_path`）の、そのターンを始めた記録（`turnOrigin` が `peer` で、`origin.body` が本文）で確認して、ターンの前の状態に戻す。印の無い依頼は、ふつうどおり終われば done。通知のターンの中で始まったサブエージェントは、ふつうどおり作業中に見せる（動いているものは見せる）。

既知の制限:

- 許可を断る（No）か Esc で止めると、Stop も PostToolUseFailure も来ない（実物で確認した。入力欄のまま 60 秒の `idle_prompt` も来なかった）。待受中のまま残り、次に打ったとき（UserPromptSubmit）に消える。`idle_prompt` が来れば、そこで本体を止まったことにする。
- 許可に答えてから長いツールが終わるまでは、待受中のまま（答えたことを知らせる hook が無い）。並行する別のツールの終わりでは外さない。同じエージェントが 2 秒より後に別のツールを呼び始めたら、断られたと見なして外す。
- 終わった背景のサブエージェントの記録は、本体が TaskStop で止めるか、そのセッションの記録が消えるまで残る（ステータス行の要約用。新しい 50 件まで）。
- Stop の時点で `kitten @ ls` に失敗すると、見ているペインでも done になる（watch が、done になってから 2 秒後に消す）。
- 同じセッションの hook は 1 つずつ書く。ロックを 5 秒で取れなければ、そのイベントを捨てて `errors.log` に残す。
- タブの題を明示していないタブ（kitty が中の題を出しているタブ）には「●」を付けられない（`set-tab-title` で題を固定してしまうため）。
- 「●」の付け外しのロックを 2 秒で取れなければ、その回は付け外ししない（次の変化で直る）。

使い方:

- `claude-state hook EVENT`: hook 用。stdin の JSON（`session_id` など）を読む。何が起きても exit 0 で、stdout には何も出さない（UserPromptSubmit では stdout が会話に入る。PermissionRequest では何も出さない = 許可の判断をしない）。kitten の呼び出しは 2 秒で打ち切る。エラーは `$XDG_RUNTIME_DIR/claude-state/errors.log` にだけ残す。同じセッションの hook と watcher は `.<session_id>.lock` で 1 つずつ書く。
- 「見ている」は、kitty の OS 窓が前面で、そのタブが表示中で、そのペインが入力先のとき（`kitten @ ls` の OS 窓・タブ・ペインの `is_focused` / `is_active`）。別のアプリや別のデスクトップを見ているときの Stop は done。
- done → idle は常駐の `claude-state watch`（lib/statewatch.py。systemd のユーザーサービス `contrib/systemd/claude-state-watch.service`）が行う。1 秒ごとに全部の kitty（`$XDG_RUNTIME_DIR/kitty-*` の、kitty がまだ動いているもの。設定 `state.watch_sockets` か `--to` で決めることもできる）の ls を読み、前面の OS 窓（`is_focused`）の表示中のタブ（`is_active`）の入力先のペイン（`is_focused`）が done で、そこへのフォーカスが `state.done_dwell_seconds`（既定 2 秒）続いたら idle にして背景を戻す。数え始めは、そのペインにフォーカスが来た時と done になった時の遅い方。途中で別のペインへ動いたり、kitty が前面でなくなったり、ls に失敗したりしたら数え直す。
  - watch が要る理由: kitty の watcher（`kitty/claude_state_watcher.py`、kitty.conf の `watcher`）は、kitty がペインを作るときに写し取られるので、動いている kitty で `load-config` しても前からあるペインには付かない。`on_tab_bar_dirty` の経路でも効くはずだったが、実物では前面の done のペインに 3.5 秒フォーカスしても消えなかった。kitty を再起動すると全セッションが落ちるので、kitty の外で同じ規則を行う。watcher は残す（新しいペインと再起動後の kitty では、こちらも効く）。
  - 二重に動いても害は無い: どちらも同じ `mark_seen`（watcher のもの）で、同じ `.<session_id>.lock` を待たずに取る。先に片方が idle にすれば、もう片方は done でないので何もしない。ロックが取れない（hook が書いている最中）回は見送り、watch は次の回にやり直す。
  - kitty が前面かは、ls の OS 窓の `is_focused` で決める（KWin で別のアプリ（全画面のアプリ）が前面のとき、全部の kitty で false になるのを確認した）。KWin は呼ばない。
  - ls と get-colors は kitten を起こさず、kitty の socket にリモート操作の文を直接書いて読む（ls 1 回は kitty 側で約 15 ms、get-colors は約 1 ms）。色を変える set-colors だけ kitten で。エラーは `errors.log` に残して続ける（ls の失敗は続くあいだ 1 回だけ）。落ちても systemd が起こし直す（`Restart=always`）。
  - 色の確認: kitty が設定を読み直すと全ペインの色が消える。watch は 1 秒ごとに、記録のある各ペインの色を get-colors で見て（1 回約 1 ms）、記録の `background`（hook が書いた色。watch が起動時に読んだ設定ではない）と 2 回続けて違ったペインを、記録の色で塗り直す。1 回だけの違いは、hook が記録を書いてから set-colors するまでの間かもしれないので待つ。set-colors の失敗や、idle にした直後に hook が割り込んだときの食い違いも、これで直る。手で `set-colors` した色も、記録のあるペインなら 2 秒ほどで戻される。
  - 制限: 1 秒ごとに見るので、1 秒より短くよそへ移ってすぐ戻ったのは、離れたと気づかないことがある。消えるのは、続けて 2 秒見てから最大 1 秒後。設定（`state.watch_sockets`）は起動時にだけ読む（変えたら `systemctl --user restart claude-state-watch`、macOS は `launchctl kickstart -k gui/$(id -u)/claude-kitty.state-watch`）。`watch_sockets` や `--to` で固定のパスを使い、kitty を起動し直すと、ペインの id が振り直されて古い記録に当たることがある（既定の `kitty-<pid>` では起きない）。
- `claude-state watch [--to SOCKET]... [--interval 1] [--once]`: 上の常駐。`--once` は 1 回だけ見て終わる（数え始めるだけなので、ふつうは何も変えない。socket に届くかの確認用）。
- `claude-state waiting`: ふつうの文で問いを残したときに担当が手で呼ぶ。Stop でも done に変わらず、次の UserPromptSubmit で working に戻る。
- `claude-state list [--json] [--all]`: 統括用。1 行 1 セッションで role・状態・続いている時間（waiting・done・working・idle の順）。`--all` は claude が動いていない古い記録も出す。
- `claude-state get [SESSION_ID]` / `claude-state paint SESSION_ID TEXT`: ステータス行用（状態の 1 語 / TEXT を状態の色で）。SESSION_ID を省くと `$CLAUDE_CODE_SESSION_ID`。
- `claude-state marks [--clear]`: タブの題の「●」を付け直す（ふつうは hook と claude-view が行う）。
- `claude-state subagents [SESSION_ID]`: ステータス行用。`サブ: 動作 N ／ 終了済み M（最古 X日）` の 1 区切り（動いているものは控えめな青、終わったまま Claude Code の一覧に残っている背景のものは灰色で、いちばん古いものが終わってからの時間）。どちらも 0 なら何も出さない。色は `state.subagent_colors`（`{"running": 110, "ended": 245}`、null で色なし）。終了済みは、本体が TaskStop で止めたら外し、それ以外はセッションの記録が続く限り残す（Claude Code の一覧から消えたことを知る hook が無いため）。
- `claude-state repaint [--to SOCKET]`: 記録から全ペインの背景を塗り直す（watcher の無い kitty で設定を読み直したあとなど）。
- 状態は `$XDG_RUNTIME_DIR/claude-state/<session_id>.json`（state・waits・agents・window・socket・role・pid など）。同じペインで新しいセッションが始まると古い記録は消す。別のペインで resume したら、前のペインを使う記録が無ければ前のペインの色を戻す。
- main（統括のいるタブ。統括がいない kitty では表示中のタブ）以外のタブに待受中のペインがいれば（done には付けない）、そのタブの題の頭に「● 」を付け、いなくなれば外す。claude-view は並べ替えの前に外し（standby タブを題で探すため）、あとで付け直す。
- kitty の外（`KITTY_WINDOW_ID` が無い）、`CLAUDE_KITTY_STATE=off`、ペインの claude が Bash ツールで起動した入れ子の claude（`claude -p` など。起動時の環境の `CLAUDECODE` か `CLAUDE_CODE_CHILD_SESSION` が空でない）では何もしない。
- 使う `kitten @` は ls・set-colors・set-tab-title だけ（watch は ls と get-colors を socket で直接読む。どれも読むだけ）。`set-colors --reset` は全ペインに効いてしまうので使わず、元の色は `background=` で指定して戻す。

設定（`~/.config/claude-kitty/config.json`、どれも省ける）:

```json
{
  "state": {
    "base_background": "#232627",
    "colors": {
      "working": {"background": "#22282e", "name": null},
      "waiting": {"background": "#2e2a22", "name": 208},
      "done":    {"background": "#222b25", "name": 108},
      "idle":    {"background": null,      "name": 245}
    },
    "done_dwell_seconds": 2,
    "watch_sockets": null,
    "notice": {"contains": ["返事は不要", "返事不要"], "prefix": ["【通知】"]}
  }
}
```
（`notice` は例。既定は空）

`base_background` を省くと kitty.conf（`$KITTY_CONFIG_DIRECTORY` か `~/.config/kitty/kitty.conf`）の `background`、それも無ければ kitty の既定の `#000000`。`background: null` は元の色、`name: null` は色を付けない。hook とステータス行の設定は [SETUP.md](SETUP.md#5-状態の色claude-state)。

## claude-show（見てほしいものを開く）

`claude-show FILE_OR_URL [--ratio 0.6]` — 見てほしいものを開く。結果は 1 行の JSON。

- HTML（`.html`/`.htm`）と URL: `claude-browser open <file:// か URL> --area others --mode show --focus none`（左の列を除いた右側のうち、自分のペインの横を避けた広い方を縦 100% で覆い、会話は見せたまま。入力先は動かさない。`--ratio` を付ければその場所の上から何割）。Flatpak の Chrome から見える場所に置くこと（claude-browser の項）。
- 画像（png/jpg/jpeg/webp/gif/svg）: 自分のペインに kitty の overlay を重ねて `kitten icat --hold` で表示する。閉じるには、overlay をクリックしてからキーを押す。svg などは ImageMagick があれば表示できる。
- 入力先は変えない（overlay も常に `--keep-focus`。ユーザーが打っている途中の文字が overlay に吸われないように）。自分のペインが表示されていないタブにあれば、その旨を `note` に書く。
- 保護する窓（`view.guard`）が前面なら何もしないで exit 2（前面の窓を確認できないときも同じ）。
- kitty の外では `xdg-open`（macOS は `open`）に渡す。
- overlay は重ねた先のペインと同じ window group に入るので、claude-view は並べ替えの対象に数えない。

## ライセンス

MIT（[LICENSE](LICENSE)）
