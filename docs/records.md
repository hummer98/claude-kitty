# 後から数える記録（hook が書く jsonl）

`claude-guard` と `claude-state` の hook（と kitty の watcher）は、後から「どこで時間がかかったか」を数えるための記録を 1 行 1 件の JSON で残す。
ガードに止められた回数、人の待ちと担当の稼働の割合、ほかのセッションからのメッセージの数、タスクリストの札の動きは、会話の記録からは拾いきれない（拾えても下限にしかならない）ため。

- 置き場は `$XDG_STATE_HOME/claude-kitty/`（既定 `~/.local/state/claude-kitty/`。macOS も同じ）。新しく作るフォルダは 0700、ファイルは 0600（前からある `claude-kitty` のフォルダの権限は変えない）。
- `XDG_STATE_HOME` も `HOME` も無い環境では書かない。
- 1 つのファイルが 1 MB を超えたら、`<名前>.1` に回す（前の `.1` は消える）。長く残すなら、ほかへ写す。回すときにロックは取らないので、2 つの hook が同時に回すと、まれに `.1` の分が消える。
- 書けないとき（置き場がファイルになっている・権限が無い）は黙って飛ばす。hook の働き（止める・色を変える）は変わらない。`claude-kitty doctor` の「記録（後から数える）」の行で、置き場とファイルの大きさが分かる。
- 時刻 `at` はローカル時刻の ISO 8601（秒まで、時差付き。例: `2026-01-02T09:00:00+09:00`）。
- 設定は無い（いつも書く）。

| ファイル | 書くもの | 書く時 |
|---|---|---|
| `guard/blocks.jsonl` | claude-guard | ツールの呼び出しを止めた（deny した）とき |
| `state/transitions.jsonl` | claude-state（と kitty の watcher・`claude-state watch`・claude-relay の印） | ペインの状態が変わったとき |
| `state/messages.jsonl` | claude-state（UserPromptSubmit） | ほかのセッションからのメッセージでターンが始まったとき |
| `state/tasks.jsonl` | claude-state（PostToolUse） | `TaskCreate`・`TaskUpdate` が通ったとき |

どの行も `role`（kitty のペインの役割。claude-state の記録から読む。分からなければ `null`）と `sid`（セッション ID）を持つ。

## guard/blocks.jsonl（ガードの止め）

```json
{"at":"2026-01-02T09:00:00+09:00","ev":"guard-block","role":"dev-a","sid":"…","guard":"app-running","kind":"command","cmd":"svcctl deploy v1"}
```

- `guard`: 止めたガードの名前。until / while のループの中の `pgrep -f`（[guard-hook.md](guard-hook.md#待つシェル自身に一致する-pgrep--f-のループ)）は `wait-loop`。
- `kind`: `path`（保護したパスへの書き込み。Edit / Write / NotebookEdit はいつもこれ）・`command`（`commands` の形だけで止めた）・`loop`（`wait-loop`）。Bash でパスとコマンドの形の両方に当たったら `path`。
- `cmd`: Bash はコマンドの頭 200 字。ファイルのツールは `<ツール名> <パス>`。
- 1 回の呼び出しを複数のガードが止めたら、ガードごとに 1 行。
- `claude-guard check`（手で判定するコマンド）は書かない。hook だけ。
- 数え方: 行の数がそのまま止めた回数。止められた後に同じコマンドを出し直して、また止められた分も数に入る（`sid` と `cmd` が同じで続く行）。

## state/transitions.jsonl（状態の移り）

```json
{"at":"2026-01-02T09:00:00+09:00","role":"dev-a","sid":"…","from":"working","to":"waiting","reason":"permission"}
```

- 状態の名前は claude-state のまま: `working`（作業中）・`waiting`（待受中: 許可の確認・AskUserQuestion・`claude-state waiting`）・`done`（終わって未読）・`idle`（見た後）。セッションの終わり（SessionEnd）は `to` が `end`、セッションの最初の行は `from` が `null`。同じペインで新しいセッションに替わった（`/clear` など）ときは、前のセッションに `end` の行を書く。
- `reason`: `to` が `waiting` のときだけ。待ちの理由のいちばん強いもの: `manual`（`claude-state waiting`）・`ask`（AskUserQuestion）・`permission`（許可の確認）・`notify`（そのほかの入力待ちの通知）・`relay`（claude-relay の受信箱に届いて渡していない）。待ちの間に理由だけが変わっても行は足さない。
- 同じ状態への移りは書かない（ほとんどの PreToolUse / PostToolUse は何も書かない）。
- claude-relay が受信箱に届いたときに付ける印 `relay`（`waiting` になる）と、渡して外したときの移りは claude-relay が書く。
- `done` → `idle`（ユーザーがペインを見た）は kitty の watcher か `claude-state watch` が書く。kitty の watcher は kitty が設定を読み直したときに入れ替わるので、それまでは前の版のままで、この行は書かれない。
- kitty の外のセッション・入れ子の claude（ツールの中で動かした `claude -p`）・`CLAUDE_KITTY_STATE=off` は書かない（claude-state が記録を持たないため）。
- 数え方: 同じ `sid` の行を時刻の順に並べ、ある行から次の行までをその行の `to` の時間とする。役割ごとに `working` の和が「担当の稼働」。`waiting` のうち `reason` が `manual`・`ask`・`permission` のものの和が「人の待ち」（`relay` は統括が渡すまでの待ち、`notify` は人とは限らない）。最後の行の後は、今か `end` の行まで。claude が落ちて SessionEnd が来なかったセッションは `end` が無いので、次の `SessionStart`（同じ `sid` で `from` が前の状態）か、ほどよい上限で切る。

## state/messages.jsonl（届いたメッセージ）

```json
{"at":"2026-01-02T09:00:00+09:00","ev":"msg-in","role":"main","sid":"…","from":"dev-b","via":"cross-session","bytes":523}
```

- UserPromptSubmit の `prompt` にある `<cross-session-message …>` の 1 つずつ。`from` はその `from-name`（無ければ `from`）。
- 中に claude-relay の起こす知らせの印（`[relay-wake ids=…]`）があれば `via` は `relay`。`from` は知らせの「<送り元>から」（無ければ枠の送り元）、`n` は印の id の数（知らせ 1 つで何通届いたか）。枠の無い印だけのものも `relay` として数える。
- `bytes`: 枠の中の本文の UTF-8 のバイト数（`relay` は知らせの文の長さ。本文は受信箱にある）。通数は `cross-session` が 1 行 1 通、`relay` は `n` を足す。
- **下限になる**: 作業中のターンの途中に届いたメッセージは、Claude Code がそのターンの中に差し込み、UserPromptSubmit の hook を通らないことがある。この記録はターンの始まりになったメッセージだけを確実に数える。届いた数そのものは、送った側の記録（claude-relay の `relay/log.jsonl` など）と合わせて見る。

## state/tasks.jsonl（タスクリストの札）

```json
{"at":"2026-01-02T09:00:00+09:00","ev":"task","role":"dev-a","sid":"…","id":"3","subject":"…","status":"pending","op":"create"}
{"at":"2026-01-02T09:05:00+09:00","ev":"task","role":"dev-a","sid":"…","id":"3","subject":null,"status":"in_progress","op":"update"}
```

- PostToolUse（成功したツールの呼び出し）の `TaskCreate`・`TaskUpdate` から。失敗（PostToolUseFailure と `"success": false` の応答）は書かない。
- `TaskCreate`: `id` は応答の `task.id`、`subject` は入力の `subject`（無ければ応答の `task.subject`）、`status` は `pending`（作ったばかりの札）。
- `TaskUpdate`: `id` は入力の `taskId`。`subject` と `status` は変えたときだけ入り（入力の `subject`・`status`、無ければ応答の `statusChange.to`）、変えていなければ `null`。今の題は、同じ `sid` と `id` の前の行から引き継いで読む。
- `subject` は頭 120 字。題の頭に付ける札（だれを待っているか、など）の決まりはチームの約束で、ここでは決めない。札ごとの時間は、`subject` が変わった時刻と `status` が `completed`・`deleted` になった時刻の間で数える。
- サブエージェントが付けた札は `agent`（サブエージェントの id）も持つ。
- タスクリストはセッションごとなので、同じ `id` でも `sid` が違えば別の札。
