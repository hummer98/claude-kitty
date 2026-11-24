# claude-prompt-suffix（文の最後の略号）

指示の文の最後に短い略号を付けて、返答の形を決める（`このやり方どう思う？ wdt` = 意見だけ、`desc 2` = 2 番を解説）。
CLAUDE.md に略号の意味を書いてモデルに任せるだけだと、効いたり効かなかったりする。
`bin/claude-prompt-suffix` は UserPromptSubmit hook で文の最後の略号を拾い、その意味をモデルに渡す（`additionalContext`）。
画面には 1 行「略号 wdt を適用」と出る（`systemMessage`）。

- 判定はコマンド型の hook の中だけで行う（Python 3 の標準ライブラリ。AI は使わない）。
- 略号が無ければ何も足さない（トークンを毎ターン増やさない）。何が起きても exit 0 で、入力は止めない。
- 入れるかは任意。`tools/install.sh --hooks=suffix` と明示したときだけ入る（`--all` には入らない。`claude-statusline` と同じ）。

## 既定の略号

| 略号 | 意味（モデルに渡す文の要約） |
|---|---|
| `wdt` | 意見だけを聞いている。作業の指示ではない。ファイルの変更・コミット・他のセッションやサブエージェントへの変更の依頼はしない（読むだけの調べものはよい） |
| `opt` | 案を 2〜4 つ、番号を振って出し、それぞれに推奨度と信頼度を付け、おすすめを 1 つ選ぶ。選ばれるまでは実装しない |
| `pre` | 生成物を見せる（HTML なら HTML、画像なら画像を開く。kitty 上なら `claude-show <file>`。デプロイしていればその URL） |
| `desc` | 解説だけ。作業はしない（読むだけの調べものはよい）。直前の話題を解説する |
| `desc <番号>` | 直前の返答のその番号の項目を解説する（`desc 2`、`desc 2,3`、`desc 1-3`）。番号の付いた項目が無い返答なら、どこを指すか確認してから |
| `next` | ユーザーが決めることだけを、1 行目にその数を書き、番号を振って並べる。自分で決められるものは決めておく |
| `ss` | 最新のスクリーンショットを開いて見てから答える。hook が最新の画像のパスと時刻を添える（下の「スクリーンショット」） |

- 並べると全部効く: `どれがいい？ opt wdt`（案を出す + 意見だけ）。4 つまで。同じものは 1 回に数える。
- 大文字小文字は区別しない（`WDT` も同じ）。
- 使える略号と文は `claude-prompt-suffix list`、ある文で拾うかは `echo 'どう思う？ wdt' | claude-prompt-suffix check`（拾えば exit 0 と足す文、拾わなければ 1）。

## 混同を防ぐ決まり

拾うのは、次をすべて満たすときだけ。

1. **貼り付けた文は除く。** Claude Code は、まとめて貼り付けた文（入力欄で `[Pasted text #1 +N lines]` に畳まれるもの）を hook の `prompt` の中で `<pasted_content id="1a2b">` … `</pasted_content id="1a2b">` で包む（Claude Code 2.1.331 で確認）。この範囲を除いてから見る。貼り付けた文の最後が `next` でも拾わない。
   - 畳まれない短い貼り付け（数行まで）は、打った文と見分けがつかない。
2. **最後の語が単独のとき。** 最後の行の最後の語で、前が空白（全角の空白も）か改行、後ろに何も無い（句読点も付かない）。`これでいい？wdt`（前に空白が無い）、`wdt。`・`next.`（句読点付き）、`--pretty=desc`（語の一部）は拾わない。英語の文の終わりの `what comes next.` を拾わないよう、句読点は許さない。
3. **最後の行がコード・コマンド・引用なら見ない。** 閉じていないコードブロック（```` ``` ```` か `~~~`）の中、`$ ` か `% ` で始まる行（シェルの入力）、`--` を含む行（オプション付きのコマンド）、`>` で始まる行（引用）。文全体が `/` か `!` で始まるとき（スラッシュコマンド・シェルのモード）も見ない。
4. **拾ったら画面に 1 行。** `略号 desc 2 を適用`（`notice: false` で出さない）。思っていないときに拾ったら気づけるように。
5. **足す文は断り付き。** 「ユーザーの文の最後の「X」を略号とみなした。明らかに文の一部なら無視してよい。」の後に、略号ごとの意味を 1 行ずつ。

サブエージェントへの指示（入力に `agent_id` があるもの。親のモデルが書いた文）は対象外。

足す文の例（`どれにする？ opt wdt`）:

```
[claude-prompt-suffix] ユーザーの文の最後の「opt wdt」を略号とみなした。明らかに文の一部なら無視してよい。
- opt: 案を 2〜4 つ出し、番号を振って、それぞれに推奨度と信頼度を付け、おすすめを 1 つ選ぶ。選ばれるまでは実装しない。
- wdt: 意見だけを聞いている。作業の指示ではない。…
```

## 一覧の番号（desc で指すため）

`desc 2` で指せるのは、直前の返答の一覧に番号が振ってあるときだけ。`opt` と `next` の文には「番号を振る」を入れてある。
それ以外の返答にも番号を振らせるには、次のどちらか。

- **CLAUDE.md に 1 行書く（勧める）**: 「案・判断・指摘の一覧には番号を振る」。毎ターン読まれるが、キャッシュに乗るので増える分はほぼ無い。
- **設定 `number_lists: true`**: 略号の無い文にも毎回この 1 行を足す（1 ターンに 40 トークンほど増える）。既定は `false`。

## スクリーンショット（ss）

`ss` の文の `{screenshot}` を、置き場の直下の画像（png・jpg・webp など）のうち一番新しいもののパスと時刻（`最新: /home/me/Pictures/Screenshots/a.png（11-24 10:31、3 分前）。`）に置き換える。見つからなければ「見つからなかった（探した場所: …）。ユーザーに置き場を確認する」。

置き場は設定 `screenshot_dir`（文字列か、そのリスト。`~` を使える）。`null`（既定）なら OS の普通の場所を順に見て、一番新しいものを選ぶ。

| OS | 見る場所 |
|---|---|
| Linux | `<ピクチャ>/Screenshots`（GNOME・KDE の Spectacle）、`<ピクチャ>/スクリーンショット`（日本語の GNOME）、`<ピクチャ>`（古い Spectacle）、`~/Pictures/Screenshots`、`~/Pictures`。`<ピクチャ>` は `~/.config/user-dirs.dirs` の `XDG_PICTURES_DIR`（日本語の環境では `~/画像` など） |
| macOS | 「スクリーンショット」の保存先の設定（`defaults read com.apple.screencapture location`）と `~/Desktop` |

`<ピクチャ>` の直下も見るので、スクリーンショットでない画像が一番新しければそれを選ぶ。違う画像を見てほしくなければ `screenshot_dir` に置き場だけを書く。

## 設定

`~/.config/claude-kitty/config.json` の `prompt_suffix`（[config.md](config.md#prompt_suffixclaude-prompt-suffix-文の最後の略号)）。

```json
{"prompt_suffix": {
  "language": "ja",
  "suffixes": {
    "wdt": "意見だけ。作業はしない。",
    "tldr": "3 行で要約する。",
    "pick": {"text": "おすすめを 1 つだけ選ぶ。", "with_number": "直前の返答の {n} 番を選んだものとして進める。"},
    "pre": null
  },
  "screenshot_dir": "~/Pictures/Screenshots"
}}
```

- `suffixes` は既定に重ねる。文字列で足す・置き換える、`null` で既定のものを消す。番号を取る略号は `{"text": 番号なしの文, "with_number": 番号ありの文}`（`{n}` が番号に置き換わる）。`{screenshot}` はどの略号の文にも書ける。
- 名前は英字で始まる 20 字までの英数字（`_`・`-` も可）。大文字で書いても小文字で比べる。
- `language`: 既定の略号と断りの文の言語（`ja` か `en`）。
- 正しくない項目は、その項目だけ既定に戻す。正しくない略号は使わない（既定にあるものも）。問題は `$XDG_RUNTIME_DIR/claude-prompt-suffix/errors.log` に残り、`claude-kitty doctor` の「設定: prompt_suffix」が NG になる。
- 環境変数 `CLAUDE_PROMPT_SUFFIX=off` で止める。

## 導入

`tools/install.sh --hooks=suffix`（ほかの hook と並べてよい: `--hooks=state,guard,suffix`）。手で入れるなら `~/.claude/settings.json` の `hooks.UserPromptSubmit` に足す（UserPromptSubmit は matcher を使わない）。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "/path/to/claude-kitty/bin/claude-prompt-suffix", "timeout": 10}]}
    ]
  }
}
```

外すのは `tools/install.sh --uninstall --hooks=suffix`。CLAUDE.md に同じ略号の説明を書いているなら、hook を入れたあと消してよい（両方あっても食い違わなければ害は無い）。

## 単体テスト

`python3 tests/prompt_suffix_test.py`（`python3 tests/run.py` にも入っている）。
