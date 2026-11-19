// claude-kitty の kitty ウィンドウを仮想デスクトップ2へ移して最大化する
for (const w of workspace.windowList()) {
    if (w.resourceClass === "kitty-claude" && workspace.desktops.length >= 2) {
        w.desktops = [workspace.desktops[1]];
        w.setMaximize(true, true);
    }
}
