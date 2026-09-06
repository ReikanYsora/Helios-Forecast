// Minimal Home Assistant websocket client on Node's built-in WebSocket: auth, then one command.
const HOST = process.env.HA_HOST ?? "192.168.0.2:8123", TOKEN = process.env.HA_TOKEN;
export function haCall(message) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://${HOST}/api/websocket`);
    let id = 1;
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "auth_required") ws.send(JSON.stringify({ type: "auth", access_token: TOKEN }));
      else if (m.type === "auth_ok") ws.send(JSON.stringify({ id, ...message }));
      else if (m.type === "auth_invalid") { reject(new Error("auth invalid")); ws.close(); }
      else if (m.type === "result") { if (m.success) resolve(m.result); else reject(new Error(JSON.stringify(m.error))); ws.close(); }
    };
    ws.onerror = (e) => reject(new Error("ws error " + (e.message ?? "")));
  });
}
