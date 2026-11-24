# claude-waitfor（上限時間付きで待つ）

Claude Code のセッションが「テストが終わるまで」「サブエージェントの出力に印が出るまで」を待つための道具。
Bash で until / while のループを書く代わりに使う。必ず上限時間で終わる。

```sh
claude-waitfor proc 'luajit tests/run.lua'                          # 一致するプロセスが無くなるまで待つ
claude-waitfor file "$F" '"stop_reason":"end_turn"' --timeout 2h    # ファイルに文字列が現れるまで待つ
```

## なぜ

実際に起きたこと（2026-11）:

- `until ! pgrep -f "luajit tests/run.lua" >/dev/null; do sleep 2; done; ...` で待ったセッションが、15〜19 時間終わらなかった。
  Claude Code は Bash のコマンドを `bash -c '<コマンド>'` として動かすので、待つシェル自身のコマンド行にパターンの字が入り、
  `pgrep -f` がそのシェルに一致し続けた。
- `until grep -q '"stop_reason":"end_turn"' $F; do sleep 5; done` は、目印が出ないまま 24 時間残った。

こうした背景のシェルが残ると、`~/.claude/sessions/<pid>.json` の `status` が `"shell"` のままになり、
`claude-roster restart` が「idle でない（status=shell）」で断る。

対策は 3 つ:

1. `claude-waitfor`（このページ）: 自分に一致せず、上限時間で必ず終わる待ち方。
2. `claude-guard` の hook: until / while のループの中の `pgrep -f` を、ガードの設定に関係なく止め、`claude-waitfor` を案内する（[guard-hook.md](guard-hook.md#待つシェル自身に一致する-pgrep--f-のループ)）。
3. `claude-roster status` と `claude-kitty doctor`: 2 時間を超えて続く背景のシェルを 1 行ずつ出す（[下](#長く続く背景のシェルを見る)）。

## proc

```
claude-waitfor proc <パターン> [--timeout 30m] [--interval 2]
```

パターンに一致するプロセスが無くなるまで待つ。

- 一致の決まりは `pgrep -f` と同じ: 引数を空白でつないだ完全なコマンド行に、パターンを**正規表現**として探す（Python の `re.search`。`re.S` で、`.` は改行にも当たる。pgrep の regexec と、claude-guard のループの判定と同じ。ERE と細部は違いうる）。部分一致で、頭から合わせるなら `^` を付ける。
- 数えないもの: 自分自身、自分の祖先（呼び出したシェル・`timeout` など）、祖先から fork しただけの子（パイプやサブシェル `{ …; } | cat` で、祖先と同じコマンド行のまま動いているもの）。ほかの `claude-waitfor` と、それを呼んだシェル（`bash -c`・`timeout` などが続く間だけ上る）も数えない（2 つのセッションが同じものを待つと、互いの待ちのコマンド行に一致して、どちらも時間切れまで待ってしまうため）。それ以外の、祖先でない別のシェルは、同じ字を含んでいれば数える（`pgrep -f` と同じ。手で書いた `until` のループなど）。
- 読めないプロセス（ほかのユーザーのもの・終わりかけのもの）は飛ばす。
- 除外の限界: 祖先は親の pid をたどって決め、fork の子は「親と同じコマンド行のまま」で見分ける。そのため、祖先のシェルが
  別のコマンドを `exec` しないまま、同じ字を含む**別の**コマンド行で動かしている子（`bash -c "…"` で中にもう一つ同じ字の
  シェルを開いたものなど）は数える。親が先に終わって init などに付け替わったプロセスは祖先の子と分からないので数える。
  ほかの claude-waitfor の呼び元のシェルは、`bash`・`sh`・`zsh`・`timeout`・`env`・`nohup` などが続く間だけさかのぼる。
  そのため、待つ対象が中で claude-waitfor を呼ぶシェルスクリプト（`bash run_all.sh` の中で `claude-waitfor …` を待つなど）だと、そのスクリプト自身を数えず、早く終わる。
- 初めから一致するものが無ければ、すぐに 0 で終わる。

## file

```
claude-waitfor file <path> <文字列> [--timeout 30m] [--interval 2]
```

ファイルに文字列が現れるまで待つ。

- 文字列はそのまま探す（正規表現ではない。UTF-8 のバイト列で比べる）。
- ファイルがまだ無くてもよい（できるまで待つ）。フォルダを渡すと引数の誤り（2）。
- 前回の続きから読む（大きなログを毎回頭から読まない）。境目で切れた文字列も見つける。次のときは頭から読み直す: 置き換わった（inode が変わった）、縮んだ、mtime が戻った、頭の 4096 バイトが前に読んだものと違う（同じ inode に `>` で書き直したなど）。
- 限界: 同じ inode に、頭の 4096 バイトまで同じ中身で、前と同じ長さ以上に、mtime を戻さずに書き直したものは見分けられない（続きの位置から先しか読まない）。

## 上限・間隔・終了コード

| | |
|---|---|
| `--timeout` | 既定 `30m`。`90s`・`2h`・秒の数（`45`）。24 時間まで。**`0`（無制限）は受けない**（待ちが残り続けるのを防ぐための道具なので） |
| `--interval` | 調べる間隔の秒。既定 2、0.1〜60 |

| 終了コード | 意味 | 出力（1 行） |
|---|---|---|
| 0 | 待ち終えた | stdout: `claude-waitfor: 一致するプロセスが無くなった（3分12秒）` など |
| 124 | 時間切れ（`timeout(1)` と同じ番号） | stderr: `claude-waitfor: 時間切れ（30分）: まだ動いている pid 4242: luajit tests/run.lua` |
| 2 | 引数の誤り（読めない正規表現・時間の形など） | stderr |

時間切れのあとに続けるかは、呼んだ側が決める（もう一度呼ぶ、上限を延ばす、相手を調べる）。

## 長く続く背景のシェルを見る

`claude-roster status` と `claude-kitty doctor` は、各セッション（claude の pid）の直下の子のシェル（`bash -c` など）のうち、
**2 時間以上**続いているものを 1 行ずつ出す（pid・経過時間・コマンドの先頭 80 字。Claude Code の包み
`source <snapshot> … && eval '…'` は外して中身を出す）。前面の Bash は 10 分で打ち切られるので、これだけ長いものは背景のもの。
どちらも見せるだけで、止めない（要るかどうかは担当か人が決める）。

```
$ claude-roster status
…
dev-a: 背景のシェル pid 2268182（22時間58分）: cd ~/x && until ! pgrep -f rail_v2.sh >/dev/null; do sleep 3; done; tail …
$ claude-kitty doctor
…
注意 背景のシェル: dev-a（claude pid 708093）: pid 2268182（22時間58分）: cd ~/x && until ! pgrep -f … → 要らない待ちなら kill 2268182（終わらない待ちは claude-waitfor に書き換える）
```

- `claude-roster status` は名簿の役割ごと（kitty のペインの claude の pid、無ければ名簿の session_id で生きているセッション）。
- `doctor` は `$CLAUDE_CONFIG_DIR`（無ければ `~/.claude`）と `~/.claude` の `sessions/` の生きているセッションごと。無ければ OK の 1 行、セッションが無ければ `--`。
- `claude-roster restart` が `status=shell` で断るときは、「背景のシェルが残っているかもしれない（claude-roster status で見る）」を添える。
- 経過時間は Linux は `/proc`、macOS は `proc_pidinfo` の起動時刻から（`lib/proc.py` の `age`。macOS で ctypes が使えないときは読まず、出さない）。
