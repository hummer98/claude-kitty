# claude-active（人が起きているか）

どれかの役割（統括を含む）のセッションで、**人が打った**最後の発話からの分数と、どの役割だったかを 1 行で返す。読むだけ。
ハマった作業を人に上げてよいか（人が起きているか）を、統括や担当が決めるためのもの。

```
$ claude-active
active 3分前 aom-master（07:13 JST）        # 終了コード 0（--minutes 分以内。既定 20）
$ claude-active
away 47分前 aom-probe（06:29 JST）          # 終了コード 1
$ claude-active
unknown 人の発話が見つからない（見たセッション 11）   # 終了コード 2
$ claude-active --minutes 30 --json
{"status": "active", "code": 0, "minutes": 3.2, "role": "aom-master", "session_id": "…", "at_jst": "…+09:00", "seen": 11, …}
```

- 見分けに自信が無いとき（役割のある動いているセッションが無い・記録が無い・読めない・人の発話が見つからない）は `unknown`（2）。**起きているとは答えない**。2 は 1 と同じく「人を呼ばない」側で扱う。
- 時刻は JST。

## 何を読むか

- セッション: `<設定の置き場>/sessions/*.json` のうちプロセスが生きているもの（claude-roster と同じ決まり）。置き場は `$CLAUDE_CONFIG_DIR`（無ければ `~/.claude`）・`~/.claude`・名簿の `config_dir`。`CLAUDE_SESSIONS_DIR`・`CLAUDE_PROJECTS_DIR` があればそちら。
- 役割: claude-state の記録の `role`、無ければ名簿（roster.json）。役割の分からないセッションと、寝ている役割は見ない。
- 会話の記録: session ID で `<置き場>/projects/*/<id>.jsonl` を全フォルダから探す（フォルダをまたいで再開すると、元のフォルダの側に書き足されるため）。数十 MB あるので末尾 2 MB → 16 MB → 全体の順に読み、人の発話が見つかったところでやめる。ほかのセッションでもっと新しい発話が見つかっていれば、それより古い記録は読まない。

## 人の発話の見分け方

`type: "user"` の行のうち、次の両方を満たすものだけを数える。

- `message.content` が文字列（ツールの結果のリストでない）で、`isMeta`・`turnCompanion`（画像などの付き物）でない。
- `origin.kind` か `turnOrigin` が `"human"`（片方でも別の値なら数えない）。

数えないもの: 別のセッションからの知らせ（`peer`。claude-relay の起こす知らせも）、定期の見回り（`turnOrigin: "scheduled"`）、背景の仕事の知らせ（`task-notification`）。本文が `<task-notification`・`<cross-session-message`・`<agent-message`・`<system-reminder` で始まるもの、`[relay-wake ids=`・`[claude-kitty disk]` を含むものも念のため除く（lib/presence.py と同じ印）。
`origin` も `turnOrigin` も無い行は、古い形式かもしれないので数えない。ほかに人の発話が無ければ `unknown` の理由に「出どころの無い古い形式の行」と出す。

数えないものは、ほかに次の 2 つ。どちらも誤って「起きている」と答えないため。

- 自動の要約（`isCompactSummary`・`isVisibleInTranscriptOnly`）。`turnOrigin` は要約を起こしたターンのものを引き継ぎ、`human` になりうる。
- 会話の最初の発話（`parentUuid` が無く `turnPosition.promptIndex` が 1）。統括が `claude "指示"` の引数で担当を起こすと、`human`・`typed` と記録される【計測】。人が新しい会話の最初に打ったものも落ちる（away 寄りになるだけ）。

作業中に打って待たせた発話（`promptSource: "queued"`）は、行の時刻が会話に入った時刻で人が打った時刻ではない（実例で 15.7 分遅れ）。その前にある同じ本文の `queue-operation` の `enqueue` の時刻を使い、見つからなければ（読んだ範囲より前・取り除かれた）数えない。
`origin` も `turnOrigin` も無い `<bash-input>`・`<command-name>`・`<local-command-stdout>` などの行は、古い形式に数えずに飛ばす。人の `!` コマンドの結果の `<bash-stdout>`（`turnOrigin: "human"`）は数える。

いまより 2 分以上先の時刻の行は使わない（時計のずれ・壊れた timestamp で active が続かないように）。

## 限界

- ほかのプロセスが kitty の `send-text` で担当の入力欄に打った文は、人の入力と見分けられない（claude-kitty のツールは send-text を使わない）。
- `claude --resume <ID> "指示"` の引数で渡した指示は、人と数える（`promptIndex` は 1 に戻るが `parentUuid` があり、resume のあとに人が打ったものと見分けられない）。claude-kitty のツールは resume に指示を付けない。
- 分は切り捨てて出す（20.9 分は「20分前」。判定は小数のまま比べる）。

claude-presence（hook が書く last.json）とは別に、記録そのものを読む。hook が入っていない・落ちたときにも使えるようにするため。
