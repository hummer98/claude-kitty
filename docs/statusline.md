# claude-statusline（ステータス行）

Claude Code のステータス行（`settings.json` の `statusLine`）に入れる 1 行のツールです。**入れるかは任意**で、`tools/install.sh` は既定では入れません（`--all` でも入れない）。

```
dev-a | Opus | ctx 380k | sub: 2 | wait: 1 | 2d4h 41.2%
```

| 区切り | 中身 | 色 |
|---|---|---|
| 名前 | stdin の `session_name`（無ければ `<CLAUDE_CONFIG_DIR>/sessions/*.json` の `name` を `session_id` で探す。見つからなければ `?`） | claude-state の状態の色（`claude-state paint` と同じ。既定は待受中が橙 208、完了（未読）が緑、idle が灰） |
| モデル | `model.display_name` | なし |
| `ctx 380k` | いまの会話の長さ（文脈に入っているトークンの数。下の「会話の長さ」） | 既定は 20 万未満なし、20 万以上は淡い黄（186）、40 万以上はくすんだ橙（173） |
| `sub: N` | 動いているサブエージェントの数（`claude-state subagents` と同じ。0 なら省く） | 控えめな青（110） |
| `wait: N` | 自分の役割（claude-state の記録の `role`）の [claude-waits](waits.md) の登録中の待ちの数（0 なら省く。設定 `statusline.waits: false` で出さない） | なし |
| 週の使用量 | `rate_limits.seven_day` の残り時間（`2d4h`・`3h`・`59m`）と使った %（小数 1 桁まで） | % の整数部が 85 を超えたら橙（208）、95 を超えたら赤（196） |

- 名前の色と `sub: N` は claude-state の hook（[SETUP.md の 5.](../SETUP.md#5-状態の色claude-state)）が入っているときだけ。無ければ名前は色なし、`sub` は出ない。
- 週の使用量は設定 `statusline.weekly_roles` の役割だけに出す（既定 `["left"]` = `view.left` の統括。アカウントで 1 つの数なので、全員に出すと同じものが並ぶだけのため。全員なら `["*"]`）。`rate_limits` は claude.ai の Pro / Max などの契約で、最初の応答のあとにだけ来る。無ければ省く。
- しきい値と色は設定 `statusline`（[config.md](config.md#statuslineclaude-statusline-ステータス行)）。

## 会話の長さ

stdin の `context_window` から読みます（[公式の説明](https://code.claude.com/docs/en/statusline)）。

1. `context_window.current_usage` の `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`（`used_percentage` と同じ数え方。出力は入れない）。
2. `current_usage` の鍵そのものが無ければ `context_window.total_input_tokens`（同じ合計）。鍵があって `null` のときは使わない（下）。
3. `context_window` そのものが無い古い Claude Code では、`transcript_path`（会話の記録）の末尾 256 KB から、本体（サブエージェントの行でない）の最後の `usage` の同じ合計。

最初の応答の前と `/compact` の直後は `current_usage` が `null`（か 0）になります。そのときは出しません（記録の最後の `usage` と `total_input_tokens` は圧縮の前の大きさが残りうるので、どちらにも戻らない）。

## 使用量のファイル（usage.json）

週・5 時間の使用量はステータス行の入力（stdin の JSON）にしか来ないので、外のツールが読めるようにファイルに書き出します（行を作ったあと。失敗しても行は変わらない）。アカウントで 1 つの値なので、`weekly_roles` に関わらず、どのセッションも同じファイルに書きます。

```json
{"seven_day": 96.4, "seven_day_resets_at": 1796083200, "five_hour": 12, "five_hour_resets_at": 1795690800,
 "written_at": 1795677137, "written_at_iso": "2026-11-26T16:12:17+09:00"}
```

| 鍵 | 中身 |
|---|---|
| `seven_day` / `five_hour` | `rate_limits.<窓>.used_percentage`（%、入力のまま） |
| `seven_day_resets_at` / `five_hour_resets_at` | `rate_limits.<窓>.resets_at`（入力のまま。Claude Code は epoch 秒の数） |
| `written_at` / `written_at_iso` | 書いた時刻（epoch 秒 / オフセット付きのローカル時刻） |

- 値が無い・`-1`・数でない項目は書かない。7 日も 5 時間も無い（`rate_limits` が無い契約・最初の応答の前）ならファイルを書かず、前の値を残す。
- 入力の `rate_limits` はそのセッションの最後の応答の値なので、止まっているセッションは古い値を送り続けます。そのまま「最後に書いたもの」にすると、動いているセッションの値と行き来したり、戻る時刻を過ぎた前の窓の値で書き直したりするので、前のファイルと窓ごとに合わせます。
  - 戻る時刻を過ぎた値は採らない（入力も前のファイルも）。
  - 同じ窓（戻る時刻の差が 1 時間未満）なら % の大きい方（窓の中では減らないため）。違う窓なら戻る時刻の新しい方。戻る時刻の分からない値は比べられないので入力を採る。
  - 入力に無い窓は前の値を残す。
- 置き場は `$XDG_STATE_HOME/claude-kitty/usage.json`（既定 `~/.local/state/…`）。0600、フォルダは 0700。同じフォルダの一時ファイルに書いてから置き換えるので、読む側が書きかけを見ることはない。
- `CLAUDE_CONFIG_DIR` が `~/.claude` 以外のセッションは、別のアカウントでありうるので `usage-<その置き場の実パスの sha1 の先頭 8 桁>.json` に書く（上書きし合わないように。名前でなくハッシュにしたのは、`/a/.claude` と `/b/.claude` のような同じ名前の置き場がぶつからないため）。場所は、そのセッションと同じ環境で `claude-statusline --usage-file` を打つと出る。
- 中身（`written_at` を除く）が前と同じで、前に書いてから 60 秒以内なら書き直さない（30 秒ごと・会話の出来事ごとに書かないため。判定は前のファイルを読むだけ）。
- 置き場は設定 `statusline.usage_file` で変えられ、`false` で書かない（[config.md](config.md#statuslineclaude-statusline-ステータス行)）。
- 書けなかったときは `$XDG_RUNTIME_DIR/claude-statusline/errors.log` に残す（直前と同じ文なら書かない）。

### しきい値で判定する（claude-statusline usage）

```sh
claude-statusline usage [--over [N]] [--window seven_day|five_hour] [--file PATH]
```

usage.json を読み、窓（既定 `seven_day`）の % が N 以上なら終了コード 0、未満なら 1、引数や設定の誤りは 2 です。N を省けば設定 `statusline.usage_warn_percent`（既定 96）。stdout に 1 行を出します。

```
seven_day 96.4% ≥ 96%（戻る 12/02 09:00）
```

- ファイルが無い・壊れている・その窓の値が無いときは 1（未満と同じ）。
- いまが戻る時刻（`<窓>_resets_at`）を過ぎていれば、前の窓の値なので 1（戻ったあと新しい応答を受けたセッションがまだ無くても、止め続けないため）。
- 設定が壊れていても既定（96・既定の置き場）で判定し、stderr に「設定の誤り」と出す（claude-waits は 0 以外をどれも「成立しない」と扱うので、誤りで 2 を返すと止める待ちが黙って効かなくなるため）。2 は引数の誤りと、`usage_file: false` で `--file` も無いときだけ。

claude-waits の `--when-cmd` からそのまま使えます（成立したときの stdout が知らせの本文に添えられる）。

```sh
claude-waits add usage-96 --role main --why "7 日の使用量が 96% に達したら作業を止める" \
  --then "全セッションの作業を止め、戻る時刻まで休ませる" \
  --when-cmd "claude-statusline usage --over" --every 2m --expires 8d
```

- claude-waits の常駐の環境には `CLAUDE_CONFIG_DIR` が無いことが多いので、`~/.claude` 以外のアカウントの値を見るときは `--file`（`claude-statusline --usage-file` で出た場所）を渡す。常駐の PATH に claude-statusline が無ければ絶対パスで書く（[waits.md](waits.md)）。
- jq で読むなら `jq -e '.seven_day >= 96' ~/.local/state/claude-kitty/usage.json` でもよい（前の窓の値の扱いは無い）。

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
- 入れたか・設定が読めるかは `claude-kitty doctor`（「statusLine」の行に claude-statusline か・別のものか・無しか、「設定: statusline」の行に設定が読めるか。statusLine は任意なので、claude-statusline なのにツールが見つからないときだけ NG）。

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
