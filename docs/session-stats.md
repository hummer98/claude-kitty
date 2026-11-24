# claude-session-stats（会話の記録から週ごとの数字）

Claude Code の会話の記録（`~/.claude/projects/*/*.jsonl`）を読み、統括と担当のセッションについて、直近の期間とその前の期間を比べた Markdown を書く。
ユーザーが同じことを言い直した回数、英語の返答の割合、返答の長さ、他のセッションへのメッセージの数などを、週ごとに見比べるためのもの。

- Python 3 の標準ライブラリだけで動く。記録は 1 行ずつ読み、日ごとの集計だけを持つ（100MB を超える記録でも軽い）。
- 数はすべて、記録を正規表現と決まりで数えたもの。言い回しの揺れで数件のずれがありえる。
- 書き出す前に、メールアドレス・実行時のユーザー名・ホームのフォルダ名・設定の `redact` の語を伏せる（`<email>`・`<user>`）。「<名前>-a1」のようなセッション名は名前を落として「a1」にする。
- 結果には、言い直しの例として**ユーザーの発言の原文**が入る。手元に置くもので、公開する場所には書かない。

## 使い方

```
claude-session-stats                              # 今日までの 7 日と、その前の 7 日
claude-session-stats --end yesterday              # 昨日までの 7 日（今日の分は途中までになるため）
claude-session-stats --end last-sunday            # 今日より前の直近の日曜まで（月〜日の週。タイマー用）
claude-session-stats --end 2026-01-31 --days 14
claude-session-stats --start 2026-01-10 --end 2026-01-31   # 前の期間は同じ日数だけ前
claude-session-stats --out ~/stats/s_{end}.md     # {end} は直近の期間の最後の日
claude-session-stats --stdout
```

- 書く先: `--out FILE` > `--out-dir DIR`（名前は `session_stats_<最後の日>.md`）> 設定の `out_dir` > `$XDG_DATA_HOME/claude-kitty/stats`（既定 `~/.local/share/claude-kitty/stats`）。書いたファイルのパスを 1 行出す。
- 書くときは一時ファイルに書いてから置き換える（途中で止まっても半端なファイルを残さない）。
- `--projects`・`--home-project` で記録の場所を、`--glossary FILE` で用語集のファイルを、その回だけ変えられる。`--no-glossary` で用語集の検査をしない。
- 終了コード: 0 = 書けた、1 = 書けない、2 = 引数か設定が正しくない（`--config` で名指ししたファイルが無いときも）。用語集を読めないときは警告を出して、残りの数字を出す（用語集の欄は「—」）。ツールが返答の途中で例外を出したら、その返答は 0 件と数え、件数を警告と「数え方」に出す。

## 何を数えるか

| 項目 | 数え方 |
|---|---|
| 統括 | ホームのフォルダ（`--home-project`、既定はホームのパスから作る `-home-you` の形）で、agentName か customTitle が `master` の記録。名前の無い記録で uuid が重なるもの（圧縮の前の会話・分岐）も足す。重なった行は uuid で 1 回だけ数える |
| 担当 | `<home><role_dir_prefix>*` のフォルダの記録（名前はフォルダの残り）と、ホームのフォルダで名前が `roles` の記録。`roles` が空なら、名前のある記録を全部その名前で数える。サブエージェントの記録と isSidechain の行は数えない |
| ユーザーの発言 | `type:"user"` の本文（tool_result・isMeta・圧縮の要約・他のセッションからの発言を除く）と、作業中に送った queued_command |
| 言い直し | 設定の `rephrase` の正規表現（発言の全文に `re.search`、`re.M`）。種類ごとに数え、新しく現れた文を 3 つまで例に出す |
| 英語の返答 | 150 字を超え、かな・カナ・漢字の割合が 10% 未満の返答 |
| 冒頭のシステム文 | 返答の先頭が設定の `echo` の文で始まるもの（既定は "The task tools haven't been used recently"） |
| 返答の長さ | ユーザーが次に発言する直前の、最後の返答の字数の中央値 |
| 全員への同じ知らせ | 宛先の名前を伏せた本文の先頭 200 字が似ている（difflib の ratio 0.8 以上）ものを、5 分の内に 3 つ以上のセッションへ送ったもの |
| 他のセッションへのメッセージ | SendMessage のうち、宛先がサブエージェントでないもの |
| 用語集 | 設定の用語集のツールを、統括の返答ごとにかけた件数（設定したときだけ） |

日付の区切りは設定の `timezone`（`"Asia/Tokyo"` など。既定はこのマシンのローカルの時刻）。更新が前の期間より前のファイルは読まない。

## 設定

`~/.config/claude-kitty/config.json`（`$CLAUDE_KITTY_CONFIG` か `$XDG_CONFIG_HOME` で場所を変えられる。リポジトリの外に置く）の `session_stats` の節。どれも省ける。知らない項目や形の違う値があれば、何も書かずに終了コード 2。

```json
{
  "session_stats": {
    "master": "main",
    "roles": ["dev-a", "dev-b"],
    "role_dir_prefix": "-team-",
    "rephrase": [
      ["again", "「もう一度」", "^\\s*もう一度"],
      ["explain", "「説明して」", "説明して|とは？"]
    ],
    "echo": [["task_tools", "\"The task tools haven't been used recently…\"", "\\s*The task tools haven't been used recently"]],
    "redact": ["my-nickname"],
    "glossary": {"module": "~/tools/term_check.py", "file": "~/docs/glossary.md", "scene": "reply"},
    "notes": ["数え方は手元の文書 X に合わせている。"],
    "projects": "~/.claude/projects",
    "home_project": "",
    "out_dir": "~/stats"
  }
}
```

| 項目 | 既定 | 中身 |
|---|---|---|
| `master` | `""` | 統括のセッション名。空なら統括を分けない（用語集の検査もしない） |
| `roles` | `[]` | 担当のセッション名。空なら名前のある記録を全部 |
| `role_dir_prefix` | `""` | 担当ごとのフォルダ（`claude` を `~/<何か>/<担当>` で起動したときの記録のフォルダ）の、ホームのフォルダ名に続く部分。空なら見ない |
| `rephrase` | `[]` | `[key, 表に出す名前, 正規表現]` のリスト。表に 1 行ずつ増える |
| `echo` | 上の 1 つ | `[key, 表に出す名前, 正規表現]`。返答の先頭に `re.match` |
| `redact` | `[]` | 伏せる語（ユーザー名とホームのフォルダ名は必ず伏せる） |
| `glossary` | なし | 用語集のツール。`module` は `parse_glossary(text)` と `check_text(text, rules, scene=...)`（見つけたもののリストを返す）を持つ .py、`file` はその用語集、`scene` は既定 `reply` |
| `notes` | `[]` | 「数え方」の頭に足す文 |
| `timezone` | `""`（ローカルの時刻） | 日付の区切りのタイムゾーン（`"Asia/Tokyo"` など、IANA の名前） |
| `projects` | `~/.claude/projects` | 記録のフォルダ |
| `home_project` | ホームから作る | 統括の記録があるフォルダの名前 |
| `out_dir` | `~/.local/share/claude-kitty/stats` | 書くフォルダ |

## 週に 1 回、自動で出す（systemd のユーザータイマー）

例の unit が `contrib/systemd/` にある。毎週月曜 07:30 に、前の日曜までの 7 日（月〜日）を数える。`Persistent=true` なので、電源が切れていて逃したら、次に起動したときに走る。そのときも `--end last-sunday` なので、期間とファイル名は月〜日の週のまま（火曜に走っても、前の週と重ならない）。記録を全部読むので `Nice=10`・`IOSchedulingClass=idle` で走らせる。

```
cp contrib/systemd/claude-session-stats.{service,timer} ~/.config/systemd/user/
# ExecStart のパス（claude-kitty の場所と --out）を手元に合わせて直す
systemctl --user daemon-reload && systemctl --user enable --now claude-session-stats.timer
systemctl --user list-timers claude-session-stats.timer     # 次の実行時刻
systemctl --user start claude-session-stats.service         # すぐに 1 回走らせる
journalctl --user -u claude-session-stats.service           # 結果（書いたパスか、エラー）
```

## テスト

`python3 tests/session_stats_test.py`（単体テスト。架空の会話の記録を一時フォルダに組み立てて数える）。
