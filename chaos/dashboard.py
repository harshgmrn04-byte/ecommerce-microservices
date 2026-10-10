"""Serve the saved evidence dashboard on loopback only."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from safety.configuration import ROOT


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        filename = self.path.split("?", 1)[0].lstrip("/") or "report.html"
        allowed = {"report.html": "text/html", "report.json": "application/json",
                   "plan.json": "application/json", "safety-acceptance.json": "application/json"}
        if filename not in allowed:
            self.send_error(404)
            return
        path = ROOT / "chaos/results" / filename
        if not path.exists():
            self.send_error(404, "Generate the report with python -m chaos.report")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", allowed[filename] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8070)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Resilience dashboard: http://localhost:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
