"""Loopback-only JSON host shared by the standalone community apps."""
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "AIRICommunity/0.1"
    max_body = 3 * 1024 * 1024

    def log_message(self, *_):
        # Do not write prompts, images, identifiers or credentials to HTTP logs.
        pass

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def boundary(self, authenticated=False):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if (host not in allowed or
                (origin is not None and origin != "http://" + host) or
                self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.send_json({"error": "Only same-origin localhost requests are allowed."}, 403)
            return False
        if authenticated and not hmac.compare_digest(
                self.headers.get("X-AIRI-Token", ""), self.server.session_token):
            self.send_json({"error": "Reload this local page to start a session."}, 401)
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.boundary(path.startswith("/api/") and path != "/api/bootstrap"):
            return
        if path == "/api/bootstrap":
            return self.send_json({"token": self.server.session_token})
        try:
            if not self.route_get(path):
                self.send_json({"error": "Not found"}, 404)
        except (OSError, ValueError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception:
            self.send_json({"error": "The local service could not complete this request."}, 503)

    def do_POST(self):
        if not self.boundary(True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= self.max_body:
                return self.send_json({"error": "Request body is empty or too large."}, 413)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send_json({"error": "Expected application/json"}, 415)
            self.connection.settimeout(15)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            self.send_json(self.route_post(urlsplit(self.path).path, body))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception:
            self.send_json({"error": "The local service could not complete this request."}, 503)


def serve(handler, port):
    server = LocalServer(("127.0.0.1", port), handler)
    server.session_token = secrets.token_urlsafe(32)
    print(f"Open http://127.0.0.1:{port} · Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
