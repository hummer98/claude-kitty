# claude-guard（状態で働く安全装置）

誰か（人・統括のセッション・スクリプト）が決めた「状態」の間だけ、Claude Code のツール呼び出しのうち、壊しうるものを止める。
`bin/claude-guard` は PreToolUse hook（Bash / Edit / Write / NotebookEdit）として動き、止めるときは理由の文を Claude に返す。Claude は「いまは後で」と分かって、別の作業に移るか、依頼主に知らせる。

- 判定はコマンド型の hook の中だけで行う（Python 3 の標準ライブラリ）。prompt / agent 型の hook（AI に判定させるもの）はトークンを使うので使わない。
- 誤って止めない方に倒す。読めない入力・設定の誤り・hook の中の例外では止めずに通し、`$XDG_RUNTIME_DIR/claude-guard/errors.log` にだけ残す。設定の誤りで保護が外れたことは `claude-guard status` の「問題:」の行で分かる（そのとき exit 1）。
- 止める候補が無いツール呼び出し（ほとんど）では、働く条件を調べない（pgrep も呼ばない）。許可があるときも調べない。
- 例外として、until / while のループの中の `pgrep -f`（待つシェル自身に一致して終わらない形）だけは、ガードが無くても常に止める（[下](#待つシェル自身に一致する-pgrep--f-のループ)）。
- claude-view の `view.guard`（前面にあったら画面を並べ替えない窓）とは別のもの。こちらはツール呼び出しを止める。

## ガード

設定は `~/.config/claude-kitty/config.json`（`$CLAUDE_KITTY_CONFIG` で変えられる）の `guard_hook.guards` に、名前ごとに書く。ガードは互いに独立で、働いているガードのどれか 1 つでも止めれば止める。

| 項目 | 中身 |
|---|---|
| `process` | 働く条件: `pgrep -f` に渡す正規表現（リスト可。どれか 1 つが動いていれば働く） |
| `command` | 働く条件: 判定のコマンド。文字列なら `sh -c`、リストならそのまま。終了コード 0 なら働く。`command_timeout` 秒（既定 2、30 まで）で打ち切り、時間切れは働かないと数える |
| （旗） | 働く条件: `claude-guard on` / `acquire` で立てた旗。どのガードにも常にある。期限が来たら自動で外れる |
| `paths` | 書き込みを止めるパス（絶対パスか `~` で始める）。その下も含む |
| `commands` | Bash で止めるコマンドの形（下の 3 つの書き方） |
| `allow_commands` | 例外として通す形（書き方は `commands` と同じ） |
| `message` | 理由の文の後に足す一文。`{name}`（ガードの名前）、`{reason}`（旗の理由）、`{condition}`（働いている条件）、`{what}`（止めた操作）を差し込める |
| `enabled` | `false` でこのガードを使わない |

働く条件は OR（旗・`process`・`command` のどれか 1 つで働く）。軽い順（旗 → pgrep → command）に調べ、1 つ当たればそこでやめる。

`commands` / `allow_commands` の書き方:

- 文字列: 語の並びの頭が一致（`"git push"` は `git push origin main` に当たる。最初の語はファイル名で比べる）。
- `{"regex": "..."}`: 単純なコマンド 1 つ（`&&` や `|` で区切った 1 つ。前の `VAR=値` と `env` / `timeout` などは外し、最初の語はファイル名にする）の文字列に `re.search`。
- `{"program": "<正規表現>", "value_options": ["--owner"], "block": ["deploy", {"sub": "config", "if_any": ["--set"]}, {"sub": "reset", "unless_any": ["--dry-run"]}]}`: プログラム名（ファイル名。`python3 x/svcctl.py` の形は `svcctl.py`）に当たり、最初のサブコマンドが `block` にあれば止める。`value_options` は値をとる全体のオプション（サブコマンドを見つけるために飛ばす）。`if_any` はそのフラグがあるときだけ、`unless_any` はそのフラグがあれば通す。`but_if_any` は `unless_any` の例外で、`unless_any` のフラグがあっても `but_if_any` のどれかがあれば止める（例: `{"sub": "road", "unless_any": ["--dry-run", "--inspect-node"], "but_if_any": ["--engine-check"]}` は `road --dry-run` と `road --inspect-node 5` を通し、`road --dry-run --engine-check` と `road --from 1,2 --to 3,4` を止める）。まとめると「`if_any` を満たし、かつ（`unless_any` のどれも無い、または `but_if_any` のどれかがある）なら止める」。フラグはサブコマンドより後ろの語だけを見る（`gamectl --engine-check road --dry-run` の `--engine-check` は見ない）。順番は問わず、`--flag=値` の形も同じに見る。止める側の `if_any` と `but_if_any` は、argparse の省略形（`--eng`・`--e` を `--engine-check` と読む）にも当てる（`--` で始まる語の `=` より前が、設定のフラグの頭と一致すれば当たり。長さの下限は無く、`--` だけは除く）。通す側の `unless_any` は完全一致のままで、省略形の `--dry` では通さない。短いオプション（`-x`）は完全一致。`allow_commands`（通す側）の `{"program"}` では省略形に当てない（例外として通すものを広げないため）。`but_if_any` を `unless_any` 無しで書くと設定の誤りとして知らせ、その項目は `but_if_any` を無視して（止める側で）使う。`allow_owner` を足すと、起動した担当だけを通す（[下](#起動した担当だけ通すallow_owner)）。

## 判定

### Edit / Write / NotebookEdit
`file_path`（NotebookEdit は `notebook_path`）が `paths` の下なら止める。相対パスは hook の入力の `cwd` から解く。

### Bash
コマンドを字句に分け（`shlex`。引用が閉じていなければ空白と演算子で粗く分ける）、`;` `&&` `||` `|` `&` `(` `)` 改行で単純なコマンドに分ける。

- **文字列はコマンドとみない**: 引用符の中（`echo "…"`、`printf '…'`、`git commit -m "…"` の値）と、ヒアドキュメントの本文（`cat >> README.md <<'EOF' … EOF`）に、止める対象の名前が書いてあるだけでは止めない。
- ただし bash が実際に動かすものは読む: `bash -c "…"` / `sh -c '…'` の中身、二重引用符の中と、区切りを引用しない `<<EOF` の本文の `$(...)` と `` `...` ``（置換はそのコマンドの cwd で読み、中の cd は外に持ち出さない）。区切りを引用した `<<'EOF'`・`<<"EOF"`・`<<\EOF` の本文と、一重引用符の中は何も読まない。
- 本文を stdin から動かすもの（`bash <<'EOF'`・`sh -s <<EOF` のような `-c` の無いシェルと `ssh host <<'EOF'`）は、本文を `bash -c` の中身と同じに読む。パイプで渡したもの（`echo … | bash`）とヒアストリング（`bash <<< …`）は読まない。
- 二重引用符の中の `<<` と、算術式（`$((1<<2))`・`(( … ))`）の `<<` はヒアドキュメントの始まりとみない（後ろの行を本文と取り違えて見逃さないため）。
注釈は語の頭の `#` だけ（`a#b` は注釈でない）。`2>/dev/null` の `2` は引数に数えない。バッククォートと `$(...)` の中も読む。`then` `do` `else` `{` `!` などの予約語は外して後ろのコマンドを見る（`for x in ...` の頭は見ない）。

- **cd を追う**: `cd X`（`pushd`）の後のコマンドは X で動くとみる（`cd <保護したパス> && git pull` を捕まえるため）。サブシェル `( ... )` の中の cd は外に持ち出さない。`~`、`$HOME`、同じコマンドの中で代入した変数（`D=...; cd "$D"`）は展開する。
- **コマンドの位置の変数**: 同じ Bash のコマンドの中で前に代入した変数（`T=…;`・`T="…";`・`export T=…`・`&&` や改行でつないだもの）が、コマンドの位置に語まるごとの `$T` / `${T}` で来たら、値を語に分けて（シェルの引用の規則で）読む。`T="python3 /x/svcctl.py --owner a"; $T deploy` は `svcctl.py deploy` として見る。前に付けたもの（`timeout 5 $T`・`X=1 $T`）や `python3 $T` の後ろでも同じ。引数の位置の `$T` は今までどおり値 1 語のまま置き換える。展開しないもの: コマンド置換で作った値（`T=$(…)`）、関数、このコマンドの中で代入していない変数（hook の環境の変数は値 1 語として置き換えるだけで分けない）、サブシェル `( … )` の中の代入、前の Bash の呼び出しで代入したもの。引用の有無は（語に分けた後では）読めない。`"$T"` は bash では分けずに 1 語で動く（`S="/opt/my tools/svcctl"; "$S" deploy` は動く）ので、コマンドの位置の変数は分けた形と分けない形の両方で読み、どちらかが止める形なら止める。分けない形のプログラム名は `svcctl.py --owner a` のようになるので、`program` を `^svcctl(\\.py)?$` のように `$` まで書けば当たらない。`$` で終わらない正規表現だと分けない形にも当たり、そこでは名乗りが読めないので、`$T` に `--owner X` を入れた形は `allow_owner` の例外にならない。
- **パスへの書き込み**（`paths` の下に当たるもの）:
  - git の `merge` `pull` `checkout` `switch` `restore` `reset` `commit` `rebase` `cherry-pick` `revert` `am` `apply` `clean` `mv` `rm` `stash`（`list` / `show` は除く）`bisect` `submodule`（`status` などは除く）`sparse-checkout` `checkout-index` `read-tree -u`。場所は cwd、`-C` / `--work-tree` / `--git-dir`、前に付けた `GIT_WORK_TREE=` / `GIT_DIR=`。`clone` / `init` / `worktree add` / `archive -o` / `format-patch` は行き先で見る。`status` `log` `diff` `show` `add` `branch` `tag` などは通す。
  - `mkdir` `rm` `rmdir` `mv` `touch` `unlink` `truncate` `shred` `chmod` `chown` `chgrp` `tee` `patch`: 引数のどれか。
  - `cp` `ln` `rsync` `install` `scp`: 行き先（最後の引数か `-t`。値をとるオプションの値は飛ばす。引数 1 つの `ln -s x` は cwd）だけ。保護したパスから外へ写すのは読むだけなので通す。
  - `sed -i` / `perl -i`、`dd of=`、`tar` の展開（`-C` か cwd）と作成（`-f`）、`unzip`（`-d` か cwd。`-l` `-t` などの読むだけの形は除く）、`curl -o` / `-O`、`wget -O` / `-P`、`find ... -delete` / `-exec`。
  - `>` `>>` `&>` などのリダイレクトの行き先（`2>&1` のような fd の付け替えは除く）。
- **コマンドの形**: `commands` に当たり、`allow_commands` に当たらないもの。`allow_commands` が外すのはコマンドの形の理由だけで、同じコマンドのパスへの書き込み（`git push --dry-run > <保護したパス>`）は止める。git は全体のオプションを外して比べる（`"git push"` は `git -C x push` にも当たる）。
- 前に付くもの（`VAR=値`、`env`、`nohup`、`timeout 30`、`sudo` など。値をとるオプションはラッパーごとに持つ）は外して見る。`bash -c` / `-lc` / `-ec` の中も読む。`--help` の付いたコマンドは止めない。
- パスの比べ方: 書かれたままを正規化した形と、シンボリックリンクを解いた形（`/home` → `/var/home` など）の両方で比べる。`files_backup` は `files` の下と数えない（名前の頭が同じだけの別のフォルダ）。

読むだけのもの（`cat`、`ls`、`grep`、`git status` / `log` / `diff`）は止めない。シェルの静的な読みなので、スクリプトの中で書き込むもの（`python3 tool.py` が中でファイルを書く）、alias、`eval` は見えない。「うっかり」を止める道具で、悪意のある回避は防がない。

## 待つシェル自身に一致する pgrep -f のループ

ガードの設定・保護するプロセスが動いているかに関係なく、**常に**止める判定（設定 `guard_hook.wait_loop_check: false` で切れる）。

Claude Code は Bash のコマンドを `bash -c '<コマンド>'` で動かす。`until ! pgrep -f "luajit tests/run.lua"; do sleep 2; done` と書くと、
`pgrep -f` が待つシェル自身のコマンド行（パターンの字を含む）に一致して、ループが終わらない（2026-11 に 15〜19 時間残り、
セッションの status が `shell` のままで `claude-roster restart` が断った。[waitfor.md](waitfor.md#なぜ)）。
この形を止め、理由に「待つシェル自身に一致して終わらない。`claude-waitfor proc '<パターン>'` を使う」と、頭の文字を囲んだ形（`pgrep -f '[l]uajit tests/run.lua'`）を返す。

止める形（字句で読む。ガードの判定と同じ字句の分け方）:

- `while` / `until` のループの中（条件でも本体でも。`done` までの間）の `pgrep` で、`-f`（`--full`、`-af` などの組も）があるもの。
- `$(...)`・`` `...` ``・`bash -c '…'` の中も、外のループの中にあれば数える。`bash -c` の中のループも読む。
- 同じコマンドの中で代入した変数（`P="luajit t"; until ! pgrep -f "$P"`）は値で見る（代入の字もシェルのコマンド行に残るので一致する）。

通す形（誤って止めない）:

- ループでない単発の `pgrep -f`（`pgrep -f x && echo running`）。`for` のループ、ループの後の `pgrep -f`。
- パターンの頭の文字を `[x]` で囲んだもの（`[l]uajit tests/run.lua`、`^[l]uajit`）。正規表現 `[l]uajit` はシェルのコマンド行の字 `[l]uajit` に一致しない。
- `^` の後に文字そのもの（英数字・`/`・`_`・`-`・`~`、`\.`、`.` の後の文字）が続くもの（`^\./TransportFever3`、`^luajit`）。シェルのコマンド行は `/bin/bash -c …` で始まるので一致しない。
- `-x`（`--exact`。全体の一致）、`-f` の無いもの（プロセスの名前だけを見る）。
- 値の分からない変数（`while read p; do pgrep -f "$p"; done`。シェルのコマンド行には `$p` の字しかない）。
- 引用符の中・ヒアドキュメントの本文（`echo "while pgrep -f x; do"`、`git commit -m '…'`）。

見逃すもの: `^/bin/bash` のようにシェルの頭そのものを書いたパターン、`eval`・関数・スクリプトの中のループ、`grep -q` で
ファイルを待つループ（`until grep -q … $F` は自分に一致しないが、目印が出なければ終わらない。`claude-waitfor file` を勧めるだけで止めない）。

- `pgrep` の字が無いコマンドでは、この判定のために設定も読まない。
- 設定の値が true / false でないときは有効のまま、`claude-guard status` の「問題:」に出す（そのとき exit 1）。`status` には「待つシェル自身に一致する pgrep -f のループ: 常に止める」の行が出る。
- `CLAUDE_GUARD_ALLOW`・許可のファイルでは外れない（ガードの名前ごとの通す道なので）。どうしてもループで書くなら `[x]` の形にする。
- ガードの止める理由と同時に当たれば、両方を並べて返す。

## 止め方

stdout に次の JSON を出して exit 0（PreToolUse の decision control。理由は Claude に返る）。

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": "[claude-guard] ガード「app-running」が働いている間（プロセス ^\\./fullscreen-app）は、この操作を止めています: …"}}
```

通すときは何も出さない。hook の中の例外・壊れた入力でも exit 0（exit 1 などは non-blocking error として通るが、transcript に hook error の表示が出るため）。

## 通す道

1. 環境変数 `CLAUDE_GUARD_ALLOW=<名前>`（カンマ区切り可）か `all`（`1` も all）。hook のプロセスの環境にあれば全部、Bash のコマンドの前に付ければそのコマンドだけ（`CLAUDE_GUARD_ALLOW=app-running svcctl deploy a`）、`export CLAUDE_GUARD_ALLOW=...` ならその後のコマンド。
2. 許可のファイル: `claude-guard allow <名前|all> --minutes 30 --reason "e2e"`。`$XDG_RUNTIME_DIR/claude-guard/<名前>.allow` に期限と理由を書く。期限が過ぎれば無効。`claude-guard revoke <名前|all>` で消す。
3. ロックの持ち主（下）。
4. 起動した担当（`commands` の `{"program"}` の項目の `allow_owner`。下）。その項目の形だけを通す。旗・ロックの間は使わない。
5. 共同作業の担当（起動の記録の `coop.with`。[下](#共同作業coop)）。4 と同じ照合・同じ範囲。

### 起動した担当だけ通す（allow_owner）

道具（例: ゲームを外から動かす `svcctl`）が「誰が起動したか」をファイルに記録するなら、起動した担当だけは、ガードが働いている間もその項目の形（`quit` など）を使える。ほかの担当は今までどおり止まる。

```json
{"program": "^svcctl(\\.py)?$", "value_options": ["--owner"], "block": ["quit", "restart"],
 "allow_owner": {"file": "$XDG_RUNTIME_DIR/svcctl/launched.json", "option": "--owner", "env": "SVCCTL_OWNER"}}
```

| 項目 | 中身 |
|---|---|
| `file` | 起動の記録。絶対パス・`~/…`・`$XDG_RUNTIME_DIR/<フォルダ>/…`（`${XDG_RUNTIME_DIR}` も可）。`XDG_RUNTIME_DIR` が無い（か空）なら `/tmp/<フォルダ>-<uid>/…`（記録を書く道具も同じ決まりにする） |
| `option` | コマンドが担当の名前を渡すオプション。サブコマンドより前の `--owner X` か `--owner=X`（最後のもの）。`value_options` に自動で足す |
| `env` | 担当の名前の環境変数。`option` が無いときに見る: 前に付けた `SVCCTL_OWNER=X`（`env SVCCTL_OWNER=X` も）、同じコマンドの中で `export` したもの、hook の環境にあるもの。`export` しない代入（`SVCCTL_OWNER=X; svcctl …`）は子に渡らないので見ない。`env -u` / `unset` で消したものも見ない |
| `same_role` | 既定 `true`: セッションの役割（kitty のペインの user var `role`）も X と同じことを求める。`false` で見ない |

`option` と `env` の少なくとも 1 つが要る。記録のファイルの形は `{"v": 1, "owner": "<担当>", "pid": <整数>, "started_at": <epoch>, "cmd": "launch"}`（`started_at` と `cmd` は見ない）。通すのは次が全部そろうときだけ:

1. コマンドが名乗る担当 X（`option` が先、無ければ `env`）が、記録の `owner` と同じ。
2. 記録が自分の普通のファイル（リンクでない）で、形が正しい（`v` が 1、`owner` が空でない文字列、`pid` が正の整数）。
3. その `pid` が動いていて、引数（`pgrep -f` と同じく空白でつないだもの）が、そのガードの `process` のどれかに当たる（pid の使い回しや、残った古い記録を効かせないため。macOS は `lib/proc.py` で読む）。
4. `same_role` なら、セッションの役割が X。役割は claude-state の記録（`$XDG_RUNTIME_DIR/claude-state/<session_id>.json` の `role`）から読む（hook を遅くしないよう kitty には聞かない）。**役割が分からないセッション（記録が無い・ペインに role が無い・kitty の外）は通さない**: 例外は止めを緩める側なので、確かでないときは今までの止める側に倒す。kitty の外で使うなら `"same_role": false`。

記録が無い・読めない・壊れている・どれかが合わないときは例外にならず、今までどおり止める。設定の誤り（`file` の形が違う、`option` / `env` が無いなど）は、その例外を使わずに「問題:」に出す。`allow_owner` は `process` のあるガードの `commands` の `{"program"}` の項目にだけ書ける。例外はその項目の `block` 全部に効く。起動した担当にも一部（`load` など）は止めたいなら、項目を 2 つに分け、`allow_owner` は通してよいサブコマンド（`quit` など）の項目にだけ書く。例外が外すのは項目の形の理由だけ（同じコマンドの保護したパスへの書き込みは止める）。**旗（`claude-guard on <名前>`）・ロック（`acquire`）が立っている間は例外を使わない**（旗は「全員を止める」ため。process も同時に動いていても止める）。例外が効くのは、旗もロックも無く、`process`（か `command`）で働いているときだけ。`claude-guard status` は旗の間「旗（ロック）の間は使わない」と出す。

### 共同作業（coop）

ユーザーが自分で動かしているゲームを、AI の担当と一緒に操作するとき（人が起動したので `owner` が担当でない）のための印。起動の記録に `coop` を足すと、`with` の担当も `allow_owner` の例外と同じ扱いで通す。

```json
{"v": 1, "owner": "aom-master", "pid": 4242, "started_at": 1795400000, "cmd": "launch",
 "coop": {"with": ["aom-probe", "aom-ui"], "reason": "一緒にダイヤを組む", "since": 1795400100, "until": 1795407300}}
```

- `with` は担当の名前 1 つか、そのリスト。`until`（epoch 秒）までが共同作業。`reason`・`since` は表示用（`since` は判定に使わない）。
- **期限が切れた印（`until` を過ぎた `coop` のキーが残っている記録）は「ユーザーがいる」扱い**（いちばん厳しい側）。期限が切れても人がまだゲームを操作しているかもしれないため。その間は `owner` の担当も `with` の担当も `allow_owner` の例外を使えず、項目の命令は止まる。断りの文には「共同作業の期限（HH:MM JST）が切れた。続けるならユーザーと決めてから coop start で延ばす（サブエージェントには打たせない）。終えるなら coop end」を添える（人と一緒に決めることなので、ここだけは続け方を書く。延ばすのはユーザーと決めてから）。`claude-show` も誰が呼んでも断る。
- **形の誤りの印も同じく「ユーザーがいる」扱い**: `coop` が dict でない、`with` が空・文字列でない、`until` が数でない・NaN・Infinity・0 以下・7 日より先（旗の上限と同じ）。誤った印で例外が開いたり、共同作業が終わらなくなったりしないように。断りの文は「共同作業の印の形が正しくない」。
- 印が無くなるのは、`coop end`（`coop` のキーが消える。`coopChanges` だけが残った記録は印でない）か、ゲームが終わったとき（記録の pid が今のゲームでない）。そのときは今までの判定（`owner` の担当だけが通る）に戻る。`"coop": null` も印なし。
- 照合は `allow_owner` と同じ（`file`・`option`・`env`・`same_role`）で、`owner` の代わりに `with` の担当と照らす: コマンドが名乗る担当 X が `with` にあり、`same_role` なら役割も X。記録の形と pid の照合（allow_owner の条件の 2・3）もそのまま要る（記録の pid が今のゲームでなければ共同作業ではない）。`owner` の担当は今までどおり通る。
- 人が `--owner` なしで起動したときは、`owner` の無い（か `null` の）記録に `coop` だけが付く。`coop`（`null` でない値。形の誤りの印も含む）があるときに限り、この形を読む（`owner` としては誰とも合わない）。`owner` も `coop` も無い記録は今までどおり形の誤り。
- **旗・ロックの間は使わない**（`allow_owner` と同じ）。**保護したパスへの書き込みと git は、共同作業でも止める**（例外が外すのは項目の形の理由だけ。ゲームが動いている間に MOD のフォルダを書くと落ちるため）。
- `coop.modified`（`true`）と `coop.changes`（正の整数）は、記録を書く道具が「プレイ用のセーブを担当が変えた回数」を残す欄。判定には使わず、`status` に添えるだけ（無い・形が違えば黙って省く）。
- 例外は項目の `block` 全部に効く（`quit`・`load` なども）。人のゲームで担当に使わせたくないサブコマンドがあれば、`allow_owner` と同じく項目を 2 つに分け、`allow_owner` は通してよい項目にだけ書く。
- 記録を書く道具は、一時ファイルに書いてから置き換える（読みかけの壊れた記録で例外を外さない）。

`claude-guard status` は、印が生きている間 `共同作業: aom-probe, aom-ui、16:15（JST）まで（一緒にダイヤを組む）、プレイ用のセーブを 3 回変えた` のように出す（時刻は JST で固定。旗の間は「（旗（ロック）の間は使わない）」を添える）。期限切れの印は `共同作業: 期限切れ（16:15）。ユーザーがいる扱いで、担当の命令は止める`、形の誤りの印は `共同作業: 印の形が正しくない。ユーザーがいる扱いで、担当の命令は止める` と出す。`claude-show` も同じ印を見る（[docs/config.md の playtest_guard](config.md#viewclaude-viewclaude-showclaude-roster)）。

理由の文には通す道を書かない（止められたサブエージェントが自分で外さないように）。外すのは依頼主か統括の判断。

## 旗とロック

- `claude-guard on <名前> [--minutes N] [--reason ...]`（既定 60 分、7 日まで）: 持ち主の無い旗。全員を止める。`off <名前>` で外す。どちらも、`acquire` のロックが生きていれば触らない（exit 1。ロックを外すのは `release`、どうしてもなら `off --force`）。
- `claude-guard acquire <名前> [--minutes N] [--reason ...] [--owner ID]`: 排他のロック。旗を持ち主つきで立てる。
  - 調べて作るまでを flock で 1 つにまとめ、作るのは `O_CREAT|O_EXCL`。同時に呼んでも取れるのは 1 つだけ。
  - 期限の切れた旗・壊れた旗は取り直す。
  - 持ち主は `--owner`、無ければ `CLAUDE_CODE_SESSION_ID`、それも無ければ `pid <呼んだシェルの pid>@<host>`。Claude Code の Bash の中では `CLAUDE_CODE_SESSION_ID` が hook の入力の `session_id` と同じ値になる。スクリプトや CI から使うときは、呼ぶたびにシェルが変わると pid の印も変わるので、`--owner` で決まった印を渡す。
  - 先に誰か（`on` の旗を含む）が持っていれば、立てずに exit 1 で、持ち主・期限・理由を stderr に出す。
- `claude-guard release <名前> [--owner ID] [--force]`: 持ち主だけが外せる（違えば exit 1）。`--force` で誰でも。
- **持ち主は止めない**: hook の入力の `session_id` が旗の持ち主と同じセッションは、そのガードでは止めない（ほかの条件で働いていても、そのガードは通す）。そのセッションのサブエージェントも同じ `session_id` で呼ばれるので通る。

## status / check

```
$ claude-guard status
app-running: 働いている（プロセス ^\./fullscreen-app）、条件 process/旗、止めるもの paths 1・commands 1、許可あり（残り 25 分、e2e）
deploy: 働いている（旗（残り 12 分、持ち主 5d0c…）、v1.4 を出す）、条件 旗、止めるもの paths 1・commands 2・例外 1
device-test: 働いていない、条件 command/旗、止めるもの paths 1・commands 0
game: 働いている（プロセス ^\./game）、条件 process/旗、止めるもの paths 1・commands 1、起動した担当の例外（/run/user/1000/svcctl/launched.json）: owner probe、pid 4242 が動いている → probe は通す（役割も同じとき）、共同作業: aom-ui、16:15（JST）まで（一緒に操作）
問題: guard_hook.guards.broken.process の正規表現が読めない: …
```

`claude-guard check '<command>' [--cwd DIR] [--session ID] [--assume-active]`、`claude-guard check --file PATH [--tool Edit]` で、hook と同じ判定を試せる（exit 0 = 通す、1 = 止める）。`--assume-active` は条件を調べず働いているものとみる。

## 例

### 例 1: 発表中のアプリが動いている間は、画面を動かす道具とその設定を止める

```json
{"guard_hook": {"guards": {"presenting": {
  "process": "^/usr/bin/slides-app --present",
  "paths": ["~/.config/window-layout"],
  "commands": ["claude-view", "claude-browser open", {"regex": "^kitten @ (focus|resize|goto)"}],
  "message": "発表が終わるまで画面を動かさないでください。"}}}}
```

### 例 2: 本番のデプロイを 1 つずつにする（ロック）

```json
{"guard_hook": {"guards": {"deploy-prod": {
  "paths": ["~/svc/deploy"],
  "commands": ["git push", {"regex": "^deploy\\.sh\\b"}],
  "allow_commands": ["git push --dry-run"],
  "message": "本番のデプロイ中です（{reason}）。終わるまで待ってください。"}}}}
```

```sh
claude-guard acquire deploy-prod --minutes 30 --reason "v1.4 を出す" || exit 1   # 取れなければ始めない
./deploy.sh && git push origin main                                            # 持ち主のセッションは通る
claude-guard release deploy-prod
```

ほかのセッションが同じ `./deploy.sh` や `git push` を実行しようとすると止まる。持ち主のセッションが落ちても、期限で外れる。

### 例 3: 装置のテスト中は、設定のフォルダを書き換えない（判定のコマンド）

```json
{"guard_hook": {"guards": {"bench-test": {
  "command": "test -e /run/bench/testing && systemctl --user is-active --quiet bench-runner",
  "command_timeout": 1,
  "paths": ["~/bench/config"]}}}}
```

## 限界

- 止めるのは Claude Code のツール呼び出し（Bash / Edit / Write / NotebookEdit）だけ。人の手、CI、cron、ほかの道具は止めない。ロックは、ほかの道具も `claude-guard acquire` を呼ぶときだけ排他になる。
- サブエージェントの中のツール呼び出しにも効く（設定ファイルの hook はサブエージェントの中でも同じように動き、入力に `agent_id` / `agent_type` が付く。公式の hooks の説明）。
- Bash はシェルの静的な読みなので、見えない書き込み（スクリプトの中、alias、`eval`）がある。コマンドの名前を変数に入れた形は、同じコマンドの中で代入したものだけ読む（上の「コマンドの位置の変数」）。
- `allow_owner` と共同作業の担当の名前は自己申告（`--owner`）と役割の照合で、なりすましは防がない（うっかり別の担当のものを止めることを防ぐ）。
- 速さ: 止める候補が無ければ Python の起動だけ（手元で約 0.03 秒）。候補があれば pgrep を 1 回（手元で 0.06 秒前後。pgrep 自体が 0.03 秒）。
- hook の `timeout` は、候補に当たるガードの `command_timeout` の合計と pgrep（打ち切り 5 秒）より長くする。超えると Claude Code が hook を打ち切り、そのときは止めずに通る。

## 入れ方

`~/.claude/settings.json` の `hooks` に足す（`timeout` は秒。条件のコマンドの打ち切りより長くする）。

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|NotebookEdit",
  "hooks": [{"type": "command", "command": "/path/to/claude-kitty/bin/claude-guard hook", "timeout": 10}]}]}}
```
