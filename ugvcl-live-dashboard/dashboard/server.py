import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .grafana import GrafanaError

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def make_handler(service, checker):
    class Handler(BaseHTTPRequestHandler):
        server_version = "UGVCLDash/1.0"

        def log_message(self, fmt, *args):
            if "/api/" not in (self.path or ""):
                return  # keep console quiet; API errors are logged below

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel):
            rel = rel.lstrip("/") or "index.html"
            full = os.path.realpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                self.send_error(404)
                return
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".js"):
                ctype = "application/javascript"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            try:
                if u.path in ("/api/snapshot", "/api/view"):
                    day = (q.get("date") or [""])[0] or None
                    try:
                        n = int((q.get("days") or ["7"])[0])
                    except ValueError:
                        n = 7
                    return self._json(service.view(day, n, (q.get("refresh") or ["0"])[0] == "1"))
                if u.path == "/api/attr-names":
                    return self._json({"names": service.attr_names()})
                if u.path == "/api/attr":
                    name = (q.get("name") or [""])[0]
                    return self._json(service.attr_distribution(name))
                if u.path == "/api/comm-job":
                    j = checker.get((q.get("id") or [""])[0])
                    if j is None:
                        return self._json({"error": "unknown or expired check"}, 404)
                    return self._json(j)
                if u.path == "/api/health":
                    return self._json({"ok": True})
                if u.path.startswith("/api/"):
                    return self._json({"error": "not found"}, 404)
                return self._static(u.path)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except GrafanaError as e:
                return self._json({"error": str(e)}, 502)
            except Exception as e:
                return self._json({"error": "internal error: %s" % e}, 500)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            try:
                if u.path != "/api/comm-check":
                    return self._json({"error": "not found"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    return self._json({"error": "list too large"}, 413)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                return self._json({"job_id": checker.start(body.get("serials", ""))})
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": "internal error: %s" % e}, 500)

    return Handler


def serve(service, checker, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(service, checker))
    return httpd
