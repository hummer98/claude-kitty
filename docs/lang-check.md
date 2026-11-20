# claude-lang-check（返答の言語の確認）

Claude Code の返答が、ときどき決めた言語（日本語など）でなく英語になる。英語のシステムの促し（"The task tools haven't been used recently…" など）の後に起きやすい。
`bin/claude-lang-check` は Stop hook で最後の返答を調べ、決めた言語でなければ、書き直す理由を返して書き直させる。

- 判定はコマンド型の hook の中だけで行う（Python 3 の標準ライブラリ）。prompt / agent 型の hook（AI に判定させるもの）はトークンを使うので使わない。
- 誤って止めない方に倒す。読めないもの・短いもの・コードや表だけのものは通す。何が起きても exit 0。

## 判定

1. 本文: hook の入力の `last_assistant_message`。無ければ transcript（JSONL）の、最後の user の行（ツールの結果を含む）より後のアシスタントの `text` をつないだもの。
2. 次を除く: コードブロック（``` と ~~~。閉じていなければ最後まで）、インラインコード、URL、HTML のタグ、パス（`/a/b`、`~/a`、`./a`、`a/b.py`。ASCII の字だけ。日本語の「成功/失敗」はパスと数えない）、表の行（`|` で始まる行）、引用の行（`>` で始まる行）。
3. 数える: 対象の言語の字（日本語はひらがな・カタカナ・漢字）は 1 字 1。英語の文の語は 1 語 2 字分（日本語で同じことを書くとおよそ 2 字になるため。字の数のまま比べると、用語が混ざっただけで割合が下がりすぎる）。識別子らしい語（`_`・数字・`.`・`-` を含む、全部大文字、途中に大文字）は数えない。ほかの文字の字（対象が ko のときの日本語など）は 1 字 1 で対象外に数える。
4. 通す: 数えた合計が `min`（既定 20）字分未満。
5. 止める（どれか 1 つ）:
   - 冒頭の `head`（既定 200）字分で、対象の言語の割合が `threshold`（既定 0.3）未満。
   - 最初の段落（`min` 字分以上あるとき。見出しの行 `#` は飛ばす）の割合が `threshold` 未満。英語の前置き＋日本語の本文を捕まえる。
   - 対象の言語の字が出てくるまでに、対象外の語が `min` の 2 倍（既定 40 字分、英語の約 20 語）続く。前置きが段落を分けずに続く形を捕まえる。

試すときは `bin/claude-lang-check judge FILE`（または stdin）。`block: …` / `pass: …` を 1 行で出す（exit 0 = 通す、1 = 止める、2 = 読めない）。

通すときは transcript を読まない（`last_assistant_message` があるとき）。設定の問題と hook の中の例外は `$XDG_RUNTIME_DIR/claude-lang-check/errors.log` にだけ残す（stdout・stderr には出さない）。

## 無限に止めない

- 入力の `stop_hook_active` が真なら必ず通す（hook で続けている最中）。
- 止めるのは 1 回の返答（ユーザーの 1 回の入力に対する応答）につき 1 回まで。返答の鍵は、transcript の最後のユーザーの入力（ツールの結果と、この hook の理由の目印 `[claude-lang-check]` を含む行は除く）の `uuid`。止めたら `$XDG_RUNTIME_DIR/claude-lang-check/<session_id>.json` に鍵を残し、同じ鍵では 2 回目を止めない。
- transcript を読めない・ユーザーの入力が見つからない・記録を書けないときは、1 回きりを守れないので通す。
- Claude Code 自身にも、続けて 8 回までの上限がある（`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`）。
- `hook_event_name` が `Stop` 以外（`SubagentStop` など）なら何もしない。

## 止め方

stdout に次の JSON を出して exit 0（Stop の decision control）。

```json
{"decision": "block", "reason": "[claude-lang-check] 直前の返答の本文が日本語になっていません。同じ内容を日本語で書き直して…"}
```

理由には「コード・識別子・引用はそのまま」「ユーザーが別の言語を求めていた場合はそのまま」を含める。

## 導入

`~/.claude/settings.json` の `hooks.Stop` に足す（ほかの Stop hook と同じ配列に並べてよい。Stop は matcher を使わない）。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {"type": "command", "command": "/path/to/claude-kitty/bin/claude-lang-check", "timeout": 10}
        ]
      }
    ]
  }
}
```

英語の出力を前提にした自動の実行（`claude -p` など）でも 1 回止まって日本語に書き直されるので、そのときは `CLAUDE_LANG_CHECK=off` を付けて起動する。

## 設定

`~/.config/claude-kitty/config.json`（`$CLAUDE_KITTY_CONFIG` か `$XDG_CONFIG_HOME` で場所を変えられる）の `lang`。
**`language` を決めるまでは何もしない**（既定は無し。hook を入れただけで、ほかの言語を使う人の返答を書き直させないため）。ほかの項目は省ける。`tools/install.sh` は `--hooks=lang` と明示したときだけ、この hook を入れる。

```json
{"lang": {"enabled": true, "language": "ja", "threshold": 0.3, "head": 200, "min": 20}}
```

- `language`: `ja`（ひらがな・カタカナ・漢字）、`zh`（漢字）、`ko`（ハングル）。
- 環境変数が設定ファイルより優先する: `CLAUDE_LANG_CHECK=off`（止める）、`CLAUDE_LANG_CHECK_LANGUAGE`、`CLAUDE_LANG_CHECK_THRESHOLD`、`CLAUDE_LANG_CHECK_HEAD`、`CLAUDE_LANG_CHECK_MIN`。
- 正しくない値はその項目だけ既定値に戻す（hook を止めないため）。

## 単体テスト

`python3 tests/lang_check_test.py`（標準ライブラリだけ）。

## 閾値の目安

過去の会話記録（約 2,800 件の返答）で試したとき、英語の返答の冒頭の割合は 0〜0.29（0.2 を超えたのは英語の促しで始まり日本語が続くものだけで、それらは最初の段落の規則でも 0 になる）、日本語の返答は 0.59 以上だった。0.3 はその間で、日本語の側に余裕を多くとった値。
