# 変更履歴

このプロジェクトの変更を版ごとに書く。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、版の番号は [Semantic Versioning](https://semver.org/lang/ja/) に従う（1.0 までは 0.x で、小さい方の数字の上がりにも互換の無い変更が入りうる）。

各版の節は「追加」「変更」「修正」と、**上げるときに手で行うこと**（`git pull` だけでは済まない設定の変更）に分ける。GitHub の release の本文は、タグを push したときにこのファイルの該当の節から作る（`tools/changelog_section.py`）。

## [Unreleased]

### 追加

- `tools/install.sh`: clone したあと 1 行で入れる（Linux と macOS。POSIX sh と python3 だけ）。部品は `--links`（`bin/` のコマンドを `~/.local/bin` へ）・`--hooks[=state,lang,guard]`（`~/.claude/settings.json` に hook）・`--kitty`（kitty.conf に include の 1 行と watcher のリンク）・`--systemd`（ユーザーユニットを写して enable、Linux だけ。macOS は launchd の案内）・`--all`。
  - まず何をするかを見せ、確認してから行う（`--yes` で省く、`--dry-run` で見るだけ）。何度流しても同じ結果で、`--uninstall` で同じ部品を外せる。
  - 既にあるものは壊さない: 別のファイルは上書きしない、既存のキーと hook は残す、同じ道具・同じ引数の hook は（パスの書き方が違っても）二重に足さない、手で入れた中身の違うユニットは上書きしない。書き換える `settings.json` と kitty.conf は `<名前>.bak-<日時>` を作ってから一時ファイル経由で置き換える。`settings.json` が JSON として読めなければ、どの部品も行わずに止める。
- `tools/merge_hooks.py`: settings.json の hook を足す・外す・調べる（install.sh と doctor が使う）。
- `claude-kitty doctor [--json]`: kitty の版・`kitten @ ls`（allow_remote_control と listen_on）・`layout_state.pairs`・python3 と node の版・コマンドのリンク・どの hook が入っているか・kitty.conf の watcher と filter_notification・systemd のサービス・設定ファイルの各節が読めるか・KWin・Chrome を 1 行ずつ調べ、足りなければ直し方を出す。読むだけ。NG があれば終了コード 1。
- `kitty/claude-kitty.conf`（と macOS 用の `claude-kitty-macos.conf`）: claude-kitty に要る kitty の設定だけ（リモート操作・ソケット・splits・通知・watcher）。自分の kitty.conf から include する。
- `config.example.json`（全部の節・全部の項目の例）と [docs/config.md](docs/config.md)（各項目の意味と既定値）。
- README の冒頭に「最短の導入」（clone → `tools/install.sh --all` → `claude-kitty doctor`）。手作業の手順は SETUP.md に残した。SETUP.md に macOS の launchd の例。

### 変更

- `claude-roster`: 設定の `roster` 節の読み込みを `lib/config.py` の `load_roster_config` に移した（doctor とテストから同じ関数で読むため。動きは同じ）。

### 修正

- `claude-guard`: 区切りを引用したヒアドキュメント（`cat >> README.md <<'EOF' … EOF`）の本文に、止める対象のコマンドの名前が書いてあるだけで止めていた。引用した区切りを見落として本文をコマンドとして読んでいたため。引用符の中（`echo` や `printf` の引数、`git commit -m` の値）とヒアドキュメントの本文は文字列として扱い、コマンドとみない。
  - bash が実際に動かすものは今までどおり読む: `bash -c` / `sh -c` の中身、二重引用符の中と区切りを引用しない `<<EOF` の本文の `$(...)` と `` `...` ``（二重引用符の中の置換は、前の版では読んでいなかった）。
  - 複数行の二重引用符の中の `<<` を、ヒアドキュメントの始まりと取り違えて後ろの行を読み飛ばすことがあった（止めるべきコマンドを見逃しうる）のも直した。
  - 本文を stdin から動かすもの（`bash <<'EOF'`・`sh -s <<EOF`・`ssh host <<'EOF'`）は、本文を `bash -c` の中身と同じに読む。算術式の `<<`（`$((1<<2))`）をヒアドキュメントの始まりとみて、後ろの行を読み飛ばさない。
- `claude-state watch`（と doctor）: kitty のソケットの持ち主が生きているかを `/proc` で見ていたため、macOS では動いている kitty を見つけられなかった。signal 0 で確かめる。

### 上げるときに手で行うこと

0.2.0 から上げるとき。`git pull` のあと、次の 1 行で足りる（もう入っているものはそのまま）。

1. `tools/install.sh --all --dry-run` で何が足されるかを見て、`tools/install.sh --all` を流す。手で入れた hook・リンク・ユニットはそのまま「入っている」と数え、足りないものだけを足す（0.2.0 の手順で `claude-lang-check` を hook に絶対パスで入れ、`~/.local/bin` にリンクしていなければ、そのリンクが足される）。
2. `claude-kitty doctor` で NG が無いことを確かめる。
3. kitty.conf を自分のものにしている（リポジトリの `kitty/kitty.conf` へのリンクでない）なら、install.sh が足した include を読ませるために kitty を起動し直す（`allow_remote_control` と `listen_on` は起動時にだけ読まれる）。

## [0.2.0] - 2026-11-20

### 追加

- `claude-state`: セッションの状態をペインの背景色とステータス行の名前の色で見せる（Claude Code の hook から呼ぶ）。
  - 状態は 4 つ: 作業中（ごく少し青）・待受中（暖色。目立たせるのはこれだけ）・完了（未読。ごく薄い緑）・idle（元の色）。完了は、ペインにフォーカスが 2 秒（`state.done_dwell_seconds`）続いたら idle に戻る。
  - 許可・質問の待ちを id ごとに、サブエージェントを SubagentStart / SubagentStop と Stop の `background_tasks` から数え、「どれかが待っている > どれかが動いている > 完了 > idle」の順で 1 か所で決める。背景のサブエージェントが動いている間は、Stop のあとも作業中のまま。
  - 通知の印（`state.notice`。「【通知】」「返事は不要」など）の付いた、他のセッションからのメッセージで始まったターンは、前の色を保つ（青に光って緑で終わらない）。
  - main 以外のタブに待受中のペインがいれば、タブの題の頭に「● 」を付ける。
  - `claude-state watch`: kitty の外の常駐（systemd のユーザーサービス）。kitty の socket を直接読み、完了のペインを見たら idle に戻し、記録と実際の色が 2 回続けて違うペインを塗り直す（`load-config` で色が消えたときなど）。
  - `claude-state waiting` / `list` / `get` / `paint` / `marks` / `subagents`（ステータス行用のサブエージェントの要約）/ `repaint`。
  - kitty の watcher `kitty/claude_state_watcher.py`（新しいペインと kitty の再起動後に、完了を idle に戻す）。
- `claude-show`: 見てほしい HTML・URL を kitty の右側全体の上に（`claude-browser --area right --mode show --focus none`）、画像を自分のペインの overlay に開く。入力先は動かさない。保護する窓が前面なら開かない（exit 2）。
- `claude-browser open/move --area right`: 左の列（統括）を除いた kitty の右側全体を覆う範囲にする（ユーザーに見せる用）。`--area` と `--focus` は 1 回ごとの指定で、次の `open` に引き継がない。
- `claude-lang-check`: Stop hook。最後の返答が決めた言語（既定は日本語）でなければ、1 回だけ書き直させる。AI を使わず、文字の割合で判定する（[docs/lang-check.md](docs/lang-check.md)）。
- `claude-guard`: PreToolUse hook。名前の付いたガードが働いている間（プロセスが動いている・判定のコマンドが 0 を返す・旗が立っている）、決めたパスへの書き込みと決めた Bash のコマンドを、理由の文を付けて止める。`acquire` / `release` で排他のロックにもなる（[docs/guard-hook.md](docs/guard-hook.md)）。
- `claude-session-stats`: Claude Code の会話の記録から、言い直し・英語の返答・返答の長さ・全員への連絡などを期間ごとに数え、Markdown で書く。メールアドレスとユーザー名は伏せる（[docs/session-stats.md](docs/session-stats.md)）。
- `claude-roster`: kitty の中のセッションの名簿（役割・セッション ID・フォルダ・モデル・配置）を 5 分ごとに保存し、kitty を起動し直したり落ちたりしたあとに `restore` 1 つで戻す。`restart` で idle の 1 人を同じペインで開き直す（[docs/roster.md](docs/roster.md)）。
- `claude-wait --model`、および `claude-roster restart` が書く続きの指示（`$XDG_RUNTIME_DIR/claude-wait/<ID>.next`）で、シェルに戻らずに別のフォルダ・モデルで resume し直す。
- `claude-kitty`: 窓のクラス（`$CLAUDE_KITTY_CLASS`）と起動の形（`$CLAUDE_KITTY_START_AS`）。既定以外のクラスでは KWin で窓を動かさない（試験用の kitty を別に起動するため）。
- systemd のユーザーユニットの例: `contrib/systemd/`（`claude-state-watch.service`、`claude-roster-save.{service,timer}`、`claude-session-stats.{service,timer}`）。
- [PRACTICES.md](PRACTICES.md): 道具と一緒に使ってきた運用の約束（体制・注意の守り方・セッションの安全・レビュー・略号）。
- 版の番号: `VERSION` と、`bin/` のすべてのコマンドの `--version`。この CHANGELOG、[CONTRIBUTING.md](CONTRIBUTING.md)、GitHub Actions（main への push と PR でテスト、`v*` のタグで release）、`tools/release.sh`。
- テストの入口を `python3 tests/run.py` の 1 つにした（別ファイルのテストもここから走る）。

### 変更

- `claude-view`: 「保護する窓」（`view.guard`）の確認を `lib/guard.py` に移し、`claude-show` と共有する。説明と例を、特定の種類のアプリでなく「キーやフォーカスを奪われたくない窓」（全画面のアプリ・発表中の画面など）として書き直した（設定のキーと動きは同じ）。
- `claude-view`: 保護する窓のために見送ったときのデスクトップ通知（`notify-send`）をやめた（終了コード 2 とメッセージだけ）。
- `claude-view`: overlay（`claude-show` の画像など）を並べ替えの対象に数えない（等分割が崩れないように）。並べ替えの前に「●」の印を外し、あとで付け直す（standby のタブを題で探すため）。
- `kitty/kitty.conf`: `filter_notification all`（入力待ちのたびのデスクトップ通知を止める。待受中はペインの色で分かる）と `watcher claude_state_watcher.py` を足した。

### 修正

- `claude-browser`: `--mode show --ratio 0.6` でもペインの全高で置かれ、下の辺が kitty の窓より下にはみ出すことがあった。最大化を解くのは最大化しているときだけにし、置いたあと実際の窓の矩形を読み直して、ずれていれば置き直す（各辺 64 px 以内の一定のずれだけを打ち消し、悪くなったら指定の矩形に戻す）。

### 上げるときに手で行うこと

0.1.0 から上げるとき。`git pull` のあと、使う道具の分だけ行う。

1. **新しいコマンドのリンク**: `~/.local/bin` に `claude-state`・`claude-show`・`claude-guard`・`claude-lang-check`・`claude-session-stats`・`claude-roster` のリンクを張る（[SETUP.md の 2.](SETUP.md#2-インストール)）。kitty の watcher も `ln -sfn "$REPO/kitty/claude_state_watcher.py" ~/.config/kitty/claude_state_watcher.py`。
2. **`~/.claude/settings.json` の hooks**（既存のキーは残して足す）:
   - `claude-state`: UserPromptSubmit・Stop・StopFailure・Notification・PreToolUse・PermissionRequest・SubagentStart・SubagentStop・PostToolUse・PostToolUseFailure・SessionStart・SessionEnd（[SETUP.md の 5.](SETUP.md#5-状態の色claude-state)。ステータス行の例も同じ所）。
   - `claude-lang-check`: Stop（[docs/lang-check.md の「導入」](docs/lang-check.md#導入)）。
   - `claude-guard`: PreToolUse（[docs/guard-hook.md の「入れ方」](docs/guard-hook.md#入れ方)）。ガードは `config.json` の `guard_hook.guards` に書く。
3. **kitty.conf**: `watcher claude_state_watcher.py` と `filter_notification all` を足す（リポジトリの `kitty/kitty.conf` にリンクしているなら、もう入っている）。
4. **systemd のユーザーユニット**（`mkdir -p ~/.config/systemd/user` のあと、`ExecStart` のパスをリポジトリを置いた場所に直して写す。timer は同じ名前の `.service` と組で写す）:
   - `claude-state-watch.service`: `systemctl --user daemon-reload && systemctl --user enable --now claude-state-watch.service`（[SETUP.md の 3.](SETUP.md#3-kittyconf-の要点)）
   - `claude-roster-save.service` と `.timer`: `systemctl --user enable --now claude-roster-save.timer`（[docs/roster.md の「自動の保存」](docs/roster.md#自動の保存)）
   - `claude-session-stats.service` と `.timer`: `systemctl --user enable --now claude-session-stats.timer`（[docs/session-stats.md](docs/session-stats.md)）
5. **動いている kitty に設定を読ませる**: `kitten @ load-config` のあと `claude-state repaint`。読み直すと `set-colors` の色が全ペインで消えるため（`claude-state watch` が動いていれば 2 秒ほどで自分で塗り直す）。前からあるペインには watcher が付かないので、完了を idle に戻すのは `claude-state watch` が受け持つ（kitty を起動し直す必要はない）。

## [0.1.0] - 2026-11-20

最初の公開。

### 追加

- `claude-kitty`: kitty を `CLAUDE_*` の環境変数なしで起動し（中の claude が子セッション扱いになり会話記録を残さなくなるのを避ける）、セッションファイルどおりにペインを開く。この道具自身の設定（`CLAUDE_KITTY_*` / `CLAUDE_VIEW_*`）は残す。
- `claude-view`: 統括用。ペインを役割名（user var `role`）で並べ替え、どの操作も等分割にそろえる（`all`・`focus`・`pair`・`restore`・`add`・`remove`・`swap`・`equalize`・`rows`・`status`、`--dry-run`）。保護する窓（`view.guard`）が前面なら何もしない。壊れた guard の設定は拒む。close 系と send-text は使わない。
- `claude-wait`: 別の場所で動いている同じセッションが確実に終わるのを待ってから resume する（同じ ID を 2 か所で開かない）。
- `claude-browser`: ペインごとに独立した Chrome（専用プロファイル・CDP ポート）を、そのペインに重ねる（`--mode self|show`、`shot`、プロファイルの台帳）。
- 設定ファイル `~/.config/claude-kitty/config.json`（`view.left`・`view.restore`・`view.guard`）。
- テスト `python3 tests/run.py`（標準ライブラリだけ。kitty や Chrome は偽物）。

[Unreleased]: https://github.com/hummer98/claude-kitty/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hummer98/claude-kitty/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hummer98/claude-kitty/releases/tag/v0.1.0
