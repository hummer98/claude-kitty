// claude-kitty の kitty の窓を、設定の仮想デスクトップへ移す（KDE Plasma の KWin スクリプト）。
// bin/claude-kitty が @@DESKTOP@@（1 から数えた番号）と @@MAXIMIZE@@（true / false）を、設定の kitty.desktop /
// kitty.maximize で置き換えてから読み込む。kitty.desktop を設定していなければ、このスクリプトは使わない。
const DESKTOP = @@DESKTOP@@;
const MAXIMIZE = @@MAXIMIZE@@;
for (const w of workspace.windowList()) {
    if (w.resourceClass === "kitty-claude" && workspace.desktops.length >= DESKTOP) {
        w.desktops = [workspace.desktops[DESKTOP - 1]];
        if (MAXIMIZE) {
            w.setMaximize(true, true);
        }
    }
}
