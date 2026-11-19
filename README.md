# claude-kitty

複数の [Claude Code](https://docs.claude.com/en/docs/claude-code) セッションを、[kitty](https://sw.kovidgoyal.net/kitty/) の 1 つの窓にペインとして並べて動かすための小さな道具の集まり。

- 1 つの「統括」セッション（左の列）が、ほかの「担当」セッションのペインを `kitten @`（kitty のリモート操作）で並べ替える。人がマウスで直さなくても、「この 2 人を右に出して」「全員を等分に」ができる。
- ほかの端末で動いているセッションを、kitty のペインへ安全に移す（同じセッションを 2 か所で同時に開かない）。
- 各セッションが自分専用の Chrome を自分のペインの上に重ね、Web アプリの動作を確かめる（プロファイルが残るのでログインは一度だけ）。

作者の環境は Linux（KDE Plasma 6 Wayland）。窓の操作に KWin スクリプトを使う部分は Linux/KDE 専用で、kitty と `kitten @` の部分は OS を問わない。セットアップの手順と macOS への移植の手引きは [SETUP.md](SETUP.md)。

| 道具 | 何をするか | OS 依存 |
|---|---|---|
| `bin/claude-kitty` | kitty を `CLAUDE_*` 環境変数なしで起動し、セッションファイルどおりにペインを開く | 仮想デスクトップへの移動だけ KWin |
| `bin/claude-view` | 統括用。ペインを役割名で並べ替える（等分割にそろえる） | 「保護する窓が前面か」の確認だけ KWin、kitty の生存確認に `/proc` |
| `bin/claude-wait` | 別の場所で動いている同じセッションの終了を待ってから resume する | `/proc` |
| `bin/claude-browser` | ペインごとの Chrome を、そのペインに重ねる | KWin・Flatpak・`/proc` |

テスト: `python3 tests/run.py`（標準ライブラリだけ。kitty や Chrome は使わない）

## claude-kitty

`claude-kitty [session]` — kitty を CLAUDE_* 環境変数なしで起動し（Claude Code の中から起動すると、中の claude が子セッション扱いになり会話記録が残らなくなるのを避ける）、仮想デスクトップ 2 に最大化する。既定の配置は `kitty/claude3.session`（左 1/4 + 右上下）。

- `kitty/example.session`: 統括 1 + 担当 5 の例（各ペインが `claude-wait` で待ってから resume する）。手元の版は `kitty/<名前>.session` に置けば git に入らない（`.gitignore`）。

## claude-view（統括用の配置）

`claude-view all | focus ROLE | pair A B | restore | add ROLE [--row N] | remove ROLE | swap A B | equalize | rows "a,b/c" | status`

- ペインは kitty の user var `role`（セッションファイルの `--var role=...`）で呼ぶ。
- 左の列（`--left`、既定は設定の `view.left`、無ければ `main`）は固定。毎回いったん左以外を standby タブへ下げてから組み直し、最後に入力先を左へ戻す。
- どの操作も等分割にそろえる（段ごとに幅を等しく、左の列も 1 列分）。
- `restore` は設定の `view.restore`（既定は右上 `dev-a`、右下 `dev-b`）。
- 設定の `view.guard`（プロセスの正規表現）に当たる窓（ゲームなど）が前面なら何もせず通知だけ（`--force` で実行）。close 系と send-text は使わない。
- ペインの位置は `kitten @ ls` の `layout_state.pairs`（分割の木）から計算する。`neighbors` の番号は window id ではなく group id で、ペインを移すとずれる。
- `--dry-run` は kitty に `ls` だけして、行う操作を表示する。

### 設定

`~/.config/claude-kitty/config.json`（`$CLAUDE_KITTY_CONFIG` か `$XDG_CONFIG_HOME` で場所を変えられる。リポジトリの外に置く）:

```json
{
  "view": {
    "left": "main",
    "restore": [["dev-a"], ["dev-b"]],
    "guard": ["^\\./SomeGame"]
  }
}
```

優先順は 引数 > 環境変数（`CLAUDE_VIEW_LEFT`、`CLAUDE_VIEW_RESTORE="a,b/c"`、`CLAUDE_VIEW_GUARD`。`none` で guard を止める。空文字は未設定と同じ）> 設定ファイル > 既定値。kitten の場所は `CLAUDE_KITTY_KITTEN`、kitty の場所は `CLAUDE_KITTY_KITTY` で変えられる。

## claude-wait

`claude-wait <名前> <セッションID> [--rename]` — 同じセッションが別の場所で動いている間は待ち、プロセスが確実に終わってから resume する（JSON の一瞬の空・後片付け中の削除・/clear・pid の再利用・同じ ID の二重待機に対応）。
- 待つ相手の JSON の `kind` が `"bg"`（daemon の中の背景セッション）なら、前面の claude を閉じても終わらないので、その旨と pid を表示する（自動では止めない。idle を確かめてから人が止める）。
- `--rename` の `-n` は resume では効かないので、再開の直前に「再開したら /rename <名前> と打ってください」と表示する。

## claude-browser

ペインごとに独立した Chrome（Flatpak、専用プロファイル・専用 CDP ポート）を kitty のペインに「常に最前面」で重ねる。kitty は縮めない（縮めると全ペインの claude-code が再描画で崩れることがある）。

- プロファイル名は起動し直しても変わらないもの: `--key` > ペインの user var `role` > `slot` > セッション ID。ログインが次回も残る。
- 台帳 `~/.var/app/com.google.Chrome/claude-profiles/ledger.json`（`profiles` で表示、`note TEXT` でメモ）。
- 既存の Chrome はプロセスの引数（`--user-data-dir` と `--remote-debugging-port`）から見つけるので、state が消えても引き継ぎ・close できる。
- `open URL [--mode self|show] [--ratio 0.65] [--over SLOT|ID] [--focus kitty|browser|none]`
  - self: 自分のペイン全体を覆う（自分の作業用）
  - show: ペインの上 ratio だけ覆い、claude-code の最新出力と入力欄を見せたまま対話する
  - `--over`: 別のペイン（slot 名 left/right-top/right-bottom か window id）に重ねる
  - `--focus none`: 入力先を変えない（全画面のアプリを使っている最中など）
- `open` を呼び直しても、未指定の mode/ratio/over は前回の値を引き継ぐ。
- `move …` / `close` / `shot FILE`（CDP）/ `url` / `status` / `list` / `profiles` / `note TEXT`
- タブの切り替えや分割比の変更には追従しないので、そのときは `move` を呼ぶ。
- Flatpak の Chrome はホームの任意の場所を読めない。file:// で手元の資料を開くなら `flatpak override --user --filesystem=~/docs:ro com.google.Chrome` のように許可する。
- 返り値の `cdp` URL に Playwright の `connectOverCDP` などで接続して操作できる。
- プロファイルは `~/.var/app/com.google.Chrome/claude-profiles/<key>` に残るので、パスキー等のログインは一度人が済ませれば以後使える。

## ライセンス

MIT（[LICENSE](LICENSE)）
