# claude-roster（名簿と復元）

kitty の中で動く複数の Claude Code セッションについて、名簿（役割・セッション ID・フォルダ・モデル・kitty の配置）を保ち、
kitty を起動し直したり落ちたりしても、1 つのコマンドで元の配置・フォルダ・モデル・会話に戻す。1 人だけ起動し直すこともできる。

```sh
claude-roster save                 # いまの名簿を保存して表示する（ふつうはタイマーが 5 分ごとに --auto で行う）
claude-roster status               # 名簿といまの差（いない役割・ID が変わった役割・別のフォルダにいる役割）
claude-roster restore              # 名簿の役割のうち、いないものを開いて並べ直す（kitty が無ければ kitty ごと）
claude-roster restore dev-a        # その役割だけ開く
claude-roster restart dev-a --model sonnet   # 1 人を、同じペインで開き直す（idle のときだけ）
claude-roster sleep dev-a          # 1 人を寝かせる（claude を止めてペインを閉じ、名簿に残す。idle のときだけ）
claude-roster wake dev-a           # 寝かせた役割を開き直し、同じ会話を resume する
claude-roster show [--from FILE]   # 名簿の中身
```

共通: `--to SOCKET`（対象の kitty。省くと `view.left` の役割がいる kitty、無ければ名簿の役割がいちばん多い kitty。
`claude-kitty` のクラス（`kitty-claude`、`$CLAUDE_KITTY_CLASS`）の窓を、試験用の kitty より優先する）。

## 名簿

`$CLAUDE_ROSTER_DIR`（既定 `$XDG_DATA_HOME/claude-kitty`、無ければ `~/.local/share/claude-kitty`）の `roster.json`（本人だけ読める）。
意味のある変化（役割・ID・フォルダ・モデル・配置）があったときだけ、前のものを `roster.json.1` … へ回す（既定で 5 世代）。
ステータスや pid だけの変化では回さない。

集めるもの:

- `kitten @ ls`: ペインの user var `role`・window id・タブ・分割の木。統括（`view.left`）のいるタブは、左の列を除いた右側を
  段（上から）× 横（左から）で記録する（`claude-view rows` の形。`lib/view.py` の `current_rows`）。ほかのタブは並び順。
  role の無いペイン、overlay（`claude-show` の画像など）、名前に空白・`,`・`/`・引用符を含む role、設定の `roster.ignore` は入れない。
- `~/.claude/sessions/<pid>.json`: ペインの前面のプロセスの pid と結び付けて、sessionId・cwd・name・kind・status を読む
  （`/clear` で ID が変わっても pid で追える）。JSON の無い claude（背景セッションの前面など）はコマンド行の `--resume` の ID で探す。
  claude-wait が待っているだけのペインは、claude-wait の引数の ID を記録する（state = waiting）。
- モデル（どれで決めたかを `model_source` に残す）:
  1. 設定の `roster.models`（役割 → モデル）: `config`
  2. 起動したときの `--model`: `argv`（開いた人が選んだもので、フォルダの既定より強い。claude 自身もこの順で効かせる）
  3. そのフォルダの `.claude/settings.local.json`、`.claude/settings.json` の `model`: `settings`
  4. 会話記録（`~/.claude/projects/*/<ID>.jsonl`）の最後の本体の assistant の `model`: `transcript`

前の名簿からの引き継ぎ:

- ペインはあるが claude がいない（/exit してシェルに戻った）役割は、前の ID・モデルを残す（`carried`）。
- ペインごと無くなった役割は、自動の保存（`--auto`）では `absent_since` を付けて 24 時間（`roster.keep_absent_hours`）残す。
  誤ってペインを閉じても `claude-roster restore <役割>` で戻せる。手で `save` したときは、いまの kitty が正として消す。
- 寝かせた役割（`sleeping`。[sleep](#sleep--wake)）は、自動の保存でも手の保存でも、`keep_absent_hours` を過ぎても消さない。
  段組みには入れず、寝かせる前のタブ（統括のタブにいたなら standby）に載せる。1 人だけのタブにいて、そのタブがペインと一緒に
  kitty から無くなっても、名簿にはその題のタブを作って載せる（wake でそのタブに開く）。その役割のペインが kitty に戻っていれば
  （手で開いたなど）、いまの kitty が正として印は外れる。その ID をほかの役割がいま開いていれば、寝ている記録は残さない
  （同じ ID を 2 人に持たせない。会話はそちらにある）。
- kitty が無いとき・役割のあるペインが 1 つも無いとき・（自動の保存で）統括のペインが無いときは保存しない
  （落ちた直後のタイマーで名簿を失わない。統括と段組みを名簿から落とさない）。
- 同じセッション ID を 2 人が持つ（claude-wait で別のペインへ移している途中など）ときは、claude が動いている役割を正とし、
  待っているだけの役割の ID は消して `dup_of` を付ける。読めない名簿は `roster.json.bad` へ退けて、保存を続ける。
- タブの題に claude-state の「● 」が付いていても外して記録する。

## 自動の保存

systemd のユーザータイマー（5 分ごと）。Claude Code の `settings.json`（hook）は変えない。

```sh
cp contrib/systemd/claude-roster-save.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now claude-roster-save.timer
```

`restore` の間はロックを持つので、途中の半端な名簿を書かない（そのときの自動の保存は 1 回休む）。

## restore

- 保護する窓（`view.guard`。全画面のアプリなど）が前面にあるときは何もしない（新しい kitty の窓がその上に出ないように。`--force` で実行）。
- **二重に開かない**: 名簿の ID が、どこかで動いている claude（`~/.claude/sessions` の生きているプロセス、または `/proc` のコマンド行の
  `claude --resume <ID>`）か、待っている claude-wait にあれば、その役割は開かない（理由を出す）。claude-wait のロックは試しに取らない
  （ちょうど起動した claude-wait を追い返してしまうため）。開いたペインも claude-wait を通すので、相手が終わるまで待つ。
- **kitty が無いとき**: 保護するプロセスが動いているだけでも起動しない（新しい窓は最大化して出るので。`--force` で実行）。名簿からセッションファイル（`$XDG_RUNTIME_DIR/claude-roster/restore-<時刻>.session`）を作り、
  `claude-kitty <ファイル>` で起動する。各ペインは `--var role=<役割> --cwd <フォルダ>` で、
  `bash -lic 'claude-wait <役割> <ID> --model <モデル>'` を開く（ID が無い役割はシェルだけ）。統括のタブは段組みどおりに割り、
  最後に `claude-view rows "<段組み>"` で元の並びと等分割にそろえる。設定の `view.pinned`（常時表示）の役割は、名簿で別のタブにいても
  統括のタブの 1 段目の先頭に開く。`--dry-run` はファイルを表示するだけ。
- **kitty が動いていて一部だけ欠けているとき**: `kitten @ launch --keep-focus` で欠けた役割だけ足し（統括のタブの役割は standby タブへ、
  ほかは名簿のタブへ。`view.pinned` の役割は、並べ直しが走るとき（統括とそのタブがあるとき）だけ standby タブへ）、統括のタブの役割を足したときだけ `claude-view rows` で並べ直す。足したのが pinned だけなら、保存した段組みには戻さず `claude-view equalize`（いまの並びのまま、pinned を 1 段目の先頭に置く）。新しいペインの環境は kitty のもの
  （呼んだ claude の `CLAUDE_*` は入らない）。
- ペインはあるが claude がいない（シェルに戻った）役割は開かない（ペインを作らないので）。そのペインで `claude-wait <役割> <ID>` を打つ。
- kitty が落ちた直後は、claude の後片付けが終わらず「いま動いている」と見えることがある。数秒後にもう一度 `restore` する。
- 保存の前からペインが無かった役割（`absent_since`）は、`--include-absent` か役割を名指ししたときだけ開く。
- 寝かせた役割（`sleeping`）は、`--include-sleeping` か役割を名指ししたときだけ開く（`--include-absent` では開かない）。
  名指しで開いたときは、寝ている印は次の保存で外れる（印をすぐ外し、段組みの元の位置へ戻すのは `wake`）。
- 名前が役割と違うセッションには `--rename` を付ける（resume では `-n` が効かないので、claude-wait が `/rename <役割>` の案内を出す）。

## restart

1 人を同じペインで開き直す。send-text と send-key は使わず、SIGTERM と claude-wait の「続きの指示」で行う。

1. 設定の `view.guard` のプロセス（全画面のアプリなど）が動いていれば、前面かどうかにかかわらず止めない（exit 2）。
2. そのペインの claude が idle であること: `~/.claude/sessions` の `status` が `idle`、かつ `claude-state get` が空・idle・done（exit 3）。
3. その claude が、続きの指示に対応した claude-wait（`$XDG_RUNTIME_DIR/claude-wait/<ID>.waiter` の pid と、claude の親の pid が一致）から
   開かれていること。そうでなければ止めない（止めてもシェルに戻るだけなので）。
4. `$XDG_RUNTIME_DIR/claude-wait/<ID>.next` に cwd・モデル・claude-wait の pid・時刻を書き、止める直前にもう一度 idle と保護するプロセスを確認してから
   SIGTERM を送る。モデルは `--model` > 名簿、フォルダは `--cwd` > 名簿。
5. claude-wait は claude が終わったあと、自分宛て（pid が一致）で 5 分以内の `.next` があれば消して読み、シェルに戻らずに、
   その cwd とモデルで、指示の中の ID（いまの会話。/clear のあとは claude-wait の引数の ID と違う）を resume し直す
   （前のプロセスが確実に終わるのを待ってから）。`.waiter` と `.next` の名前は claude-wait の引数の ID。
6. 止まらなければ（既定 30 秒）、続きの指示を消す（あとで人が /exit したときに勝手に開き直さない）。

フォルダとモデルの既定: `--cwd` > 名簿（名簿の ID がいまの ID と同じときだけ）> いまのフォルダ。`--model` > 名簿（同じ条件）>
いまの claude の `--model` > claude-wait の既定（いまのモデルのまま）。名簿のモデルは `roster.models` が先なので、`--model` で替えても、
次の保存の記録は `roster.models` の値になる。

背景セッション（kind=bg）は、`--allow-bg` を付けたときだけ止める。前面ではなく JSON の pid（daemon の中の、そのセッションの host）を止める。同じ pid を他のセッションも
持っていれば（daemon 本体かもしれない）止めない。host が止まっても前面が残っていれば、前面も止める（claude-wait が続きを開く）。

## sleep / wake

役目を終えた担当のセッションを止めてペインを閉じ（実行レーンを減らす）、あとで会話ごと戻す。

`claude-roster sleep <役割>`:

1. 統括（`view.left`）と常時表示（`view.pinned`）の役割は寝かせない（exit 3）。保護するプロセス（`view.guard`）が動いていれば止めない（exit 2）。
2. そのペインの claude が idle であること（restart と同じ: `~/.claude/sessions` の `status` が `idle`、`claude-state get` が空・idle・done。
   exit 3）。`--force` で idle の確認を省く。claude がいない（シェルや待っているだけの）ペインは断る。
3. その claude を開いた claude-wait が続きの指示に対応していれば、`<ID>.next` に「終わる」の指示（`"action": "exit"`。
   `session_id` は null）を書く。claude-wait は claude が終わったあと、シェルに戻らずに終わる（ペインが閉じる）。
   前の版の claude-wait はこの指示を ID の形で断ってシェルに戻るだけで、同じ会話を開き直すことは無い。
4. 止める直前にもう一度 idle と保護するプロセスを確かめ、claude に SIGTERM を送る（restart と同じ。send-text の /exit は使わない。
   打ち込む先を取り違えないように）。背景セッション（kind=bg）は `--allow-bg` を付けたときだけ。host を止めたあとに前面が残っていれば、
   止める前に覚えた起動時刻と同じプロセスのとき（claude-wait がいればその子のときも）だけ前面も止める。
5. `--timeout`（既定 30 秒）で止まらなければ、続きの指示を消し、ペインは閉じず名簿も変えない（exit 1）。
6. 閉じる前に、そのセッションの claude がどこにもいないこと（`~/.claude/sessions`）と、ペインの前面にシェル（bash・zsh など）しか
   残っていないことを確かめる。シェルはオプションだけのもの（`bash -l`・`bash --login`）と、restore が開く形の親
   （`bash -lic 'claude-wait …'`。claude-wait の呼び出し 1 つだけで、`;`・`&&`・`|`・置換・改行で続きが無いもの）に限る。
   スクリプトを動かしているシェル（`bash run-claude.sh`）、claude-wait、別のコマンド（エディタなど）が残っていれば閉じない。
   前面のプロセスが読めない・空のときも閉じない（中が全部終わったペインは kitty が自分で閉じる）。`kitten @ ls` が失敗し続けて
   確かめられないときも閉じない（どれも exit 1。名簿も変えない）。ペインが自分で閉じていなければ `kitten @ close-window --match id:<window>` で閉じる。
   見るのは前面のプロセスだけなので、そのシェルの背景のジョブ（`npm run dev &` など）は閉じると止まる。素のシェルから claude を開いた役割は
   先に確かめる。閉じたのにペインが残ったときは、名簿を変えない（残ったペインで次の保存が印を外すので）。
7. 名簿のその役割に `sleeping: true`・`slept_at`（時刻）・`sleep_place`（寝かせる前のタブ、段組みの段・列・1 人だけの段だったか）を書く。
   セッション ID（/clear のあとならいまの ID）・cwd・model・name・slot は残す。段組みからは外す。統括のタブにいたなら
   `claude-view equalize` で並べ直す。

ペインがもう無い役割（`absent_since`）は、止めるものが無いので名簿に印を付けるだけ（その ID がどこかで動いていれば断る）。
統括のペインが kitty に無いときは寝かせない（自動の保存と同じく、統括と段組みを名簿から落とさないため。exit 3）。
`--dry-run` は止めずに、何をするかだけを出す。寝かせている間はロックを持つ（自動の保存は 1 回休む）。

`claude-roster wake <役割>`: 寝かせた役割を `restore <役割>` と同じ決まりで開く（claude-wait で同じ会話を resume。統括のタブの役割は
standby に足して `claude-view rows` で前に出す、kitty が無ければ kitty ごと）。そのとき段組みは `sleep_place` の位置に戻す
（段組みが変わっていれば、範囲に収めて入れる）。開けたら寝ている印を外す。その ID がどこかで動いていれば開かない（exit 1）。
`--dry-run` は開かず、印も外さない。

`claude-roster status` は「寝ている（2026-11-23 10:00 から）」、`show` も同じ印を出す。`claude-state` の色の見張りと
`claude-kitty doctor` は名簿の役割を見ないので、寝ている役割を「いない」とは言わない。

## 設定

`~/.config/claude-kitty/config.json`（どれも省ける）:

```json
{
  "roster": {
    "models": {"dev-a": "opus"},
    "ignore": ["scratch"],
    "generations": 5,
    "keep_absent_hours": 24
  }
}
```

環境変数: `CLAUDE_ROSTER_DIR`（名簿の場所）、`CLAUDE_ROSTER_WAIT_CMD`（ペインで動かす claude-wait。既定はこのリポジトリの `bin/claude-wait`）、
`CLAUDE_ROSTER_LAUNCHER`（kitty を起動するコマンド。既定は `bin/claude-kitty`）、`CLAUDE_SESSIONS_DIR`・`CLAUDE_PROJECTS_DIR`（試験用）。
`claude-kitty` は `CLAUDE_KITTY_CLASS`（窓のクラス。`kitty-claude` 以外のときは KWin で動かさない）と `CLAUDE_KITTY_START_AS` で、
試験用の kitty を別に起動できる。claude-wait は `CLAUDE_WAIT_CLAUDE`（試験用の偽の claude）。

## 試験

`python3 tests/roster_test.py`（kitty も claude も使わない。偽の kitten と偽の claude で、restart の停止から開き直しまでを通す）。

kitty を使った確認は、本物の kitty に触れないよう、見えない画面（`cage` の headless backend）に別の `XDG_RUNTIME_DIR`・別のクラスの
kitty を起動し、ダミーの名簿と偽の claude で行う:

```sh
S=$(mktemp -d); mkdir -m 700 $S/rt
XDG_RUNTIME_DIR=$S/rt WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 setsid cage -- sleep 900 &
# kitty.conf は allow_remote_control socket-only / listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid} / enabled_layouts splits
env -u DISPLAY XDG_RUNTIME_DIR=$S/rt WAYLAND_DISPLAY=wayland-0 KITTY_CONFIG_DIRECTORY=$S/kconf \
    CLAUDE_KITTY_CLASS=kitty-rostertest CLAUDE_ROSTER_DIR=$S/data CLAUDE_ROSTER_WAIT_CMD=$S/bin/test-wait \
    claude-roster restore
```

## 既知の制限

- 動いている claude-wait は、更新前のものだと続きの指示に対応していない。restart はそれを見分けて断る（一度 `restore` か
  `claude-wait` で開き直せば使える）。
- 背景セッションの restart は、実物の背景セッションでは確認していない（単体テストだけ）ので、`--allow-bg` が要る。
- idle の判定は Claude Code の status と claude-state で、背景の shell・monitor のタスク（claude-state は数えない）は見ない。
  背景で長いコマンドや Monitor を回している役割は、止める前に確認する。
- kitty を `claude-roster restore` 以外の方法（ふつうのセッションファイルで新しい claude）で起動し直すと、5 分後の自動の保存で
  新しい ID が名簿に入る。古い ID は世代（`roster.json.1` …）にだけ残る。起動し直すときは `claude-roster restore` を使う。
- 別のフォルダ（`--cwd`）で resume したとき、Claude Code がその ID の会話記録を見つけられるかは、版によって違う見込み。
- 統括のセッションが別の場所で動いていて開かなかったときは、並べ直さない（`claude-view` は統括のいる kitty を前提にする）。
- sleep の close-window と、claude-wait が終わってペインが自分で閉じる流れは、偽の kitten でだけ確かめている（本物の kitty ではまだ）。
