#!/usr/bin/env python3
"""Keeps the VPN connected, and serves its status and sign-in page.

The VPN signs in through single sign-on with Cisco's "SSO v2" flow, which Cisco's client
runs in its own browser window. Here that window is Chromium on a virtual display, which
you can view through noVNC. The flow:

1. Ask the VPN server to start a sign-in; it returns the identity provider's sign-in URL
   (such as Microsoft Entra ID's).
2. Open that URL in Chromium. With a remembered sign-in it completes by itself;
   otherwise the page asks you to sign in through noVNC.
3. The server ends the flow by setting the acSamlv2Token cookie. Hand that to the
   server for a VPN session cookie, and start openconnect with it.

When the VPN session ends, start over, so it reconnects without you while the identity
provider remembers the sign-in.
"""
import collections
import html
import http.cookiejar
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from playwright.sync_api import sync_playwright

VPN_SERVER = os.environ["VPN_SERVER"]
VPN_NAME = os.environ.get("VPN_NAME") or "VPN"
TS_HOSTNAME = os.environ.get("TS_HOSTNAME") or "localhost"
ROUTE_MODE = os.environ.get("ROUTE_MODE", "split")
AUTO_CONNECT = os.environ.get("AUTO_CONNECT", "true") == "true"
# The server only offers SSO to clients that identify as Cisco's.
CLIENT_VERSION = "4.10.07061"
USER_AGENT = f"AnyConnect Linux_64 {CLIENT_VERSION}"
BROWSER_PROFILE = "/var/lib/vpn-exit-node/browser"
CONNECTED_FILE = "/run/vpn-exit-node/connected"  # written by vpn-script
# The virtual display's size (Xvfb in entrypoint.sh), which noVNC shows.
SCREEN_WIDTH, SCREEN_HEIGHT = 1024, 768
# How long a sign-in may take before it's clear the identity provider wants you to do it.
UNATTENDED_SIGN_IN = 20

log = collections.deque(maxlen=300)
lock = threading.Lock()
state, detail = "disconnected", ""
want = threading.Event()  # set while the VPN should be up
wake = threading.Event()  # cuts a wait short
restart = threading.Event()  # start the sign-in over with a fresh request
proc = None


class Cancelled(Exception):
    pass


class Restart(Exception):
    pass


def note(line):
    line = line.rstrip()
    log.append(time.strftime("%H:%M:%S ") + line)
    print(line, flush=True)


def set_state(new, why=""):
    global state, detail
    with lock:
        state, detail = new, why


def wait(seconds):
    wake.wait(seconds)
    wake.clear()


def xml_request(opener, body):
    req = urllib.request.Request(f"https://{VPN_SERVER}/", data=body.encode(), headers={
        "User-Agent": USER_AGENT,
        "X-Transcend-Version": "1",
        "X-Aggregate-Auth": "1",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with opener.open(req, timeout=30) as resp:
        return ET.fromstring(resp.read())


def config_auth(kind, inner):
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<config-auth client="vpn" type="{kind}" aggregate-auth-version="2">'
            f'<version who="vpn">{CLIENT_VERSION}</version><device-id>linux-64</device-id>'
            f'{inner}</config-auth>')


def sign_in(ctx):
    """Run the SSO flow in the browser and return the VPN session cookie."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    init = xml_request(opener, config_auth("init",
        f"<group-access>https://{VPN_SERVER}</group-access>"
        "<capabilities><auth-method>single-sign-on-v2</auth-method></capabilities>"))
    auth = init.find("auth")
    if init.get("type") != "auth-request" or auth is None or auth.find("sso-v2-login") is None:
        raise RuntimeError(f"The server didn't offer SSO: {init.findtext('error') or ET.tostring(init, 'unicode')}")
    opaque = init.find("opaque")
    opaque.tail = None
    token_name = auth.findtext("sso-v2-token-cookie-name")
    error_name = auth.findtext("sso-v2-error-cookie-name")

    # A token left over from an earlier sign-in would be taken for this one's.
    ctx.clear_cookies(domain=VPN_SERVER)
    page = ctx.new_page()
    fit_window(page)
    page.goto(auth.findtext("sso-v2-login"))
    started = time.monotonic()
    try:
        while True:
            if not want.is_set():
                raise Cancelled()
            if restart.is_set():
                raise Restart()
            if page.is_closed():
                raise RuntimeError("The sign-in window was closed.")
            if "/+CSCOE+/message.html" in page.url:
                # The server's own error page, e.g. "Validation failure."
                try:
                    message = page.inner_text("body").strip()
                except Exception:
                    message = page.url
                raise RuntimeError(f"The VPN server rejected the sign-in: {message}")
            cookies = {c["name"]: c["value"] for c in ctx.cookies(f"https://{VPN_SERVER}")}
            if cookies.get(error_name):
                raise RuntimeError(f"Sign-in failed: {urllib.parse.unquote(cookies[error_name])}")
            if cookies.get(token_name):
                token = cookies[token_name]
                break
            if state == "signing-in" and time.monotonic() - started > UNATTENDED_SIGN_IN:
                note("Waiting for you to sign in in the browser")
                set_state("needs-sign-in")
            wait(1)
    finally:
        if not page.is_closed():
            page.close()

    reply = xml_request(opener, config_auth("auth-reply",
        "<session-token/><session-id/>" + ET.tostring(opaque, "unicode") +
        f"<auth><sso-token>{escape(token)}</sso-token></auth>"))
    cookie = reply.findtext("session-token")
    if reply.get("type") != "complete" or not cookie:
        raise RuntimeError(f"The server refused the sign-in: {reply.findtext('error') or ET.tostring(reply, 'unicode')}")
    return cookie


def run_vpn(cookie):
    global proc
    with lock:
        if not want.is_set():
            return None
        proc = subprocess.Popen(
            ["openconnect", "--protocol=anyconnect", f"--useragent={USER_AGENT}",
             f"--version-string={CLIENT_VERSION}", "--cookie-on-stdin",
             "--script=/usr/local/bin/vpn-script", VPN_SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    proc.stdin.write(cookie + "\n")
    proc.stdin.close()
    for line in proc.stdout:
        note(line)
    return proc.wait()


def fit_window(page):
    """Make the page's browser window exactly fill the virtual display.

    Chromium restores the window size its profile remembers rather than --window-size,
    and that can be larger than the display, which cuts the window off in noVNC.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {
            "left": 0, "top": 0, "width": SCREEN_WIDTH, "height": SCREEN_HEIGHT}})
        cdp.detach()
    except Exception as err:
        note(f"Couldn't fit the browser window to the display: {err}")


def open_browser(pw):
    ctx = pw.chromium.launch_persistent_context(
        BROWSER_PROFILE, executable_path="/usr/bin/chromium", headless=False, viewport=None,
        args=["--window-position=0,0", f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
              "--no-first-run", "--no-default-browser-check", "--password-store=basic",
              # No "Restore pages?" after the container stopped, and no warning bar about
              # --no-sandbox (needed to run as root): neither fits on the small display.
              "--hide-crash-restore-bubble", "--test-type"])
    ctx.closed = False
    ctx.on("close", lambda _: setattr(ctx, "closed", True))
    for page in ctx.pages:
        fit_window(page)
    return ctx


def supervise():
    failures = 0
    with sync_playwright() as pw:
        ctx = None
        while True:
            want.wait()
            try:
                if ctx is None or ctx.closed:
                    ctx = open_browser(pw)
                set_state("signing-in")
                note(f"Signing in to {VPN_SERVER}")
                cookie = sign_in(ctx)
                set_state("connecting")
                started = time.monotonic()
                code = run_vpn(cookie)
                note(f"openconnect exited with code {code}")
                # A session that lasted a while ended normally; reconnect straight away.
                failures = 0 if time.monotonic() - started > 300 else failures + 1
                set_state("retrying", f"The last session ended at {time.strftime('%H:%M')} (openconnect exit code {code}).")
            except Cancelled:
                pass
            except Restart:
                restart.clear()
                continue
            except Exception as err:
                failures += 1
                note(f"Error: {err}")
                set_state("retrying", str(err))
            if not want.is_set():
                set_state("disconnected")
                continue
            delay = min(300, 5 * 2 ** failures) if failures > 1 else 5
            wait(delay)


def connect():
    want.set()
    wake.set()


def disconnect():
    want.clear()
    wake.set()
    with lock:
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT)  # logs the session off before exiting


def tailscale_state():
    try:
        out = subprocess.run(["tailscale", "status", "--json", "--peers=false"],
                             capture_output=True, text=True, timeout=5).stdout
        st = json.loads(out)
    except Exception:
        return {"state": "unknown"}
    me = st.get("Self") or {}
    return {
        "state": st.get("BackendState"),
        "auth_url": st.get("AuthURL"),
        "name": (me.get("DNSName") or "").rstrip("."),
        "ips": me.get("TailscaleIPs") or [],
        "exit_node": me.get("ExitNodeOption"),
    }


def status():
    with lock:
        current, why = state, detail
    session = None
    if current == "connecting" and os.path.exists(CONNECTED_FILE):
        current = "connected"
        with open(CONNECTED_FILE) as f:
            session = dict(l.split("=", 1) for l in f.read().splitlines() if "=" in l)
    return {"vpn": current, "detail": why, "session": session, "tailscale": tailscale_state()}


STYLE = """
:root { --bg:#f3f5f8; --card:#fff; --fg:#14181d; --muted:#5f6b78; --line:#e2e6eb;
        --soft:#f6f8fa; --accent:#2563eb; --accent-fg:#fff;
        --ok:#15803d; --ok-bg:#e9f7ee; --warn:#a15c00; --warn-bg:#fdf3e1;
        --bad:#b42318; --bad-bg:#fdecea; --code-bg:#0f141a; --code-fg:#d6dde5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0b0f14; --card:#131920; --fg:#e7ecf2; --muted:#8b97a5; --line:#232c36;
          --soft:#182029; --accent:#4f8cff; --accent-fg:#fff;
          --ok:#4ade80; --ok-bg:#12291b; --warn:#f5b84b; --warn-bg:#2d2310;
          --bad:#f87171; --bad-bg:#2e1515; --code-bg:#080b0f; --code-fg:#c9d2dc; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width:840px; margin:0 auto; padding:32px 16px 40px; }
header { display:flex; align-items:center; gap:12px; margin-bottom:20px; }
.logo { width:40px; height:40px; border-radius:12px; display:grid; place-items:center;
        background:var(--accent); color:var(--accent-fg); flex:none; }
h1 { font-size:20px; line-height:1.2; margin:0; }
.sub { color:var(--muted); font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px;
        box-shadow:0 1px 2px rgba(16,24,40,.04); }
.hero { display:flex; gap:16px; align-items:flex-start; flex-wrap:wrap; margin-bottom:16px; }
.hero .text { flex:1 1 280px; min-width:0; }
.pill { display:inline-flex; align-items:center; gap:8px; font-weight:650; font-size:14px;
        color:var(--tone); background:var(--tone-bg); padding:4px 12px 4px 10px; border-radius:999px; }
.dot { width:9px; height:9px; border-radius:50%; background:var(--tone); }
.busy .dot { animation:pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.3; } }
.hero p { margin:10px 0 0; color:var(--muted); }
.ok { --tone:var(--ok); --tone-bg:var(--ok-bg); }
.warn { --tone:var(--warn); --tone-bg:var(--warn-bg); }
.bad { --tone:var(--bad); --tone-bg:var(--bad-bg); }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px; margin-bottom:16px; }
h2 { font-size:12px; font-weight:650; text-transform:uppercase; letter-spacing:.06em;
     color:var(--muted); margin:0 0 12px; display:flex; justify-content:space-between; gap:8px; }
h2 .pill { font-size:12px; text-transform:none; letter-spacing:0; padding:2px 10px 2px 8px; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:8px 16px; margin:0; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
.mono { font:13px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { background:var(--soft); border:1px solid var(--line); border-radius:6px; padding:0 7px; }
ol.steps { list-style:none; counter-reset:step; padding:0; margin:14px 0 0; }
ol.steps li { counter-increment:step; display:flex; gap:12px; margin-bottom:12px; }
ol.steps li::before { content:counter(step); flex:none; width:24px; height:24px; border-radius:50%;
                      display:grid; place-items:center; font-size:13px; font-weight:650;
                      background:var(--soft); border:1px solid var(--line); }
a { color:var(--accent); }
.button, button { display:inline-flex; align-items:center; gap:8px; font:inherit; font-weight:600;
                  padding:9px 16px; border-radius:10px; border:1px solid var(--accent);
                  background:var(--accent); color:var(--accent-fg); text-decoration:none; cursor:pointer; }
.button:hover, button:hover { filter:brightness(1.08); }
button.secondary { background:transparent; color:var(--fg); border-color:var(--line); }
button.secondary:hover { background:var(--soft); filter:none; }
form { margin:0; }
details.card { padding:0; }
details summary { cursor:pointer; padding:14px 20px; font-weight:600; list-style:none;
                  display:flex; justify-content:space-between; }
details summary::after { content:"Show"; color:var(--accent); font-weight:500; font-size:14px; }
details[open] summary::after { content:"Hide"; }
details .body { padding:0 20px 20px; }
details pre { margin:12px 0 0; background:var(--code-bg); color:var(--code-fg); border-radius:10px;
              padding:14px; font-size:12px; line-height:1.55; max-height:340px; overflow:auto;
              white-space:pre-wrap; overflow-wrap:anywhere; }
footer { color:var(--muted); font-size:12px; text-align:center; margin-top:20px; }
"""

SHIELD = ("<svg width=22 height=22 viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 "
          "stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
          "<path d='M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z'/><path d='M9 12l2 2 4-4'/></svg>")


def since_text(since):
    """`2026-10-20 09:12` as `since 09:12 (3h 05m)`, or as it is when it doesn't parse."""
    try:
        start = time.mktime(time.strptime(since, "%Y-%m-%d %H:%M"))
    except (TypeError, ValueError):
        return since or "–"
    minutes = max(0, int(time.time() - start) // 60)
    took = f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"
    return f"{since[11:]} ({took})"


def page():
    s = status()
    e = html.escape
    ts = s["tailscale"]
    vpn = s["vpn"]
    browser_url = "browser"
    node = e(ts.get("name", "").split(".")[0] or TS_HOSTNAME)
    out = ["<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width, initial-scale=1'>"
           f"<title>{e(VPN_NAME)}</title><style>{STYLE}</style></head><body><main>"
           f"<header><div class=logo>{SHIELD}</div><div><h1>{e(VPN_NAME)}</h1>"
           f"<div class=sub>{e(VPN_SERVER)} · Tailscale exit node <span class=mono>{node}</span></div>"
           "</div></header>"]

    reach = "its networks and the internet" if ROUTE_MODE == "full" else "its networks"
    label, tone, busy, says = {
        "connected": ("Connected", "ok", False,
                      f"Devices that use <b>{node}</b> as their exit node reach {reach} through the VPN."),
        "signing-in": ("Signing in", "warn", True, "Signing in to the VPN server."),
        "needs-sign-in": ("Waiting for you to sign in", "warn", False,
                          "The VPN wants you to sign in again. It takes a minute."),
        "connecting": ("Connecting", "warn", True, "Signed in. Starting the VPN tunnel."),
        "retrying": ("Reconnecting soon", "warn", True, "The VPN session ended. It starts a new one by itself."),
        "disconnected": ("Disconnected", "bad", False,
                         f"Devices that use <b>{node}</b> as their exit node aren't going through the VPN."),
    }[vpn]
    out.append(f"<section class='card hero {tone}{' busy' if busy else ''}'><div class=text>"
               f"<span class=pill><span class=dot></span>{label}</span><p>{says}</p>")
    if s["detail"] and vpn in ("retrying", "disconnected"):
        out.append(f"<p>{e(s['detail'])}</p>")
    if vpn == "needs-sign-in":
        out.append(
            "<ol class=steps>"
            f"<li><span><a class=button href='{e(browser_url)}' target=_blank rel=noopener>"
            "Open the sign-in browser</a></span></li>"
            "<li><span>Sign in with your VPN account. If it asks whether to stay signed in, "
            "choose <strong>Yes</strong>, so the VPN can reconnect by itself later.</span></li>"
            "<li><span>This page shows <em>Connected</em> when it's done.</span></li></ol>")
    out.append("</div>")
    if vpn == "disconnected":
        out.append("<form method=post action=connect><button>Connect</button></form>")
    else:
        out.append("<form method=post action=disconnect><button class=secondary>Disconnect</button></form>")
    out.append("</section><div class=grid>")

    out.append(f"<section class=card><h2><span>VPN session</span></h2><dl>"
               f"<dt>Server</dt><dd class=mono>{e(VPN_SERVER)}</dd>")
    if vpn == "connected" and s["session"]:
        se = s["session"]
        routes = "".join(f"<span class='chip mono'>{e(r)}</span>" for r in (se.get("routes") or "").split())
        out.append(f"<dt>Since</dt><dd>{e(since_text(se.get('since')))}</dd>"
                   f"<dt>Address</dt><dd class=mono>{e(se.get('address') or '–')}</dd>"
                   f"<dt>DNS</dt><dd class=mono>{e(se.get('dns') or '–')}</dd>"
                   f"<dt>Domain</dt><dd class=mono>{e(se.get('domain') or '–')}</dd>"
                   f"<dt>Claims</dt><dd><div class=chips>{routes or '–'}</div></dd>")
    mode = "All traffic" if ROUTE_MODE == "full" else "Only the VPN's networks"
    out.append(f"<dt>Mode</dt><dd>{e(mode)} <span class=sub>· {e(ROUTE_MODE)}</span></dd></dl></section>")

    ts_ok = ts["state"] == "Running"
    ts_tone = "ok" if ts_ok else "bad"
    out.append(f"<section class=card><h2><span>Tailscale</span><span class='pill {ts_tone}'>"
               f"<span class=dot></span>{e(ts['state'] or 'unknown')}</span></h2>")
    if ts.get("auth_url"):
        out.append(f"<p><a class=button href='{e(ts['auth_url'])}' target=_blank rel=noopener>"
                   "Log this node in to Tailscale</a></p>")
    if ts_ok:
        exit_node = {True: "Approved", False: "Waiting for approval in the admin console"}.get(ts.get("exit_node"), "–")
        out.append(f"<dl><dt>Name</dt><dd class=mono>{e(ts['name'])}</dd>"
                   f"<dt>Addresses</dt><dd class=mono>{'<br>'.join(e(ip) for ip in ts['ips'])}</dd>"
                   f"<dt>Exit node</dt><dd>{e(exit_node)}</dd></dl>")
    out.append("</section></div>")

    out.append("<details class=card><summary>Log</summary><div class=body>"
               f"<a href='{e(browser_url)}' target=_blank rel=noopener>Open the sign-in browser</a>"
               "<pre>" + e("\n".join(list(log)[-80:])) + "</pre></div></details>"
               "<footer>This page updates by itself.</footer>")
    out.append("<script>const shown = " + json.dumps({"vpn": vpn, "ts": ts["state"]}) + ";"
               "setInterval(async () => { try { const s = await (await fetch('status')).json();"
               " if (s.vpn !== shown.vpn || s.tailscale.state !== shown.ts) location.replace('./'); }"
               " catch (e) {} }, 3000);</script></main></body></html>")
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            self.send(200, json.dumps(status()), "application/json")
        elif self.path == "/":
            self.send(200, page())
        elif self.path == "/browser":
            # The server's sign-in request expires ("Validation failure"), so one that's
            # been waiting a while is replaced before you start.
            if status()["vpn"] == "needs-sign-in":
                restart.set()
                wake.set()
                time.sleep(4)
            host = (self.headers.get("Host") or TS_HOSTNAME).rsplit(":", 1)[0]
            self.send_response(303)
            self.send_header("Location", f"http://{host}:6080/vnc.html?autoconnect=1&resize=scale&reconnect=1")
            self.end_headers()
        else:
            self.send(404, "Not found", "text/plain")

    def do_POST(self):
        if self.path == "/connect":
            connect()
        elif self.path == "/disconnect":
            disconnect()
        else:
            return self.send(404, "Not found", "text/plain")
        self.send_response(303)
        self.send_header("Location", "./")
        self.end_headers()

    def log_message(self, *args):
        pass


def shutdown(*_):
    # Log the VPN session off instead of leaving it to time out on the server.
    disconnect()
    if proc:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    os.makedirs(os.path.dirname(CONNECTED_FILE), exist_ok=True)
    if AUTO_CONNECT:
        want.set()
    threading.Thread(target=supervise, daemon=True).start()
    port = int(os.environ.get("WEB_PORT", "80"))
    print(f"Status page on port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
