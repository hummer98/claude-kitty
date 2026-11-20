// Chrome DevTools Protocol の最小クライアント（依存なし、Node 22+ の組み込み WebSocket を使う）。
// usage: node cdp.mjs <port> close | shot <file> | goto <url> | url | window | place <windowId> <x> <y> <w> <h> | front
// window・place・front は macOS の重ね置き（KWin の代わり。claude-browser）が使う。座標は画面のポイント。
const [port, cmd, arg, ...rest] = process.argv.slice(2);
const base = `http://127.0.0.1:${port}`;

function rpc(wsUrl, method, params = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => { ws.close(); reject(new Error(`timeout: ${method}`)); }, 30000);
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method, params }));
    ws.onerror = (e) => { clearTimeout(timer); reject(new Error(`websocket error: ${e.message || e.type}`)); };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id !== 1) return;
      clearTimeout(timer);
      ws.close();
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    };
  });
}

async function activePage() {
  // /json/list は最近使った順に並ぶので、先頭の page がいま前面のタブ
  const list = await (await fetch(`${base}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("no page target");
  return page;
}

try {
  if (cmd === "close") {
    const v = await (await fetch(`${base}/json/version`)).json();
    await rpc(v.webSocketDebuggerUrl, "Browser.close").catch(() => {}); // 閉じると応答前に切れることがある
  } else if (cmd === "shot") {
    const page = await activePage();
    const r = await rpc(page.webSocketDebuggerUrl, "Page.captureScreenshot", { format: "png" });
    const fs = await import("node:fs");
    fs.writeFileSync(arg, Buffer.from(r.data, "base64"));
    console.log(arg);
  } else if (cmd === "goto") {
    const list = await (await fetch(`${base}/json/list`)).json();
    const page = list.find((t) => t.type === "page");
    if (page) {
      const r = await rpc(page.webSocketDebuggerUrl, "Page.navigate", { url: arg });
      if (r.errorText) throw new Error(`navigate: ${r.errorText}`);
    } else {
      // タブを全部閉じられていたら新しく作る
      const v = await (await fetch(`${base}/json/version`)).json();
      await rpc(v.webSocketDebuggerUrl, "Target.createTarget", { url: arg });
    }
  } else if (cmd === "url") {
    console.log((await activePage()).url);
  } else if (cmd === "window") {
    // 前面のタブの窓の ID（Browser.setWindowBounds に渡す）
    const page = await activePage();
    const v = await (await fetch(`${base}/json/version`)).json();
    const r = await rpc(v.webSocketDebuggerUrl, "Browser.getWindowForTarget", { targetId: page.id });
    console.log(String(r.windowId));
  } else if (cmd === "place") {
    const windowId = Number(arg);
    const [left, top, width, height] = rest.map(Number);
    if (!Number.isInteger(windowId) || ![left, top, width, height].every(Number.isFinite)) throw new Error("place: bad args");
    const v = await (await fetch(`${base}/json/version`)).json();
    const ws = v.webSocketDebuggerUrl;
    const cur = await rpc(ws, "Browser.getWindowBounds", { windowId });
    // 最大化・全画面・最小化のままでは大きさを変えられないので、先にふつうの窓に戻す（同じ呼び出しでは混ぜられない）
    if (cur.bounds && cur.bounds.windowState && cur.bounds.windowState !== "normal") {
      await rpc(ws, "Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
    }
    await rpc(ws, "Browser.setWindowBounds", { windowId, bounds: { left, top, width, height } });
    const got = (await rpc(ws, "Browser.getWindowBounds", { windowId })).bounds;
    console.log(JSON.stringify([got.left, got.top, got.width, got.height]));
  } else if (cmd === "front") {
    const page = await activePage();
    await rpc(page.webSocketDebuggerUrl, "Page.bringToFront");
  } else {
    throw new Error(`unknown command: ${cmd}`);
  }
} catch (e) {
  console.error(`cdp: ${e.message}`);
  process.exit(1);
}
