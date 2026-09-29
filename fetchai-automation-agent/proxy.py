"""Ingress proxy: serves the built Vue panel and forwards /rest/* POSTs to the
agent's REST API running on AGENT_PORT.

Only the Supervisor may talk to it (2.24.2). The proxy has to listen on 0.0.0.0
— the Supervisor connects from its own container for ingress — and it checks no
credentials: Home Assistant authenticates the user before ingress forwards
anything. Every other container on the hassio network (other add-ons, the host,
SSH) could reach 172.30.33.1:8098 and, through it, every agent endpoint with no
login. So a request from anywhere but ALLOWED_CLIENTS is refused before it is
read; Home Assistant's add-on docs recommend exactly this for ingress. The
agent itself listens on 127.0.0.1 (core/uagent.py), so this proxy is the only
way in, and the watchdog's /health comes through here too."""
import http.server
import urllib.request
import urllib.error
import re
import sys
from datetime import datetime
from socketserver import ThreadingMixIn


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

AGENT_PORT = 8099
PROXY_PORT = 8098
PANEL_DIR = "/panel/dist"

# The Supervisor's fixed address on the hassio network, from which both ingress
# and the watchdog connect; loopback for a person debugging inside the
# container, who can reach the agent on 127.0.0.1 anyway.
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1"}

# Below the Supervisor's 10 s watchdog timeout, so a stalled agent loop is
# answered as a 503 rather than left for the Supervisor to time out on.
HEALTH_TIMEOUT_S = 8


def client_allowed(address) -> bool:
    """Whether a request from `address` (the socket peer's IP) may be served.
    The peer, never a forwarded header: nothing a client sends can change it."""
    return address in ALLOWED_CLIENTS


def strip_ingress_prefix(path):
    """Strip HA ingress prefix if present: /api/hassio_ingress/<token>/... -> /..."""
    match = re.match(r"^/api/hassio_ingress/[^/]+(.*)$", path)
    if match:
        stripped = match.group(1)
        return stripped if stripped else "/"
    return path


class Handler(http.server.BaseHTTPRequestHandler):
    def _refused(self) -> bool:
        """Answers 403 and returns True unless the peer is allowed."""
        if client_allowed(self.client_address[0]):
            return False
        print(f"[{_ts()}] [proxy] refused {self.command} {self.path} from "
              f"{self.client_address[0]}", file=sys.stderr, flush=True)
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _health(self):
        """The watchdog's check, answered by the agent's own loop: a stalled
        loop times this out and the Supervisor sees a 503."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{AGENT_PORT}/health",
                                        timeout=HEALTH_TIMEOUT_S) as resp:
                status, data = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        except Exception as e:
            status, data = 503, str(e).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self._refused():
            return
        path = strip_ingress_prefix(self.path)
        if path == "/health":
            self._health()
            return
        path = "/index.html" if path in ("", "/") else path
        try:
            with open(PANEL_DIR + path, "rb") as f:
                data = f.read()
            self.send_response(200)
            ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
            ctype = {
                'html': 'text/html',
                'js':   'application/javascript',
                'css':  'text/css',
                'png':  'image/png',
                'jpg':  'image/jpeg',
                'jpeg': 'image/jpeg',
                'svg':  'image/svg+xml',
                'ico':  'image/x-icon',
                'woff': 'font/woff',
                'woff2':'font/woff2',
                'ttf':  'font/ttf',
                'map':  'application/json',
            }.get(ext, 'application/octet-stream')
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        if self._refused():
            return
        path = strip_ingress_prefix(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        target = f"http://127.0.0.1:{AGENT_PORT}{path}"
        print(f"[{_ts()}] [proxy] POST {self.path} -> {target} body={body[:200]}", file=sys.stderr, flush=True)
        req = urllib.request.Request(
            target,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
            print(f"[{_ts()}] [proxy] agent responded 200 len={len(data)}", file=sys.stderr, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            print(f"[{_ts()}] [proxy] agent responded {e.code}: {data[:200]}", file=sys.stderr, flush=True)
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "text/plain"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            err = str(e).encode()
            print(f"[{_ts()}] [proxy] agent error: {e}", file=sys.stderr, flush=True)
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def log_message(self, format, *args):
        print(f"[{_ts()}] [proxy] {self.address_string()} {format % args}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    ThreadedHTTPServer(("0.0.0.0", PROXY_PORT), Handler).serve_forever()
