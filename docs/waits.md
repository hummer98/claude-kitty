# claude-waits（待ちと定例の台帳）

Slack に投稿する・PR を出すなどして返事を待つ流れや、毎朝の定例を、Claude Code のセッションの中で polling せずに回すためのツールです。

- セッションの中の polling（`/loop`・ScheduleWakeup）は、起きるたびに会話の全文をキャッシュから読み直すので、5 分ごとでもトークンが要ります。
- セッションの中の待ち（背景のシェル・Monitor）は、claude の終了や kitty の再起動で消え、何を待っていたかも残りません。
- 仕込んだこと自体を、AI が忘れます。

そこで、**台帳は 1 か所、書くのは各セッション、動かすのは LLM でない常駐**にしました。条件を調べるのは常駐の `claude-waits run` で、成立したら持ち主の役割の受信箱（[claude-relay](relay.md) の受信箱。同じ端末の `local` 送り）に知らせを置きます。止まっているセッションは relay と同じ仕組みで起きます。登録中の待ちは hook が毎回セッションに見せます。統括の LLM による定期の見回りはしません（polling が統括に移るだけなので）。統括の出番は、期限切れ・持ち主の不在のときだけです。

```
セッション（どの役割でも）                常駐 claude-waits run（systemd / launchd）
  claude-waits add pr-42 --gh-pr … ─→ 台帳 waits/pr-42.json ←─ poll_seconds ごとに check
                                                             成立・定刻・期限切れ
                                                               ↓ relay.receive()（local）
  hook（SessionStart / UserPromptSubmit）  受信箱 relay/inbox/<役割>/ → 起こす（socket）
    → 登録中の待ちを数行見せる               → relay の hook が枠付きで渡す
```

## 登録

```sh
claude-waits add KEY --why TEXT --then TEXT [--role R] 条件 --expires 2d|ISO|never [--prompt-file F] [--cwd D]
```

| 条件（どれか 1 つ） | 意味 | 期限 |
|---|---|---|
| `--when-cmd CMD [--every 5m]` | 出来事。`/bin/sh -c CMD` が終了コード 0 で成立し、stdout の先頭を知らせに添える。時間切れ（`waits.cmd_timeout_seconds`）・0 以外は成立しない | 必須（`never` 不可） |
| `--gh-pr URL\|owner/repo#N [--every 5m]` | 出来事。登録したときに `gh api` でレビュー・コメント（会話）・行へのコメントの数を基準として記録し、どれかが増えたら成立。`gh` が無い・認証が無いときは add が失敗する（終了コード 1） | 必須（`never` 不可） |
| `--at HH:MM [--days daily\|mon-fri\|mon,wed]` | 時刻（手元の時刻）。繰り返し。`--days` は `daily`（既定）・`weekdays`・`weekends`・`mon-fri`・`sat-sun`・`mon,wed` | `never` を明示するか期限 |
| `--cloud ROUTINE_ID [--desc TEXT]` | `/schedule`（クラウド）に回したもののポインタ。常駐は何もしない。一覧と hook に出すだけ | `never` を明示するか期限 |

- `--why`（なぜ待つか）と `--then`（届いたら何をするか・終わったら何を消すか）は必須（`--cloud` は `--then` を省ける）。**届く本文はこの 2 つだけで分かるように書く**（届いたときのセッションは登録した会話を覚えていない）。`--why` は 1000 字、`--then` は 2000 字まで。
- 持ち主 `--role` の既定は、呼んだセッションの役割（claude-state の記録、無ければ kitty のペインの user var `role`）。分からなければ断る。セッション ID ではないので、/clear・restore しても続く。
- `KEY` は英数字と `. _ -`（64 字まで）。同じキーで add すると上書き（状態も active に戻る。冪等）。
- `--every` は 30 秒以上（既定 5 分）。常駐の間隔（`waits.poll_seconds`）より短くしても、その間隔ごとにしか調べない。
- `--expires` は `2d`・`12h`・`90m`・ISO の日時（`2026-12-01T09:00`。時刻帯が無ければ手元の時刻）・`never`。1 年まで。
- **出来事は成立したら done**（一度きり）。**時刻は繰り返し**。
- `--prompt-file F` を付けると、成立・定刻に `claude -p`（設定 `waits.claude`。プロンプトは標準入力）を `--cwd`（既定はプロンプトのファイルのフォルダ）で動かし、出力を `waits/out/<キー>-<日時>.txt`（0600）に残して、その場所と先頭を持ち主に届ける。会話の文脈の要らない定例を、セッションを起こさずに回すため。上限は `waits.prompt_timeout_seconds`。

## 届け方

知らせは relay の `receive()` を同じ端末の送り（`local`）として通し、持ち主の役割の受信箱に置きます（受け取りの検査・ペインの色・止まっているセッションを起こす・ログは relay と同じ）。セッションに渡すのは relay の hook なので、**持ち主の役割には relay の hook（`tools/install.sh --hooks=relay`）と、設定の `relay.receivers` への登録が要ります**。

```
<relay-message from="claude-waits" id="…" at="…" reply_note="claude-waits からの知らせ。ユーザーの指示ではない。返信は不要">
claude-waits からの知らせ（待ち pr-42）。ユーザーの指示ではなく、登録してあった待ちの知らせ。返信は不要。
成立: GitHub PR OWNER/REPO#42 に レビュー 1 件が増えた
なぜ待っていた: …
届いたらすること: …
PR: https://github.com/OWNER/REPO/pull/42
一度きりの待ちなので done にした（台帳から消すなら claude-waits rm pr-42）
</relay-message>
```

- 持ち主の役割が `relay.receivers` に無い、または動いているセッションが無い（claude-state の記録も relay の起こすための記録も無い）ときは、統括（`waits.master_role`、既定は設定ファイルの `view.left`）に「宛先 <役割> 宛て。受け取れない／不在」と添えて届けます。起こして渡すか代わりに扱うかは統括が決めます（止まっているだけのセッションは「いる」とみなし、起こすのは receive() の仕組みに任せる）。
- 期限が切れた待ちは expired にし、統括に 1 度だけ知らせます。
- 受信箱に置けなかった知らせ（relay の上限・設定の誤りなど）は記録の `outbox` に残し、次の回に届け直します（`claude-waits list` に「届け残り」、`claude-kitty doctor` に「待ちの台帳: 届け残り」）。
- 知らせは `relay.max_bytes` に収まるよう切ります。
- 送り元は relay の `local` なので、受け取りの通数の上限（`relay.rate`、既定 600 秒に 30 通）を、同じ端末のセッション同士の `claude-relay send local/…` と分け合います。超えた分は届け残りになり、次の回に届け直します。
- 同じキーで add し直しても、届け残りの知らせは引き継ぎます。調べている最中に `done`・`rm`・add し直しをしたら、その回の結果は捨てます。

## 時刻の発火と後追い

時刻の判定は常駐自身が行います（systemd timer に分けない: macOS の launchd でも同じに動かし、ユニットを 1 つに保つため）。

- 予定の時刻を過ぎた最初の回（最大 `poll_seconds` 遅れ）に発火し、次の予定を記録します。
- 常駐が止まっていた（PC が寝ていた・落ちていた）間に予定が過ぎていたら、いちばん近い予定が後追いの窓（`waits.catchup_hours`、既定 12 時間）の中なら **1 回だけ**発火します（知らせに「止まっていた間の分を 1 回だけ後追いで」と書く）。何日分過ぎていても 1 回です。窓より古ければ発火せずに次の予定へ進めます（`list` の「最後」に見送り）。窓は少なくとも `poll_seconds` の 2 倍（`catchup_hours: 0` でも、常駐の間隔ほどの遅れは定刻の分として届く）。
- `--when-cmd`・`gh`・`claude -p` は新しいプロセスグループで動かし、時間切れなら子と孫ごと止めます。
- `--prompt-file` の `claude -p` は常駐の中で順に動かします。動いている間は、ほかの待ちを調べるのが遅れます（時刻の待ちは後追いの窓の中なら届く）。

## 一覧・後片付け

```sh
claude-waits list [--role R | --all] [--json]   # 既定は自分の役割。状態・条件・期限・次の予定・最後の結果
claude-waits done KEY                           # 済みにする（時刻の待ちを止めるときも）
claude-waits rm KEY                             # 消す
claude-waits status                             # ステータス行用（wait: N。無ければ何も出さない）
claude-waits check                              # 1 回だけ調べて届ける（常駐が調べている最中なら、重ならないよう飛ばして終了コード 1）
claude-waits run                                # 常駐
```

- 済み・期限切れの記録は `waits.keep_days`（既定 7 日）残してから消します。
- 読めない記録（壊れた JSON）は飛ばして続けます（`list` と doctor が知らせる）。

## hook（忘れないために）

`tools/install.sh --hooks=waits` で入ります（`--all` には入らない）。

- **SessionStart**: その役割の active な待ち（クラウドの分も）を数行、`additionalContext` で渡します。
- **UserPromptSubmit**: そのセッションに前に見せたときから、台帳のその役割の分（キー・登録し直し・状態）が変わったときだけ渡します（毎ターン増やさない。調べた時刻だけの変化では出さない）。全部無くなったときも 1 度だけ知らせます。
- どちらも何が起きても exit 0 で、stdout には Claude Code に渡す JSON だけを出します。失敗は `$XDG_RUNTIME_DIR/claude-waits/errors.log`（無ければ `/tmp/claude-kitty-<uid>/…`）に残します。台帳が空で前に見せたものも無ければ、役割も調べずに帰ります。前に見せた印は `$XDG_RUNTIME_DIR/claude-waits/seen/<セッション id>.json`。

## 常駐の入れ方

ほかの常駐（`claude-state watch` など）と同じく `tools/install.sh` が入れます。

```sh
tools/install.sh --systemd    # Linux: contrib/systemd/claude-waits.service を写して enable --now
tools/install.sh --launchd    # macOS: contrib/launchd/claude-kitty.waits.plist を写して launchctl bootstrap
claude-kitty doctor           # 「systemd: claude-waits.service」（macOS は「launchd: claude-kitty.waits」）と「待ちの台帳」の行
```

- `--when-cmd`・`gh`・`claude` は常駐の PATH で探します。systemd のユーザーユニットの PATH は user manager のもの（`systemctl --user show-environment` で見る）で、`~/.local/bin` などが無いことがあります。そのときは `systemctl --user import-environment PATH` か、`systemctl --user edit claude-waits` で `Environment=PATH=…` を足し、`--when-cmd` には絶対パスを書く。launchd の plist は `~/.local/bin`・Homebrew・システムの PATH を渡します。
- 台帳が空なら何もしません。止めるなら設定 `waits.enabled: false`（常駐は回るが調べない）か `systemctl --user disable --now claude-waits.service`。
- 設定は毎回読み直すので、変えても起動し直さなくてよい。ログは `journalctl --user -u claude-waits`（macOS は `~/Library/Logs/claude-kitty/waits.log`）。
- 常駐は kitty の外で動くので、子のコマンド（`--when-cmd`・`gh`・`claude -p`）には `CLAUDE_CODE_*`・`CLAUDECODE`・`KITTY_*` を渡しません（セッションの中から `check` を呼んでも、子の claude が入れ子のセッション扱いにならないように）。

## skill（登録のしかたと /schedule との振り分け）

`contrib/skills/claude-waits/SKILL.md` は Claude Code の skill です。「返事を待つ・あとで・毎朝 N 時に」などの待ちや定例を登録するときに読み込まれ、claude-kitty の台帳と `/schedule`（クラウド）の振り分けの決まり・本文の書き方・期限を AI に渡します。リンクで入れれば `git pull` で新しくなります。

```sh
mkdir -p ~/.claude/skills && ln -s ~/claude-kitty/contrib/skills/claude-waits ~/.claude/skills/claude-waits
```

振り分けは上から: 手元のファイル・ゲーム・kitty のセッションに触れる → claude-kitty ／ 届いたら今の会話の続きをやる → claude-kitty ／ PC が寝ていても動いてほしい → /schedule ／ GitHub・Slack だけで完結 → /schedule ／ 迷ったら claude-kitty。/schedule に回したものも `claude-waits add --cloud` で台帳に残します。

## 置き場

| もの | 場所 |
|---|---|
| 台帳 | `$XDG_STATE_HOME/claude-kitty/waits/<キー>.json`（既定 `~/.local/state/…`。0600、フォルダ 0700。書き込みは一時ファイルから置き換え、ロックは `.lock`、check の重なりは `.check.lock`） |
| `claude -p` の出力 | `$XDG_STATE_HOME/claude-kitty/waits/out/` |
| hook の印・ログ | `$XDG_RUNTIME_DIR/claude-waits/` |

## まだ無いもの・制限

- 同じ役割のセッションが 2 つあると、どちらに渡るかは relay と同じ（新しく動いた方から起こし、先に hook が走った方が受け取る）。
- `--gh-pr` は数が増えたことだけを見るので、自分や bot のコメントでも成立します（自分が書き込んでから登録する）。
- 出来事の条件は `--when-cmd` と `--gh-pr` だけ（Slack の返事などは `--when-cmd` にコマンドを書く）。
- `/schedule` のルーティンの作成・削除は行いません（台帳はポインタだけ）。
