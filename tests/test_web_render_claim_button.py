"""「领取」按钮的真实浏览器渲染测试。

静态契约测试（tests/test_claim_button_state.py）只能证明"代码写了 canClaim"，
不能证明"点了真的没反应"——Vue 绑定写错、hydration 失败、模板语法错误
（比如 JS 模板字面量里混入真实换行导致整页编译失败）都会让静态断言照样通过。
所以这里起一个假 API + 无头 Chrome，把页面真渲染出来，直接读按钮的 disabled/title。

没有 Chrome 或没有 websocket 能力时跳过，不让测试环境差异变成红灯。
"""

import http.server
import json
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

pytestmark = pytest.mark.skipif(
    not Path(CHROME).exists(), reason="本机没有 Chrome，无法做真实渲染校验"
)

# account_id -> 签到状态（覆盖全部被禁用的分支 + 一个正常可领的对照）
FIXTURE_CHECKINS = {
    1: {"ok": True, "active": True, "claimed": True, "credit": 100},          # 刚领
    2: {"ok": True, "active": True, "already_claimed": True, "credit": 0},    # 今日已领
    3: {"ok": True, "active": False, "unavailable": True, "credit": 0},       # 活动未开启
    4: {"ok": True, "active": True, "claimed": False, "credit": 0},           # 可领取
    5: {"ok": True, "active": True, "claimed": False, "credit": 0},           # 账号 expired
}
FIXTURE_ACCOUNTS = [
    {
        "id": i,
        "name": name,
        "nickname": name,
        "uid": f"u{i}",
        "status": "expired" if i == 5 else "active",
        "provider": "workbuddy",
        "weight": 1,
        "priority": 0,
        "total_requests": 0,
        "total_tokens": 0,
        "total_credits": 0,
    }
    for i, name in [(1, "just-claimed"), (2, "claimed"), (3, "no-activity"), (4, "claimable"), (5, "expired")]
]

# 期望：(按钮是否禁用, title 里的关键词)
EXPECTED = {
    "just-claimed": (True, "今日已领取"),
    "claimed": (True, "今日已领取"),
    "no-activity": (True, "活动未开启"),
    "claimable": (False, ""),
    "expired": (True, "账号未启用"),
}


def _minimal_ws_client():
    """极简 WebSocket 客户端（避免为一个测试引入 websocket-client 依赖）。"""
    import base64
    import secrets
    import struct

    class WS:
        def __init__(self, url, timeout=30):
            rest = url[5:]
            hostport, _, path = rest.partition("/")
            host, _, port = hostport.partition(":")
            self.s = socket.create_connection((host, int(port)), timeout=timeout)
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            self.s.sendall(
                (
                    f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    f"Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.s.recv(4096)
                if not chunk:
                    raise RuntimeError("WebSocket 握手被关闭")
                buf += chunk
            assert b"101" in buf.split(b"\r\n")[0], buf[:200]
            self.buf = buf.split(b"\r\n\r\n", 1)[1]

        def _exact(self, n):
            while len(self.buf) < n:
                data = self.s.recv(65536)
                if not data:
                    raise EOFError
                self.buf += data
            out, self.buf = self.buf[:n], self.buf[n:]
            return out

        def send(self, text):
            payload = text.encode()
            mask = secrets.token_bytes(4)
            n = len(payload)
            header = bytearray([0x81])
            if n < 126:
                header.append(0x80 | n)
            elif n < 65536:
                header.append(0x80 | 126)
                header += struct.pack(">H", n)
            else:
                header.append(0x80 | 127)
                header += struct.pack(">Q", n)
            header += mask
            self.s.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def recv(self):
            while True:
                b1, b2 = self._exact(2)
                op = b1 & 0x0F
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._exact(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._exact(8))[0]
                data = self._exact(n)
                if op == 1:
                    return data.decode()
                if op == 8:
                    raise EOFError("WebSocket 已关闭")

        def close(self):
            try:
                self.s.close()
            except Exception:
                pass

    return WS


class _Handler(http.server.BaseHTTPRequestHandler):
    html = ""

    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(self.html, "text/html")
        if p == "/admin/accounts":
            return self._send(json.dumps(FIXTURE_ACCOUNTS))
        if p == "/admin/api-keys":
            return self._send("[]")
        if p == "/admin/models/catalogs":
            return self._send(json.dumps({"sources": []}))
        if p == "/admin/channels":
            return self._send(json.dumps({"channels": []}))
        if p == "/admin/site-preference":
            return self._send("{}")
        if p == "/admin/aliases":
            return self._send("{}")
        if p == "/admin/accounts/checkin-status-all":
            rows = [dict(v, account_id=k) for k, v in FIXTURE_CHECKINS.items()]
            return self._send(
                json.dumps(
                    {
                        "total": len(rows),
                        "ok": len(rows),
                        "claimed": 1,
                        "already_claimed": 1,
                        "unavailable": 1,
                        "failed": 0,
                        "results": rows,
                    }
                )
            )
        if p == "/admin/accounts/discover":
            return self._send(json.dumps({"accounts": [], "valid_count": 0}))
        return self._send("{}")

    def do_POST(self):
        return self._send("{}")


@pytest.fixture(scope="module")
def rendered_dom():
    _Handler.html = INDEX_HTML.read_text(encoding="utf-8")
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    debug_port = probe.getsockname()[1]
    probe.close()

    profile = "/tmp/buddy2api-claim-render-profile"
    subprocess.run(["rm", "-rf", profile], check=False)
    chrome = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=0.5)
                break
            except Exception:
                time.sleep(0.25)
        else:
            pytest.skip("Chrome 调试端口未就绪")

        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list").read())
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        ws = _minimal_ws_client()(ws_url)
        counter = [0]

        def call(method, params=None):
            counter[0] += 1
            ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == counter[0]:
                    return msg

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
        time.sleep(6)
        expr = (
            "JSON.stringify({mounted:!!document.querySelector('[data-v-app]'),"
            "rows:document.querySelectorAll('table tbody tr').length,"
            "claim:[...document.querySelectorAll('button')]"
            ".filter(b=>b.textContent.trim()==='领取')"
            ".map(b=>({dis:b.disabled,title:b.title,row:b.closest('tr')"
            "?.previousElementSibling?.querySelector('td')?.innerText||''}))})"
        )
        res = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        value = res.get("result", {}).get("result", {}).get("value")
        assert value, f"页面未能渲染：{json.dumps(res)[:400]}"
        ws.close()
        yield json.loads(value)
    finally:
        chrome.terminate()
        server.shutdown()


def test_page_renders_with_accounts(rendered_dom):
    """先确认页面真的挂载了账号列表，否则后面的断言都是空转。"""
    assert rendered_dom["mounted"], "Vue 应用没有挂载（可能是模板编译失败导致白屏）"
    assert rendered_dom["rows"] >= len(FIXTURE_ACCOUNTS), (
        f"只渲染出 {rendered_dom['rows']} 行，期望至少 {len(FIXTURE_ACCOUNTS)} 行"
    )
    assert len(rendered_dom["claim"]) == len(FIXTURE_ACCOUNTS), (
        f"领取按钮数量 {len(rendered_dom['claim'])} 与账号数不符"
    )


def test_claim_button_disabled_state_matches_checkin(rendered_dom):
    """逐个账号核对按钮可点性与禁用原因。"""
    mismatches = []
    for btn in rendered_dom["claim"]:
        name = (btn["row"] or "").split("\n")[0].strip()
        want_dis, want_title = EXPECTED.get(name, (None, None))
        if want_dis is None:
            mismatches.append(f"未知账号 {name!r}（夹具与页面不同步）")
            continue
        if btn["dis"] != want_dis:
            mismatches.append(f"{name}: disabled={btn['dis']}，期望 {want_dis}")
        if want_title and want_title not in (btn["title"] or ""):
            mismatches.append(f"{name}: title={btn['title']!r}，期望包含 {want_title!r}")
    assert not mismatches, "领取按钮状态不符：\n" + "\n".join(mismatches)


def test_already_claimed_accounts_cannot_be_clicked(rendered_dom):
    """用户报告的原始问题：今天领过的账号按钮还能点。单独钉一条回归。"""
    clickable = [
        b["row"].split("\n")[0].strip()
        for b in rendered_dom["claim"]
        if not b["dis"] and ("claimed" in (b["row"] or "") or "no-activity" in (b["row"] or ""))
    ]
    assert not clickable, f"这些账号已领过/无活动，但按钮仍可点：{clickable}"


def test_at_least_one_account_remains_claimable(rendered_dom):
    """防止修过头：还有可领账号时，按钮必须保持可点。"""
    assert any(not b["dis"] for b in rendered_dom["claim"]), (
        "所有领取按钮都被禁用了，可领账号也点不动"
    )
