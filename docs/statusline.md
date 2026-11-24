# claude-statusline（ステータス行）

Claude Code のステータス行（`settings.json` の `statusLine`）に入れる 1 行の道具です。**入れるかは任意**で、`tools/install.sh` は既定では入れません（`--all` でも入れない）。

```
dev-a | Opus | ctx 380k | sub: 2 | 2d4h 41.2%
```

| 区切り | 中身 | 色 |
|---|---|---|
| 名前 | stdin の `session_name`（無ければ `<CLAUDE_CONFIG_DIR>/sessions/*.json` の `name` を `session_id` で探す。見つからなければ `?`） | claude-state の状態の色（`claude-state paint` と同じ。既定は待受中が橙 208、完了（未読）が緑、idle が灰） |
| モデル | `model.display_name` | なし |
| `ctx 380k` | いまの会話の長さ（文脈に入っているトークンの数。下の「会話の長さ」） | 既定は 20 万未満なし、20 万以上は淡い黄（186）、40 万以上はくすんだ橙（173） |
| `sub: N` | 動いているサブエージェントの数（`claude-state subagents` と同じ。0 なら省く） | 控えめな青（110） |
| 週の使用量 | `rate_limits.seven_day` の残り時間（`2d4h`・`3h`・`59m`）と使った % | % の整数部が 85 を超えたら橙（208）、95 を超えたら赤（196） |

- 名前の色と `sub: N` は claude-state の hook（[SETUP.md の 5.](../SETUP.md#5-状態の色claude-state)）が入っているときだけ。無ければ名前は色なし、`sub` は出ない。
- 週の使用量は設定 `statusline.weekly_roles` の役割だけに出す（既定 `["left"]` = `view.left` の統括。アカウントで 1 つの数なので、全員に出すと同じものが並ぶだけのため。全員なら `["*"]`）。`rate_limits` は claude.ai の Pro / Max などの契約で、最初の応答のあとにだけ来る。無ければ省く。
- しきい値と色は設定 `statusline`（[config.md](config.md#statuslineclaude-statusline-ステータス行)）。

## 会話の長さ

stdin の `context_window` から読みます（[公式の説明](https://code.claude.com/docs/en/statusline)）。

1. `context_window.current_usage` の `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`（`used_percentage` と同じ数え方。出力は入れない）。
2. それが無ければ `context_window.total_input_tokens`（同じ合計）。
3. `context_window` そのものが無い古い Claude Code では、`transcript_path`（会話の記録）の末尾 256 KB から、本体（サブエージェントの行でない）の最後の `usage` の同じ合計。

最初の応答の前と `/compact` の直後は `current_usage` が `null`（か 0）になります。そのときは出しません（記録の最後の `usage` は圧縮の前の大きさなので、記録には戻らない）。

## 入れ方（任意）

`~/.claude/settings.json`（`$CLAUDE_CONFIG_DIR`）に足します。**`statusLine` は 1 つしか持てません。** 既に自分の `statusLine` があるなら、置き換える前にその `command` を控えておいてください（中身のスクリプトに claude-statusline に無い項目があれば、そのスクリプトから `claude-statusline` を呼んで後ろに足すこともできる）。

```json
{
  "statusLine": {"type": "command", "command": "/path/to/claude-kitty/bin/claude-statusline", "refreshInterval": 30}
}
```

- `command` は絶対パスで書くのがよい（GUI などから起動した Claude Code は PATH に `~/.local/bin` が無いことがある）。
- `refreshInterval`（秒）は、会話が止まっている間（統括がサブエージェントを待っているときなど）も `sub: N` と週の残り時間を進めるため。省くと会話の出来事のときだけ書き直される。

`tools/install.sh --statusline` でも入れられます（変えるものを見せ、確認を取ってから書く。`settings.json.bak-<日時>` を残す）。

- `statusLine` が無いときだけ入れる。別の `statusLine` があれば置き換えず、`!` の行で知らせて何もしない。
- `tools/install.sh --uninstall --statusline`（`--all --uninstall` でも）は、claude-statusline の `statusLine` だけを外す（別のものは触らない）。
- 入れたか・設定が読めるかは `claude-kitty doctor`（「設定: statusline」の行）。

## 確かめる

```sh
echo '{"session_name":"dev-a","model":{"display_name":"Opus"},"context_window":{"current_usage":{"input_tokens":5,"cache_read_input_tokens":250000}}}' | claude-statusline; echo
```

## 失敗したとき

ステータス行が空になると何が起きたか分からないので、どこで失敗しても何かしら 1 行を出し、終了コードはいつも 0 です。

- stdin が空・JSON として読めない: `?`
- 1 つの区切りが失敗した（記録が読めないなど）: その区切りだけを省く。
- 設定が壊れている: 既定で出し、末尾に「設定の誤り（claude-kitty doctor）」。
- それでも失敗した: `claude-statusline: <例外の名前>`。

呼ばれるたびに 1 つの python3 で済ませます（jq や claude-state を別のプロセスで起こさない）。手元では 1 回 40 ms ほどです。
