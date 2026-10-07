#!/usr/bin/env python3
"""Standalone Stock101: same engine, separate local data and credentials."""
import argparse
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from local_http import LocalHandler, serve

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Stock101 community research and paper trading")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--standby", action="store_true", help="Disable background workers for testing")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    if args.standby:
        os.environ["AIRI_STANDBY"] = "1"
    os.chdir(ROOT)
    import llm_client
    from stock101 import http, service
    app = SimpleNamespace(ROOT=ROOT, DATA=args.data_dir.resolve(), CHAT_LOCK=threading.Lock(),
                          llm_client=llm_client, settings=lambda: {"model": "qwen3:4b"})
    app.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)

    class Handler(LocalHandler):
        def route_get(self, path):
            if path == "/":
                self.send_response(302)
                self.headers_common()
                self.send_header("Location", "/stock101")
                self.end_headers()
                return True
            return http.handle_get(app, self)

        def route_post(self, path, body):
            if path != "/api/stock101":
                raise ValueError("Unknown API endpoint")
            return service.action(app, body)

    service.store(app)
    serve(Handler, args.port)


if __name__ == "__main__":
    main()
