# 変更履歴

このプロジェクトの変更を版ごとに書く。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、版の番号は [Semantic Versioning](https://semver.org/lang/ja/) に従う（1.0 までは 0.x で、小さい方の数字の上がりにも互換の無い変更が入りうる）。

各版の節は「追加」「変更」「修正」と、**上げるときに手で行うこと**（`git pull` だけでは済まない設定の変更）に分ける。GitHub の release の本文は、タグを push したときにこのファイルの該当の節から作る（`tools/changelog_section.py`）。

## [Unreleased]

### 追加

- `claude-roster`: プロジェクトごとに `CLAUDE_CONFIG_DIR` で設定の置き場を分けている（direnv の `.envrc` など）ときに、名簿と復元が効くようにした（[docs/roster.md](docs/roster.md#プロジェクトごとの設定の置き場と包み)）。
  - `save` はペインの claude の `CLAUDE_CONFIG_DIR` を読み、既定でなければ役割に `config_dir` を残す。セッションの記録・モデルもその置き場から読む。
  - `restore` は `config_dir` のある役割を `env CLAUDE_CONFIG_DIR=… claude-wait …` で開く（同じ置き場でないと resume で会話が見つからない）。
  - 設定 `roster.claude`（既定 `{}`）: フォルダ → 開くときに claude の代わりに使うコマンド（`CLAUDE_WAIT_CLAUDE`）。プロジェクトごとの包み（権限のモード・環境の守り）で開き直すため。
  - 見る置き場は kitty のペインの claude と名簿に記録したものだけ（マシンの全部の claude から集めると、macOS では `~/Documents` の下などの保護されたフォルダを読んで止まった）。
  - 「既定の置き場」は Claude Code の既定（`~/.claude`）と比べる（direnv の効いたシェルから手で save しても落とさない）。claude-wait が待っているだけのペインは claude-wait の環境から読み、/exit したペインや読めなかったときは前の名簿の `config_dir` を引き継ぐ（世代も回す）。
  - 名簿の `config_dir`・`cwd` に制御文字があれば読まない（セッションファイルに行を足せないように）。`roster.claude` の値は引数なしの実行ファイル（空白・制御文字は断る）。
  - 相対パスの `CLAUDE_CONFIG_DIR`（direnv の `export CLAUDE_CONFIG_DIR=.config` など）は、そのプロセスの cwd から絶対パスにして残す。制御文字の入った値は記録しない。`save` は書く前に名簿を確かめ、読めない名簿は書かない（前の名簿のまま）。
  - claude-roster 自身を direnv の効いたシェルから動かしても、既定の置き場（`~/.claude`）のセッションと会話の記録も読む。
- `claude-active`: どれかの役割（統括を含む）のセッションで、人が打った最後の発話からの分数と役割を 1 行で返す（読むだけ。[docs/active.md](docs/active.md)）。ハマった作業を人に上げてよいか（人が起きているか）を決めるため。
  - 会話の記録の `origin.kind` / `turnOrigin` が `human` の文字列の行だけを数える（自動の要約・起動の引数で渡した最初の指示・他のセッションからの知らせ・見回りの cron・背景の仕事の知らせ・道具の結果・付き物・出どころの無い古い行は数えない）。
  - 既定は 20 分以内で終了コード 0、超えれば 1（`--minutes N`）。見分けに自信が無ければ `unknown` で 2（起きているとは答えない）。`--json`。時刻は JST。
  - 記録は session ID で projects の全フォルダから探し、末尾から読む。待たせた発話（queued）は、打った時刻（`queue-operation` の enqueue）で数える。
- `claude-show`: 設定 `view.playtest_guard`（claude-guard のガードの名前、既定 `null` = 今までどおり）。そのガードのプロセス（ゲームなど）が動いていて、人がテストプレイ中なら何も開かないで exit 2（「ゲームをテストプレイ中なので開かない。終わってから」）。テストプレイ中の画面に Chrome や overlay を出さないため（[README](README.md#claude-show見てほしいものを開く)・[docs/config.md](docs/config.md#viewclaude-viewclaude-showclaude-roster)）。
  - 見分けは claude-guard の `allow_owner` の起動の記録（`lib/guardhook.py` の `read_launch_record` を使い回す）。記録が無い・pid が今のプロセスでない・owner が役割（名簿か claude-state）でなければ人がプレイ中。記録が生きていて owner が役割なら担当の e2e として今までどおり（前面のときだけ断る）。
- 共同作業（coop）: 人が動かしているゲームを AI の担当と一緒に操作できるようにした。占有の決まりのままだと、人が起動したゲームでは担当の操作がすべて止まるため（[docs/guard-hook.md](docs/guard-hook.md#共同作業coop)）。
  - `claude-guard`: 起動の記録に `"coop": {"with": 担当かそのリスト, "reason", "since", "until"}` があり、期限内で記録の pid が今のプロセスなら、`with` の担当も `allow_owner` と同じ照合（`option`・`env`・`same_role`）で通す。旗・ロックの間は使わない。保護したパスへの書き込みと git は共同作業でも止める。`owner` の無い記録も `coop` があれば読む（人が `--owner` なしで起動した形）。
  - `claude-guard status`: 「共同作業: <担当>、HH:MM（JST）まで（理由）」。`coop.modified` / `coop.changes` があれば「プレイ用のセーブを N 回変えた」を添える（判定には使わない）。
  - `claude-show`（`view.playtest_guard`）: 呼んだペインの役割が `coop.with` にあれば、人がいても開く（見せるのが目的）。ゲームの窓が前面なら断る（exit 2）。
  - `claude-show`: 印を owner より先に見る（`launch --coop` の最中に、`with` に無い担当まで担当の e2e として開いていた）。印が効いていれば `with` の担当だけが開け、ほかは owner が役割でも断る。共同作業の HTML は `claude-browser --no-above`（新設。keepAbove を付けない・外す）と `--focus none` で開く（ユーザーがゲームに戻ったとき Chrome を上に残さない）。

## [0.7.6] - 2026-11-23

### 修正

- init の無いコンテナ（CI の Python 3.9 の job など）で、止めたプロセスがゾンビのまま残ると「まだ動いている」と見ていた。`claude-roster sleep` が 30 秒待って止めたことにならなかった。Linux ではゾンビ（`/proc/<pid>/stat` の状態が Z・X）を終わったものとして扱う（`lib/proc.py` の `alive`・`start_time`）。

### 変更

- docs/relay.md の「まだ無いもの」: LAN の外へ延ばすときや受け手のマシンを共用するときに要る、本文の暗号化と 1 通ごとの署名のこと（いまは要らない理由も）。

## [0.7.5] - 2026-11-23

### 追加

- `claude-roster sleep <役割>` と `wake <役割>`: 役目を終えた担当を「寝かせる」。サーヴァントが増え続けるので、使っていない実行レーンを減らし、あとで会話ごと戻すため（[docs/roster.md](docs/roster.md#sleep--wake)）。
  - sleep: idle を確かめ（restart と同じ。`--force` で省く）、claude-wait に「終わる」の続きの指示（`"action": "exit"`）を書いて claude に SIGTERM を送り、claude が終わってペインの前面にシェルしか残っていないのを確かめてから `kitten @ close-window` で閉じる。止まらなければ・ほかのプロセスが残っていれば閉じず、名簿も変えない。名簿のその役割に `sleeping`・`slept_at`・`sleep_place`（タブと段組みの位置）を書き、ID・cwd・model・name・slot は残して段組みから外す。統括（`view.left`）と常時表示（`view.pinned`）は断る。ペインが既に無い役割は印を付けるだけ。
  - 寝ている役割は、自動の保存でも手の保存でも、`keep_absent_hours` を過ぎても消さない。`restore` は開かない（`--include-sleeping` か名指しのときだけ）。`status`・`show` に「寝ている（いつから）」。
  - wake: `restore <役割>` と同じ決まりで claude-wait の形で開き直し、段組みは寝かせる前の位置に戻して並べ直し、印を外す。
  - claude-wait: 続きの指示が `"action": "exit"` なら、claude が終わったあとシェルに戻らずに終わる。前の版の claude-wait はこの指示を断ってシェルに戻るだけ（同じ会話を開き直さない）。
- `claude-guard`: `commands` の `{"program"}` の `block` の項目に `but_if_any`。`unless_any` のフラグで通すものでも、`but_if_any` のどれかがあれば止める（`{"sub": "road", "unless_any": ["--dry-run", "--inspect-node"], "but_if_any": ["--engine-check"]}`）。読むだけの形でも、エンジンに評価させるフラグを足すとゲームが落ちる余地があるため。`if_any` / `unless_any` だけの項目の意味は変わらない。`commands` の `if_any` と `but_if_any` は argparse の省略形（`--eng`）にも当て（`allow_commands` では当てない）、通す側の `unless_any` は完全一致のまま。`unless_any` 無しの `but_if_any` は設定の誤りとして知らせる（[docs/guard-hook.md](docs/guard-hook.md)）。

### 修正

- `claude-guard`: 止める側のフラグ（`if_any`・`but_if_any`）を argparse の省略形にも当てる。tf3ctl などは既定で省略形を受け付けるので、`camera --se 1`（`--set`）や `road --dry-run --eng`（`--engine-check`）が通っていた。通す側の `unless_any` は完全一致のまま（`--dry` は止まる）。
- テスト: macOS で `tests/run.py` を回すと、install_test が本物の `launchctl` を呼び、claude-kitty の常駐（state-watch など）を launchd から外していた。偽の launchctl を渡す（PR #4）。

## [0.7.4] - 2026-11-23

### 追加

- `claude-presence`（新しいコマンド）と設定 `model_policy`（既定は無効）: 人がいるかを最後に人が打ってからの時間で決め、離席中（既定 20 分）は実装のサブエージェント（Agent ツール）の `model` を軽いもの（既定 `haiku`）に変える。離席中の作業はトークンの少ない方で、人がいるときは呼び出しどおり（速さ優先）にするため（[docs/config.md](docs/config.md#model_policyclaude-presence-離席中のサブエージェントのモデル)）。
  - 記録: claude-state の UserPromptSubmit の hook が、人の入力のときだけ時刻・セッション・役割を `<置き場>/claude-presence/last.json` に書く（全セッション共通の 1 つ）。背景の作業の完了の通知・セッション間メッセージ・claude-relay の起こす知らせ・`state.notice` に当たるもの・入れ子の claude は数えない。
  - 切り替え: PreToolUse の hook（`claude-presence hook`、matcher `Agent|Task`）が呼び出しを `permissionDecision: "deny"` で断り、`permissionDecisionReason` で「離席中なので model を `away_model` にして同じ呼び出しを出し直してください（ほかの項目はそのまま）」と頼む。出し直し（`model` が `away_model`）は通し、そのとき `switches.jsonl` に残す（断った記録は `denials.jsonl`）。入力を書き換える `updatedInput` は使わない: auto mode では hook が入力を変えると判定が無効になって断られ、同じ返答のほかの呼び出し（Bash など）まで巻き込まれたため（実測）。deny なら巻き込まない見込み（実機の auto mode ではまだ確かめていない）。`keep` の語（レビュー・評価・調べものなど）を含むもの、`subagent_type` が `claude-code-guide`・`Explore`・`Plan`・`web-fetch`・`fork` のもの、`model` が `fable` のもの、記録が無いとき、例外のときは止めない。英字の `keep` の語は単語として探す（`guide` は `guideline` に当てない）。同じセッションの同じ prompt を 300 秒以内に断っていたら、元の model のままの出し直しも通す（Claude が従わなかったときに断りをくり返して作業を止めないため）。断りの記録が書けない・読めないときは断らない。
  - `claude-presence [--json] [--recent N]` で最後の入力からの分数・判定・設定・hook・直近の切り替え・断った数（うち切り替わった数）を見る。`claude-kitty doctor` に「モデルの切り替え」と「設定: model_policy」の行。
  - `tools/install.sh --hooks=model` で hook を入れる（`--all` には入らない。`--uninstall` で外せる）。
- `claude-state watch` にディスクの使用率の見張りと設定 `disk_watch`: `interval_sec`（既定 60 秒）ごとに `paths`（既定 `["/tmp"]`）の容量と inode の使用率を `os.statvfs` で測り、`thresholds`（既定 70・85 %）を超えたら知らせる。2026-11-23 に /tmp（tmpfs）の inode が 100% になり、全部のセッションでコマンドが起動できなくなった（テストの一時フォルダの残り）ため、その前に人と統括に知らせる（[docs/config.md](docs/config.md#disk_watchclaude-state-watch-ディスクの使用率の見張り)）。
  - 閾値ごとに 1 回だけ。閾値より 10 ポイント下がってからまた超えたら、もう 1 回。知らせた印は `$XDG_RUNTIME_DIR/claude-disk-watch/state.json` に置き、watch を起動し直しても繰り返さない。inode の数を持たないファイルシステム（btrfs など）は inode を見ない。無い path は飛ばす。
  - 送り先は `notify_role` の役割のセッション（既定 `null` = デスクトップの通知だけ）。claude-relay 第 2 段の起こす部品（`lib/relay.py` の `WAKERS`、cross-session の socket）で 1 通送り、止まっているセッションにも届ける。送れなければデスクトップの通知（`notify-send` / `osascript`）。本文に、直下の項目を名前の頭（`tmp*`・`lua_*`・`pytest-*` など）でまとめた数の上位 3 つと、直下の項目の数。直下が少なければ、直下のフォルダごとの中の数の上位 2 つも（1 秒で数え終わらなければ省く・途中まで）。`claude-state watch --once` は測った値を出すだけで、知らせない。本文の頭の `[claude-kitty disk]` は claude-presence が人の入力と数えない。
  - `claude-kitty doctor` に「ディスク: <path>」（使用率と、閾値を超えているか）と「設定: disk_watch」の行。
- 設定 `view.pinned`（既定 `[]`）: 統括（`view.left`）と同じく常時表示にする役割。`claude-view` のどの操作（`all`・`focus`・`pair`・`restore`・`add`・`remove`・`swap`・`equalize`・`rows`）のあとも右側の 1 段目の先頭（統括のすぐ右上。複数なら書いた順）に置き、standby に下げない。運用の担当を統括と同じく常に見えるところ・同じ場所に置くため。`rows` で別の位置を指定しても先頭へ移し、並べ替えた結果を 1 行出す。`swap` で pinned を入れ替えようとすると断る。`rows`・`restore`・`pair` の指定そのものの誤り（同じ役割を 2 回など）は、pinned を先頭へ移す前の形で断る（保護する窓が前面なら終了コード 2 のまま）。kitty にいない役割・`view.left` と同じ役割は警告を 1 行出して置かずに続ける。`claude-roster restore` のセッションファイルも統括のタブの 1 段目の先頭に開く（名簿で別のタブにいても二重に開かない）。kitty が動いているときに足したのが pinned だけなら、保存した段組みに戻さず `claude-view equalize` で前に出す（[docs/config.md](docs/config.md#viewclaude-viewclaude-showclaude-roster)）。

### 変更

- PRACTICES.md と docs/relay.md: ユーザーの指示があれば、担当どうし（別の端末の担当とも）統括を経由せずに直接やりとりしてよい、と書いた。relay では担当の役割を `relay.receivers` に足せば `<端末>/<役割>` 宛てで直接届く（ユーザーの原文「「統括を経由する」ルールは緩和して、指示があれば直接サーヴァント間会話するのは許容します」）。

### 修正

- `claude-guard`: `git clone` の値をとるオプション（`--branch master`・`-b master`・`-bmaster`・`--branch=master`・`--depth 1` など）の値を元、元を行き先と読み、保護したフォルダから外へ写すだけの `git clone -q --branch master <保護> <外>` を止めていた。clone と init の値をとるオプション（略した `--bra` も）を飛ばして元と行き先を読む。`git init -b main <保護>` は `main` を行き先と読んで通していたので止める。`--separate-git-dir` と `--template` の値、`GIT_DIR` / `--git-dir` で指した場所が保護したフォルダなら止める。`git archive -o<保護>/a.tar`・`git format-patch --output-dir <保護>` のように値をくっつけた形・略した形の出力先も止める。`git worktree repair <保護>` と、cwd が保護したフォルダのときの `git worktree prune` / `lock` / `unlock` も止める（`list` は通す）。`GIT_WORK_TREE` / `--work-tree` と `GIT_DIR` / `--git-dir` は別々に見る。`-C` は cwd を変えるだけとみて、`git -C <保護> clone <元> <外>` は通す。

### 上げるときに手で行うこと

- 使うときだけ: 設定に `"model_policy": {"enabled": true}` を書き、`tools/install.sh --hooks=model`。
- `systemctl --user restart claude-state-watch.service`（ディスクの見張りを始める）。統括に送るなら設定に `"disk_watch": {"notify_role": "<役割>"}` を書く（その役割が `relay.receivers` に入り、relay の hook が入っていること）。
- 使うときだけ: 設定の `view` に `"pinned": ["<役割>"]` を書く。

## [0.7.3] - 2026-11-22

### 追加

- `claude-guard`: `commands` の `{"program"}` の項目に `allow_owner`（起動した担当だけ通す例外）。道具が書く起動の記録（`{"v":1,"owner","pid",…}`）の owner と、コマンドの `--owner X` / `--owner=X`（か前に付けた環境変数）が同じで、記録の pid がそのガードの `process` に当たるプロセスとして動いていて、セッションの役割（claude-state の記録の `role`）も X のときだけ、その項目の形を通す。旗（`claude-guard on`）・ロック（`acquire`）の間は「全員を止める」ので使わない（process だけで働いているときに効く）。役割が分からないセッション・記録が無い・壊れている・pid が死んでいる・別のプロセスのときは今までどおり止める。`claude-guard status` に記録の owner と pid の状態を出す（[docs/guard-hook.md](docs/guard-hook.md#起動した担当だけ通すallow_owner)）。

### 修正

- `claude-guard`: コマンドの名前を変数に入れた形（`T="python3 /x/tools/svcctl.py --owner a"; $T quit`）を見逃していた。値を 1 語のまま置き換えていたので、プログラム名が `svcctl.py --owner a` になって当たらなかった。同じコマンドの中で前に代入した変数（`T=…;`・`export T=…`・`&&` でつないだもの）がコマンドの位置に `$T` / `${T}` で来たら、値を語に分けた形と分けない形（`"$T"`。bash では 1 語で動く）の両方で読み、どちらかが止める形なら止める（`S="/opt/my tools/svcctl"; "$S" deploy` も止める）。コマンド置換・関数・外の環境変数は今までどおり読まない。

## [0.7.2] - 2026-11-22

### 追加

- 設定 `browser.enabled`（既定 `true`）: `false` でブラウザの機能（おまけの `claude-browser`・`claude-show` の Chrome）を止める。ブラウザの使い方を自分で決めている人の邪魔をしないため（[docs/config.md](docs/config.md#browserclaude-browserclaude-show-重ねる-chrome)）。
  - `claude-browser` は `close` のほかを何もしないで、stderr に 1 行出して終了コード 3 で終わる（`close` は前に開いた Chrome を片づけるためだけに動く）。
  - `claude-show` は HTML・画像・URL を OS の既定のアプリ（`xdg-open`、macOS は `open`）で開く（結果の `via` が `"browser.enabled: false"`）。
  - `claude-kitty doctor` は Chrome を探さず「browser: オフ（browser.enabled: false）」と出し、node も調べない。

### 修正

- `claude-view equalize`・`add` などが、`claude-show` の画像（overlay）を重ねたペインを「kitty に見つからない」として止まることがあった。kitty（0.51 で確認）は window group の並びで overlay を先に置くことがあり、group の 2 つめ以降を overlay とみなしていたため本体が外れていた。group に window が 2 つ以上あるときは role を持つ window を本体とする（role を持つものが無い・2 つ以上なら従来どおり先頭）。

## [0.7.1] - 2026-11-22

### 追加

- `claude-relay`（第 2 段の 1 つ目）: 止まっている（入力待ちの）受け取りのセッションを起こす（[docs/relay.md](docs/relay.md#止まっているセッションを起こすsocket)）。別の端末のセッション同士が、人を介さずにやりとりできる。
  - 受け取りの役割のセッションの hook が、Claude Code の cross-session messaging の socket（`CLAUDE_CODE_MESSAGING_SOCKET`・`CLAUDE_CODE_MESSAGING_TOKEN`）を `relay/wake/<役割>/<セッション id>.json`（0600）に記録する（同じ役割のセッションが複数あっても上書きし合わず、recv は新しく動いた方から試す）。
  - `recv` が受信箱に置いたあと、その socket に短い知らせ（何通・どこから・印 `[relay-wake ids=…]`。本文は入れない）を送る。記録が無い・claude が終わっている・送れないときは第 1 段のまま（recv の結果は変えない）。成否と使った部品はログの `wake`・`wake_via` に残す。
  - 知らせで始まったターンの UserPromptSubmit の hook が、本文を枠と注意書き付きでそのまま渡す（セッションが `inbox --deliver` を打たなくてよい）。渡ったことを受領（`wake acked`。hook で渡したか、Stop・`inbox --deliver` などで渡ったか）として id ごとに 1 度ログに残す。60 秒たっても渡っていなければ、次の `recv` か `status` のときに `wake unacked` を 1 度ログに出し、`status` に「（起こす知らせが未受領）」と付ける（常駐の見張りは無い）。起こすのは役割ごとに 10 分に 20 回まで（人がいないままの往復の歯止め）。
  - 起こす方法は届け方の部品（`available` と `wake` だけを持つ。今は socket の 1 つ）に分け、上から試す。本文・受領・未受領は部品の外で扱う。
  - 受け手の `crossSessionInbound` や権限モードによっては、知らせが保留（人の承認待ち）になることがある。
  - 知らせを別のセッションに送らないよう、祖先の claude の pid が分からない hook では socket を記録しない。送る前に、記録の claude が生きていて起動時刻も同じで、socket のファイル名が `<pid>.sock` ならその pid とも同じことを確かめ、違う記録は使わずに消す（pid が使い回されると、同じパスの socket を同じユーザーの別の claude が作っていることがある）。
  - 置き場（`$XDG_STATE_HOME/claude-kitty/relay/` など）を初めて作るときは、親のフォルダも 0700 で作る（既にあるフォルダの権限は変えない）。

### 変更

- `tools/install.sh --hooks=relay` は SessionStart にも `claude-relay hook SessionStart` を足す（始まったばかりのセッションの socket も記録する）。

### 修正

- `claude-relay`: 受信箱が空のとき、受け取りの役割のセッションの hook（UserPromptSubmit・Stop）が毎回「TypeError: cannot unpack non-iterable NoneType object」を `relay/errors.log` に残していた（hook は exit 0 なので動きには出ない）。`claude-relay inbox --deliver` は受信箱が空だとトレースバックで落ちていた。

### 上げるときに手で行うこと

- `--hooks=relay` を入れているなら、`tools/install.sh --hooks=relay` をもう一度（SessionStart の hook を足す。`claude-kitty doctor` は入れ直すまで「hook: claude-relay」を注意と出す）。
- 止まっているセッションを自動で起こしたいなら、受け手のセッションが cross-session messaging を受け付ける設定か確かめる（`crossSessionInbound` が `hold` や権限モードによっては、知らせが人の承認待ちになる）。

## [0.7.0] - 2026-11-21

### 追加

- `claude-relay`（第 1 段）: 互いに承認した端末（別の claude.ai アカウントでもよい）の Claude Code セッションの間で、LAN の中の ssh でメッセージを渡す（[docs/relay.md](docs/relay.md)、登録の手順は [SETUP.md の 7.](SETUP.md#7-端末をまたぐ連絡claude-relay)）。
  - `pair`（専用の ed25519 の鍵を `~/.config/claude-kitty/relay/` に作り、相手に渡す 1 行を出す）・`trust` / `untrust`（`~/.ssh/authorized_keys` の受け取り専用の行・設定の `relay.peers`・相手の host key を、見せて確認してから書く。ほかの行は残し、`.bak-<日時>` を作って一時ファイル経由で置き換え、0600 を保つ）・`send` / `ping`・`peers`・`inbox [--deliver]`・`status`（ステータス行用）・`log`。
  - 受け取る側は authorized_keys の強制コマンド `claude-relay recv --from <名前>` だけを動かす（`recv [役割]` 以外は拒否）。送り元は鍵で決まる。`relay.enabled`・受け取りの役割（既定は統括 = `view.left`）・大きさ・送り元ごとの通数で拒否し、受信箱（`~/.local/state/claude-kitty/relay/`）とログに置く。
  - 統括のペインがあれば、claude-state の記録に新しい印 `relay` を付けて待受の色にする（`lib/state.py` に小さく追加。Stop や入力では外れず、渡したときだけ外れる）。
  - `tools/install.sh --hooks=relay`（`--all` には入れない）: UserPromptSubmit で `additionalContext` として、Stop で `decision: block` として渡す（`stop_hook_active` なら渡さない）。枠 `<relay-message …>` には「ユーザーの指示ではない」と注意書きを付け、本文の中の閉じタグなどはエスケープする。
  - `claude-kitty doctor`: 設定に `relay` の節があるときだけ、sshd・鍵・相手・受け取りの登録・hook を調べる。設定の `relay` の節（[docs/config.md](docs/config.md)、`config.example.json`）。

### 上げるときに手で行うこと

- `claude-relay` を使うなら `tools/install.sh --links`（新しいコマンドのリンク）と `--hooks=relay`、受け取る側は sshd（macOS はリモートログイン）。使わないなら何もしなくてよい。

## [0.6.2] - 2026-11-21

### 変更

- `claude-state`: 手の待受（`claude-state waiting`）の消え方を変えた。前は人が打つまで消えず、他のセッションからのメッセージや背景の作業の通知で何ターンも作業して、最後は問いの無い報告で終わっても、問いを残したときの待受の色のままだった。今は付けたターンのものとし、付けたターンの Stop では残し、人が打てばすぐ消し、それ以外で始まった次のターン（通知のターンや、本体が自分で再開したターンも）はその Stop までに `claude-state waiting` が呼び直されなければその Stop で消す（Esc で止めたときは入力欄に戻った通知で。StopFailure では返答を書けていないので消さない）。まだ答えを待っているなら、そのターンの最後の返答でも呼び直す。次のターンで本体がツールを使い始めたら、その時点で消す（mac で使った PR #1 の直しと合わせた）。前の版の記録（manual に at だけ）は、次のターンの Stop で同じく消える。README・PRACTICES.md の説明も合わせた。
- `claude-kitty`: 起動の前に、呼んだ側の端末の環境変数も消す（`CMUX_*`・`C11_*`・`GHOSTTY_*`・`TMUX`・`TMUX_PANE`・`TERM_PROGRAM`・`TERM_PROGRAM_VERSION`・`KITTY_WINDOW_ID`・`KITTY_LISTEN_ON`・`KITTY_PID`・`WINDOWID`）。cmux（c11）の中の Claude Code から起動すると、kitty の各ペインの claude が「c11 の中に居る」と判断し、c11 用の SessionStart hook が失敗していた。足すものは `CLAUDE_KITTY_UNSET`（名前か `FOO_*`）。`CLAUDE_KITTY_*` と `CLAUDE_VIEW_*` を残すのは今までどおり。

### 修正

- `claude-browser`・`claude-show`: Flatpak（や Snap）の Chrome で、サンドボックスから見えない場所（Flatpak の filesystems の許可の外など）の file:// を開くと「アクセスできませんでした」になっていた。サンドボックスの Chrome のときだけ、開く file:// が中から見えるかを判定し（Flatpak は `flatpak info --show-permissions` と override の filesystems: host・home・xdg-*・`~/…`・絶対パス・`!` の否定・`flatpak run --filesystem=`。Snap は home インターフェース。読めない・解釈できないときは見えない側）、見えなければ表示用のコピーを `~/.var/app/<アプリ ID>/claude-show/<ハッシュ>/`（Snap は `~/snap/<名前>/common/claude-show/<ハッシュ>/`）に写してそちらを開く。HTML は同じフォルダの中を指す相対参照の資源も同じ位置に写す（`../`・絶対パスの参照は写さず stderr に 1 行。合計 50MB を超えれば HTML だけ）。写しは 1 日で消し、呼び直すと毎回写し直す。元のファイルは動かさず、結果の JSON の `target` は元のまま、`staged` に写した先、`note` に「直すのは元のファイル」を入れる。ネイティブの Chrome では前と同じ。`claude-kitty doctor` の Chrome の行にも、サンドボックスのときはその旨を出す（ok のまま）。
- `claude-kitty`: macOS で `--class` を渡していた。macOS の kitty（0.51）には `--class` が無く「Unknown flag: --class use --help.」で終わるので、kitty が開かなかった。しかも背景で起動するので 0 で終わり、失敗に気づけなかった。macOS では `--class` を渡さず（`CLAUDE_KITTY_CLASS` は Linux だけ）、`CLAUDE_KITTY_WAIT` 秒（既定 2）のうちに kitty が 0 以外で終わったら kitty のエラーを出して 1 で終わる。
- `claude-wait`・`claude-roster`: Claude Code の sessions を `~/.claude/sessions` に決め打ちしていた。`$CLAUDE_CONFIG_DIR`（direnv などでプロジェクトごとに分けている）で動く claude を見つけられず、動いている相手を「いない」とみなして同じセッションを 2 か所で開いていた。`CLAUDE_SESSIONS_DIR` > `$CLAUDE_CONFIG_DIR/sessions` > `~/.claude/sessions` の順に見る（`claude-roster` の projects も同じ）。
- `tools/install.sh`・`tools/release.sh`・`claude-kitty`: macOS の `/bin/sh`（bash 3.2）で、変数のすぐ後ろに全角文字があるメッセージ（`$BIN_DIR（` など）の変数が空になり、出力が UTF-8 として壊れていた（`${名前}` と書く）。

## [0.6.1] - 2026-11-21

### 修正

- `claude-browser`: 前回の `--over` の先のペインが見えていないタブに移ったあと、`claude-show`（`open --mode show --area others`）や `--area others` の `open` まで「ペイン N はいま表示されていないタブにある」で止まり、`claude-browser close` するまで開けなかった。前回の over を引き継ぐのは area が `pane` のときだけにした（`others`・`right` は呼び出した自分のペインと左の列で決まるので、前回の over は使わない）。引き継いだ over の先が見えないタブにある・もう無いときは、止めずに自分のペインに戻して stderr に 1 行で知らせ、状態にも自分のペインとして残す（`move` で area も over も省いたときも同じ）。`--over` を明示したときは、前と同じくエラーで止まる。

### 変更

- PRACTICES.md: プロンプトの略号に `pre`（preview: 生成物を見せる。HTML・画像は `claude-show`、デプロイ先はその URL）を足した。

## [0.6.0] - 2026-11-20

### 追加

- macOS に試験的に対応した（作者は macOS の実機では動かしていない。macOS の分岐は Linux の上で OS の部分を偽物にした単体テスト `tests/macos_test.py` で確認した）。手順と、使えるもの・試験的なもの・使えないものの表は [SETUP.md の macOS での導入](SETUP.md#macos-での導入)。
  - `lib/proc.py`: プロセスの情報（生きているか・引数・起動時の環境・親・起動時刻・一覧）を 1 か所にまとめた。Linux は `/proc`、macOS は ctypes で `sysctl`（KERN_PROCARGS2）と `proc_pidinfo`（使えなければ `ps`）。`claude-state`（入れ子の判定）・`claude-wait`・`claude-roster`・`claude-browser`・`claude-view` と状態の記録の掃除がこれを使い、`/proc` を直接読むのはここだけになった。kitty の watcher は signal 0 で生存を見る。
  - `claude-wait`・`claude-roster`: Claude Code の `procStart` と起動時刻を比べるのは Linux だけ。macOS では比べず、その pid が生きている間は「動いている」とみなす（同じセッションを 2 か所で開かない側）。
  - `claude-browser`: macOS では、kitty の窓の位置を CoreGraphics（`CGWindowListCopyWindowInfo`。画面収録・アクセシビリティの許可は要らない）で読み、Chrome の窓を CDP の `Browser.setWindowBounds` で置く（`lib/macwin.py`、`lib/cdp.mjs` の `window`・`place`・`front`）。「常に最前面」は無く、Chrome を前に出す（入力先も移る）のは `--focus browser` のときだけ（既定の kitty と none では入力先を動かさないので、Chrome が kitty の後ろに隠れることがある。新しく起動した Chrome は macOS が前に出すことがある）。読めない・置けないときは止めずに普通の窓のまま開く。プロファイルの既定の置き場は `~/Library/Application Support/claude-kitty/browser-profiles`。`~/Applications` の Chrome と `Chromium.app` も探す。
  - `view.guard`（`claude-view`・`claude-show`）: macOS では前面のアプリを `lsappinfo` で読む。読めなければ、当たるプロセスが動いているだけで並べ替えを控える。
  - `claude-kitty`: macOS では `--detach` の代わりに `nohup` で起動する。`kitty.maximize`・`kitty.desktop` は使わず、設定してあれば注意を出す。`readlink -f` を使わない（古い macOS に無い）。
  - `claude-show`: kitty の外では、macOS は `open` で開く。
  - `tools/install.sh --launchd`（macOS の `--all` に含む）: `contrib/launchd/` の LaunchAgent（`claude-state watch`、`claude-roster save --auto` の 5 分ごと、`claude-session-stats` の週 1 回）を `~/Library/LaunchAgents` に写して `launchctl bootstrap` する。PATH（`~/.local/bin`・`/opt/homebrew/bin`・システム）と HOME を plist で渡す。`--uninstall` で `bootout` して消す。
  - `claude-kitty doctor`: macOS では launchd の LaunchAgent が入って読み込まれているか、`lsappinfo` が使えるかを出す。Chrome は `claude-browser` と同じ探し方（`lib/browser.py`）で出す。
- `claude-kitty doctor`: hook が入っていても、その道具（command の最初の語）が絶対パスでも PATH でも見つからなければ NG にする（Claude Code の中で黙って失敗するため）。

### 変更

- 対応する Python の下限を 3.10 から 3.9 にした（macOS の Command Line Tools の `/usr/bin/python3` が 3.9。3.9 でテストが全部通ることを確認した）。`tools/install.sh` は python3 が 3.9 より古ければ何もせずに止まり、`claude-kitty doctor` は NG にする（macOS は `brew install python` を案内する）。
- 道具の記録の置き場（`claude-state`・`claude-browser`・`claude-wait`・`claude-roster`・`claude-view` のロック）は、`$XDG_RUNTIME_DIR` が無いとき `/tmp` ではなく `/tmp/claude-kitty-<uid>`（0700）にする（`lib/rundir.py`。`/tmp` はほかのユーザーと共有なので）。kitty の socket は前と同じく `$XDG_RUNTIME_DIR` か `/tmp` を探し、macOS では両方を探す。`$XDG_RUNTIME_DIR` がある環境（ふつうの Linux）は変わらない。
- `tools/install.sh --all` の常駐は OS に合う方だけを行う（Linux は `--systemd`、macOS は `--launchd`）。
- `claude-wait`: 終わったあとに入るシェルは `$SHELL` が bash か zsh ならそれ（macOS の既定は zsh）、ほかは前と同じ bash。
- `claude-session-stats`: 統括の記録のフォルダの名前（`session_stats.home_project` の既定）を、Claude Code と同じく英数字以外を `-` にして作る（前は `/` だけを置き換えていたので、`/Users/first.last` などで合わなかった）。
- 文言: 「確かめる」を「確認する」に言い換えた（メッセージ・文書・コメント）。

### 修正

- `claude-wait --help` が何も出さずに終わっていた（使い方を出す）。引数の誤りで使い方を出したあとシェルを exec するとき、表示がバッファのまま消えていたのも直した。
- `claude-view`: kitty が 1 つも無いときに「対象の kitty を 1 つに決められない（--to で指定する）」と出ていたのを、「kitty が見つからない（kitty の中で動かすか、claude-kitty で起動する）」に分けた。
- `claude-browser`: kitty の外で呼んだときの英語の「no --key, kitty pane or CLAUDE_CODE_SESSION_ID」を日本語にした。

### 上げるときに手で行うこと

- macOS で使うなら、[SETUP.md の macOS での導入](SETUP.md#macos-での導入) の手順で入れ、`tools/install.sh --all` を流す（`--launchd` が入る）。そのあと kitty を ⌘Q で終わらせて `claude-kitty` で開き直す。
- Linux で `$XDG_RUNTIME_DIR` の無い環境（cron・ssh など）で道具を動かしていた人だけ: 記録の置き場が `/tmp/claude-state` などから `/tmp/claude-kitty-<uid>/…` に変わる。動いている claude の色の記録は、次の hook で新しい場所に作り直される（古い `/tmp/claude-state` などは消してよい）。
- それ以外（Linux で `$XDG_RUNTIME_DIR` がある、ふつうの環境）は何もしなくてよい。hook は今の形（素の名前・`~/.local/bin/…`・絶対パス）のまま数えるので、`tools/install.sh --hooks` を流し直しても二重にならない。

## [0.5.0] - 2026-11-20

### 追加

- `claude-browser`: 起動する Chrome を自動で探す。順は、設定の `browser.command` > 下のうちプロファイルの置き場が既にあるもの > ネイティブの Chrome（`google-chrome-stable`・`google-chrome`、macOS は Google Chrome.app）> Flatpak の Chrome（`com.google.Chrome`）> ネイティブの Chromium（`chromium`・`chromium-browser`）> Flatpak の Chromium（`org.chromium.Chromium`）。前は Flatpak の `com.google.Chrome` に決め打ちで、ほかの入れ方の Chrome では動かなかった。探し方は `lib/browser.py`（doctor と共通）。
  - 設定の `browser.command`（文字列のリスト。引数も足せる）で起動のコマンドを決め、`browser.profile_root` でプロファイルと台帳の置き場を決められる（[docs/config.md](docs/config.md#browserclaude-browserclaude-show-重ねる-chrome)）。
  - プロファイルの置き場の既定: Flatpak は `~/.var/app/<アプリ ID>/claude-profiles`（Chrome は前と同じ）、Snap は `~/snap/<名前>/common/claude-kitty-profiles`、それ以外は `$XDG_DATA_HOME/claude-kitty/browser-profiles`（ネイティブの Chromium は `chromium-profiles`。Chrome とは cookie の鍵が違うため）。
  - Flatpak の Chrome を使っていた人は、プロファイル（ログイン）と台帳が前と同じ場所のまま使われる。後から別の Chrome を入れても、置き場が既にある方を選ぶ（Flatpak の Chromium などでも同じ）。
  - 設定ファイルが壊れていても、止まるのは `open` と `move` だけ（`close`・`list`・`status`・`profiles` は既定の探し方で続け、動いている Chrome を止められる）。
  - `flatpak run --branch beta <アプリ ID>` のような、値を次の語に取るオプションを読み違えない。
  - `status` に、使う Chrome のコマンド（`browser`）と置き場（`profile_root`）を出す。Chrome が見つからなければ、kitty や KWin を呼ぶ前に、探したものと設定の書き方を出して止まる。
- 設定の節 `browser`（`config.example.json` にも）。

### 変更

- `claude-state`: 通知の印 `state.notice` の既定を空にした（他のセッションからのメッセージを、どれも普通の依頼と同じに色を変える）。前の既定（「返事は不要」「返事不要」を含む、「【通知】」で始まる）は作者のチームの約束ごとだったので、docs/config.md の例に移した。
- `claude-kitty doctor`: 「Chrome」の行を claude-browser と同じ探し方で出す（どれを使うかとプロファイルの置き場）。設定の `browser.command` が見つからなければ NG。「設定: browser」の行を足した。「KWin」の行は `qdbus6`・`qdbus-qt6` も数え、使えないときは理由（qdbus が無い / KWin が応答しない）を出す。
- `claude-kitty`: kitty を公式インストーラの場所（`~/.local/kitty.app`）に決め打ちせず、無ければ PATH の kitty、macOS の kitty.app を使う。見つからなければそう言って止まる。セッションを省いたとき、`~/.config/kitty/claude3.session`（`install.sh --kitty` のリンク）が無ければリポジトリの `kitty/claude3.session` を使う。
- KWin を呼ぶ所（`claude-browser`・`claude-view` と `claude-show` の保護する窓の確認・`claude-kitty` のデスクトップ移動）: `qdbus` が無ければ `qdbus6`・`qdbus-qt6` を使う（ディストリビューションによって Plasma 6 の qdbus の名前が違う）。`$CLAUDE_KITTY_QDBUS` で場所を指せる。
- PRACTICES.md: 「統括 + 担当」の体制は一例で、1 人で使う・並べるだけでもよいと書いた。どの体制でも効く決まりと、作者の体制・チームに固有の決まり（略号・言い回し・担当は実装しない など）を分け、CLAUDE.md の例も 2 つに分けた。
- README・SETUP.md: Chrome は Flatpak に限らないこと、プロファイルの置き場、qdbus の別名を書いた。

### 修正

- KWin が無い環境（KDE 以外のデスクトップ・qdbus や journalctl が無い）で KWin を呼ぶと、Python の traceback や「KWin スクリプトを読み込めない: ''」だけで止まっていた。何が足りないかと「窓の配置は KDE Plasma の KWin だけ」を出して止まる。

### 上げるときに手で行うこと

0.4.0 から上げるとき。**前の動きのままにしたいとき**は、`~/.config/claude-kitty/config.json` に書き足す（無い節は足す）。

- 他のセッションからの「【通知】」「返事は不要」のメッセージで色を変えない: `"state": {"notice": {"contains": ["返事は不要", "返事不要"], "prefix": ["【通知】"]}}`（書かないと、そのターンも作業中・完了の色になる）。常駐の `claude-state watch` は起動時に設定を読むので、書いたら `systemctl --user restart claude-state-watch`。
- Chrome: Flatpak の `com.google.Chrome` を使っていて前のプロファイルがあるなら、何もしなくても前と同じ Chrome と置き場を使う。ネイティブの Chrome などを入れたあとも必ず Flatpak の Chrome にしたいなら `"browser": {"command": ["flatpak", "run", "com.google.Chrome"]}`（置き場は前と同じ `~/.var/app/com.google.Chrome/claude-profiles`）。
- `claude-kitty doctor` で「Chrome」の行が使いたい Chrome と置き場を指していることを確認する。

## [0.4.0] - 2026-11-20

### 変更

- `claude-browser`: `--mode show` の既定の場所を、新しい `--area others`（自分以外のペインの場所に縦 100%）にした。前は自分のペインの上 0.65 だけで、小さいペインでは画面の 1/5 幅 × 3 割高さほどになり、見せるには小さすぎた。使い分けは、自分だけで確認するなら `--mode self`（既定、自分のペイン）、ユーザーに見せるなら `--mode show`。
  - others: 左の列（統括）を除いた右側のうち、自分のペインの横を除いた左右の隙間の広い方を、右側の全高で使う。広い方が右側の幅の 30% 未満なら右側を半分に分けて自分の中心が無い方、自分が左の列にいれば右側全体。避けるのは呼び出した自分のペインで、`--over` の先ではない。
  - show で `--ratio` を省くと、others では 1.0（全高）。pane・right では前と同じ 0.65。
  - `--area pane` / `--area right` は明示すれば前と同じ（前の show の動きは `--mode show --area pane`）。
  - `--over` を指定して area を省くと `pane`（その先のペインの上。show でも前と同じ）。
  - `open` は、前回が他のペインの場所（`others`・`right`）なら mode を引き継がず self に戻す（claude-show のあとの、`--mode` なしの自分の作業用の `open` が他のペインを覆わないように）。
  - `move` で mode も area も省けば前回の area のまま置き直す（前は open と同じく area を引き継がなかった）。ratio は前回と同じ area のときだけ引き継ぐ。
  - `list` の各行に `area` を足した。
  - `open` / `move` の結果の JSON に `area` を足した。
- `claude-show`: HTML と URL を `--area others` で開く（前は `--area right` で右側全体の上 0.65）。画像は前と同じく自分のペインの overlay。

## [0.3.0] - 2026-11-20

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

他のプロジェクトで初めて使う人に合わせて、特定の運用に寄っていた既定を外した（どれも設定で前と同じにできる）。

- `claude-lang-check`: `lang.language` の既定を無しにした。言語を決めるまでは何もしない（hook を入れただけで、ほかの言語を使う人の返答を書き直させない）。`tools/install.sh` は `--hooks=lang` と明示したときだけ入れる（`--all` には入らない。外すときの `--all --uninstall` は外す）。`claude-kitty doctor` は、入っていないことを注意として出す。
- `claude-kitty`: 仮想デスクトップ 2 への移動と最大化をやめた。設定の `kitty.desktop`（KDE Plasma の仮想デスクトップの番号）と `kitty.maximize` で入れる（既定は移さない・最大化しない。`--start-as normal`）。KWin のスクリプトは `lib/kitty_to_desktop.js` になった。
- `claude-session-stats`: 日付の区切りを JST に固定していたのをやめ、既定をそのマシンのローカルの時刻にした。設定の `session_stats.timezone`（`"Asia/Tokyo"` など）で固定できる。
- docs: `state.notice` の既定（「返事は不要」「【通知】」）は日本語の運用の例で、自分の決まりに合わせて変えるものだと書いた。
- `claude-roster`: 設定の `roster` 節の読み込みを `lib/config.py` の `load_roster_config` に移した（doctor とテストから同じ関数で読むため。動きは同じ）。

### 修正

- `claude-guard`: 区切りを引用したヒアドキュメント（`cat >> README.md <<'EOF' … EOF`）の本文に、止める対象のコマンドの名前が書いてあるだけで止めていた。引用した区切りを見落として本文をコマンドとして読んでいたため。引用符の中（`echo` や `printf` の引数、`git commit -m` の値）とヒアドキュメントの本文は文字列として扱い、コマンドとみない。
  - bash が実際に動かすものは今までどおり読む: `bash -c` / `sh -c` の中身、二重引用符の中と区切りを引用しない `<<EOF` の本文の `$(...)` と `` `...` ``（二重引用符の中の置換は、前の版では読んでいなかった）。
  - 複数行の二重引用符の中の `<<` を、ヒアドキュメントの始まりと取り違えて後ろの行を読み飛ばすことがあった（止めるべきコマンドを見逃しうる）のも直した。
  - 本文を stdin から動かすもの（`bash <<'EOF'`・`sh -s <<EOF`・`ssh host <<'EOF'`）は、本文を `bash -c` の中身と同じに読む。算術式の `<<`（`$((1<<2))`）をヒアドキュメントの始まりとみて、後ろの行を読み飛ばさない。
- `claude-state watch`（と doctor）: kitty のソケットの持ち主が生きているかを `/proc` で見ていたため、macOS では動いている kitty を見つけられなかった。signal 0 で確認する。

### 上げるときに手で行うこと

0.2.0 から上げるとき。`git pull` のあと、次の 1 行で足りる（もう入っているものはそのまま）。

1. **前の既定のままにしたいとき**は、`~/.config/claude-kitty/config.json` に書き足す（無い節は足す）。
   - 返答の日本語の検査: `"lang": {"language": "ja"}`（書かないと claude-lang-check は何もしなくなる）
   - kitty をデスクトップ 2 に最大化して開く: `"kitty": {"desktop": 2, "maximize": true}`
   - session-stats の日付を JST で区切る: `"session_stats": {"timezone": "Asia/Tokyo"}`
   - 0.2.0 の claude-session-stats は知らない項目で止まるので、`timezone` を書くのは `git pull` の後にする。
2. `tools/install.sh --all --dry-run` で何が足されるかを見て、`tools/install.sh --all` を流す。手で入れた hook・リンク・ユニットはそのまま「入っている」と数え、足りないものだけを足す（0.2.0 の手順で `claude-lang-check` を hook に絶対パスで入れ、`~/.local/bin` にリンクしていなければ、そのリンクが足される）。lang の hook は `--all` では足さない（入っていれば残す）。
3. `claude-kitty doctor` で NG が無いことを確認する。
4. kitty.conf を自分のものにしている（リポジトリの `kitty/kitty.conf` へのリンクでない）なら、install.sh が足した include を読ませるために kitty を起動し直す（`allow_remote_control` と `listen_on` は起動時にだけ読まれる）。

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

[Unreleased]: https://github.com/hummer98/claude-kitty/compare/v0.7.6...HEAD
[0.7.6]: https://github.com/hummer98/claude-kitty/compare/v0.7.5...v0.7.6
[0.7.5]: https://github.com/hummer98/claude-kitty/compare/v0.7.4...v0.7.5
[0.7.4]: https://github.com/hummer98/claude-kitty/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/hummer98/claude-kitty/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/hummer98/claude-kitty/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/hummer98/claude-kitty/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/hummer98/claude-kitty/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/hummer98/claude-kitty/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/hummer98/claude-kitty/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/hummer98/claude-kitty/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/hummer98/claude-kitty/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/hummer98/claude-kitty/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/hummer98/claude-kitty/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/hummer98/claude-kitty/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hummer98/claude-kitty/releases/tag/v0.1.0
