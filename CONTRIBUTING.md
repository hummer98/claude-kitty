# 貢献の手引き

claude-kitty への issue と pull request を歓迎します。ここには、出し方と、変更するときに守ってほしいことを書きます。
道具の使い方は [README.md](README.md)、組み立て方は [SETUP.md](SETUP.md)、運用の約束は [PRACTICES.md](PRACTICES.md) にあります。

## issue

- **不具合**: 何をしたら（コマンドと引数）、何が起き、何を期待したかを書いてください。次の情報があると早く直せます。
  - `claude-view --version` などの版（どのコマンドも `--version` で `名前 版` を出します）
  - OS とデスクトップ（例: Linux / KDE Plasma 6 Wayland、macOS）、`kitty --version`、`claude --version`、`python3 --version`
  - エラーの出力。hook の道具（`claude-state`・`claude-guard`・`claude-lang-check`）は失敗しても黙るので、`$XDG_RUNTIME_DIR/claude-state/errors.log` などのログも
- **提案**: 困っていること（何が手間か）を先に書いてください。作りの案はその後で構いません。
- 貼るものから、手元のセッション ID・役割名・パス・ユーザー名・メールアドレスを消してください（`claude-session-stats` の出力には発言の原文が入ります）。

## pull request

1. fork して、`main` から枝を切ります。
2. 変更とテストを足し、全部のテストを通します（下の「テスト」）。
3. [CHANGELOG.md](CHANGELOG.md) の `## [Unreleased]` に、利用者から見た変更を書きます（下の「CHANGELOG」）。
4. `main` に向けて PR を出します。GitHub Actions（`.github/workflows/test.yml`）が Python 3.12 と最新の Python でテストを回します。

1 つの PR には 1 つの目的を。直しと新しい機能、整形だけの変更は分けてください。

## テスト

```sh
python3 tests/run.py
```

これ 1 つで全部が走ります（`tests/run.py` が `tests/*_test.py` も読み込みます。新しいテストのファイルを足したら、`tests/run.py` の `SIDE_TESTS` にも足してください）。

- 標準ライブラリだけを使います。kitty・Claude Code・Chrome・KWin・Node は使いません（どれも偽物に置き換えています）。要るのは Python 3.9 以降（CI は 3.9・3.12・最新）、`git`、`pgrep`（procps）です。
- kitty のペインの中で走らせても、外と同じ結果になるようにしてください。テストは `KITTY_*` や `CLAUDE_*` の環境変数に頼らず、要るものは自分で与えます。CI と同じ条件は次で確認できます。
  ```sh
  env -i PATH=/usr/bin:/bin HOME="$(mktemp -d)" LANG=C.UTF-8 python3 tests/run.py
  ```
- 直しには、その不具合が起きる条件の回帰テストを、新しい機能には代表の場合と端の場合（空・無い・壊れた入力など）のテストを付けてください。
- 動いている kitty やほかのセッションに触れるテストは書かないでください（ペインを閉じたり、文字を打ち込んだりしうるため）。

## 守ること

この道具は、ほかの Claude Code セッションが動いている kitty の中で使われます。次のことは、どの変更でも守ってください。

- **ペインやタブを閉じる操作（`kitten @ close-window` / `close-tab` など）と `kitten @ send-text` を足さない。** 中で動いている Claude Code の作業や、人が打っている途中の入力を壊します。並べ替えは `detach-window`・`focus-window`・`layout_action` で行います。
- **同じセッション ID を 2 つのプロセスで同時に開く道を足さない。** 会話の記録が壊れえます。別の場所から移すときは `claude-wait` を通します（相手が確実に終わってから resume します）。
- **hook は決して失敗させず、stdout を汚さない。** `claude-state hook`・`claude-guard hook`・`claude-lang-check` は Claude Code の hook から呼ばれます。何が起きても exit 0 で終わり、例外はログ（`$XDG_RUNTIME_DIR/<道具>/errors.log`）に残して通します。stdout に出してよいのは、Claude Code に渡す決まった JSON（止めるときの `deny` / `block`）だけです。hook はツールを使うたびに走るので、重い import や外部コマンドを足すときは、要るときだけにしてください。
- **固有の名前を入れない。** 作者や利用者のセッション名・役割名・パス・ユーザー名・メールアドレス・特定のアプリの名前を、コード・例・テスト・ドキュメントに入れないでください。手元に合わせる値は `~/.config/claude-kitty/config.json`（リポジトリの外）から読み、例には `main`・`dev-a`・`fullscreen-app` のような一般の名前を使います。
- **`bin/` のファイルは別のファイルに書いてから `mv` で差し替える。** 動いているプロセスが読んでいることがあります（シェルスクリプトは実行中に読み進めます）。手元で直すときも同じです。
- フォーカスを勝手に動かさない（人が打っている途中の文字が別の窓に入るため）。kitty の窓を縮めたり動かしたりしない。
- 依存を増やさない。Python は標準ライブラリだけ、`lib/cdp.mjs` は Node の組み込みだけです。

## コミット

- 意味のある単位で分けます。
- メッセージは英語で、1 行目に何をしたか、本文に **なぜ** を書きます（何が困っていたか、なぜこの作りにしたか、何を確認したか）。差分を読めば分かる「何を」の繰り返しより、差分からは分からない理由を。
- 推測で直したところは、そう書いてください（例: 「These causes are inferred from the code; the fix still needs checking on the real desktop.」）。
- テストが通ってからコミットします。

## CHANGELOG

利用者から見て変わることは、PR の中で [CHANGELOG.md](CHANGELOG.md) の `## [Unreleased]` に書きます（形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)）。

- `### 追加`・`### 変更`・`### 修正` に分けます。内部の作り直しだけで動きが変わらないものは書かなくて構いません。
- **`git pull` だけでは済まないこと**（`~/.claude/settings.json` に hook を足す、kitty.conf を変える、systemd のユニットを入れ直す、設定ファイルのキーが変わる など）は、`### 上げるときに手で行うこと` に手順を書きます。いちばん読まれる所です。

## 版の上げ方（メンテナー向け）

版の番号は [SemVer](https://semver.org/lang/ja/) の 0.x です（1.0 までは、小さい方の数字の上がりにも互換の無い変更が入りえます。直しだけなら最後の数字を上げます）。

1. `CHANGELOG.md` の `## [Unreleased]` の中身を、新しい `## [X.Y.Z] - YYYY-MM-DD` の節に移し、`[Unreleased]` を空にします。末尾の比較リンク（`[Unreleased]: …/compare/vX.Y.Z...HEAD` と `[X.Y.Z]: …/compare/v前の版...vX.Y.Z`）も直します。
2. `VERSION` を `X.Y.Z` にします。
3. 1 と 2 をコミットします。
4. `tools/release.sh X.Y.Z --dry-run` で、確認する内容・release の本文・行う操作を見ます。
5. `tools/release.sh X.Y.Z` を実行します。

`tools/release.sh` は、`main` にいること・作業ツリーがきれいなこと・`VERSION` と CHANGELOG の見出しが合うこと・タグ `vX.Y.Z` が手元にも origin にも無いことを確認し、テストを走らせてから、軽量タグ `vX.Y.Z` を付け、`main` とそのタグだけを push します（`--all`・`--tags` は使いません）。タグの push を受けて `.github/workflows/release.yml` が、`main` の CHANGELOG の該当の節を本文にして GitHub の release を作ります。

過去の commit に版のタグを付けるときは `tools/release.sh X.Y.Z --at <commit>` です。`VERSION` とテストは見ず、`main` も push せず、タグだけを push します（その commit が origin/main にあり、origin/main の CHANGELOG にその版の節があること）。過去の commit には `release.yml` が無いのでタグの push では release が作られません。`gh workflow run release.yml -f tag=vX.Y.Z` で、既定の枝の定義から作ります。
