"""Tiny demo service used as the workload the agent keeps healthy.

One image, two roles (MODE=api | MODE=frontend). Standard library only so the
image is small and builds offline.

api:
  GET /healthz  process is alive (used by liveness/readiness probes)
  GET /deps     checks redis (used by the agent's synthetic prober)
  GET /items    increments a counter in redis; returns 500 when BUGGY=1
  GET /metrics  Prometheus text format
frontend:
  GET /         calls the api's /items and renders the result
  GET /healthz
  GET /metrics
"""

import json
import os
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

MODE = os.environ.get("MODE", "api")
VERSION = os.environ.get("APP_VERSION", "dev")
BUGGY = os.environ.get("BUGGY", "0") == "1"
PORT = int(os.environ.get("PORT", "8080"))

_counts: dict[str, int] = {}
_lock = threading.Lock()


def log(level: str, msg: str, **kv) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level,
                      "svc": MODE, "msg": msg, **kv}), flush=True)


def count(path: str, code: int) -> None:
    with _lock:
        key = f'path="{path}",code="{code}"'
        _counts[key] = _counts.get(key, 0) + 1


def redis_cmd(url: str, *args: str, timeout: float = 2.0) -> bytes:
    """Minimal RESP client: enough for PING and INCR."""
    u = urlparse(url)
    with socket.create_connection((u.hostname, u.port or 6379), timeout=timeout) as s:
        payload = f"*{len(args)}\r\n" + "".join(f"${len(a)}\r\n{a}\r\n" for a in args)
        s.sendall(payload.encode())
        data = s.recv(1024)
    if data.startswith(b"-"):
        raise RuntimeError(data.decode().strip())
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default access log
        pass

    def send(self, code: int, body: str, ctype: str = "application/json") -> None:
        path = self.path.split("?")[0]
        if path != "/metrics":
            count(path, code)
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/healthz":
                return self.send(200, json.dumps({"ok": True, "version": VERSION}))
            if path == "/metrics":
                return self.send(200, metrics(), "text/plain; version=0.0.4")
            if MODE == "api":
                return self.api(path)
            return self.frontend(path)
        except Exception as e:  # last-resort handler
            log("error", "unhandled exception", error=str(e), trace=traceback.format_exc())
            return self.send(500, json.dumps({"error": str(e)}))

    def api(self, path: str):
        redis_url = os.environ["REDIS_URL"]
        if path == "/deps":
            try:
                redis_cmd(redis_url, "PING")
                return self.send(200, json.dumps({"redis": "ok"}))
            except OSError as e:
                log("error", "redis connection failed", error=str(e), redis=redis_url)
                return self.send(503, json.dumps({"redis": str(e)}))
        if path == "/items":
            if BUGGY:
                try:
                    raise KeyError("item_id")  # the bug shipped in 1.1.0
                except KeyError:
                    log("error", "failed to serialize item", trace=traceback.format_exc())
                    return self.send(500, json.dumps({"error": "internal error"}))
            try:
                n = int(redis_cmd(redis_url, "INCR", "hits")[1:].strip())
            except OSError as e:
                log("error", "redis connection failed", error=str(e), redis=redis_url)
                return self.send(503, json.dumps({"error": "redis unavailable"}))
            return self.send(200, json.dumps({"hits": n, "version": VERSION}))
        return self.send(404, json.dumps({"error": "not found"}))

    def frontend(self, path: str):
        if path != "/":
            return self.send(404, "not found", "text/plain")
        api = os.environ.get("API_URL", "http://api")
        try:
            with urlopen(f"{api}/items", timeout=3) as r:
                data = json.loads(r.read())
            return self.send(200, f"<h1>devon demo</h1><p>hits: {data['hits']} (api {data['version']})</p>",
                             "text/html")
        except (URLError, OSError, ValueError, KeyError) as e:
            log("error", "upstream api call failed", error=str(e), api=api)
            return self.send(502, f"<h1>502</h1><p>api unavailable: {e}</p>", "text/html")


def metrics() -> str:
    lines = ["# TYPE http_requests_total counter"]
    with _lock:
        lines += [f'http_requests_total{{svc="{MODE}",{k}}} {v}' for k, v in _counts.items()]
    lines.append(f'app_info{{svc="{MODE}",version="{VERSION}"}} 1')
    return "\n".join(lines) + "\n"


def main() -> None:
    if MODE == "api" and not os.environ.get("REDIS_URL"):
        log("fatal", "REDIS_URL not set, cannot start")
        sys.exit(1)
    # Ballast makes memory usage realistic so a too-low limit really OOMs.
    ballast_mb = int(os.environ.get("BALLAST_MB", "0"))
    ballast = bytearray(ballast_mb * 1024 * 1024)
    for i in range(0, len(ballast), 4096):
        ballast[i] = 1  # touch pages so they count as RSS
    log("info", "starting", version=VERSION, port=PORT, ballast_mb=ballast_mb, buggy=BUGGY)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
