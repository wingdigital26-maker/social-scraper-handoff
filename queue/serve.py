"""Local review-queue server. Runs on your machine, talks to Supabase with the
service key (which therefore never touches a browser or public host).

    python queue/serve.py            # opens http://localhost:8765
    ENV_FILE=path/to/.env python queue/serve.py

Approve saves the (possibly edited) draft and marks status=approved.
Skip marks status=rejected. Nothing is ever auto-sent — you copy the reply
and post it yourself, then click Sent.
"""
import json
import os
import pathlib
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8765


def load_env():
    vals = dict(os.environ)
    for p in (os.environ.get("ENV_FILE"), HERE.parent / ".env",
              r"C:\Users\wjack\ghl-cli\.env"):
        if not p:
            continue
        p = pathlib.Path(p)
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break
    return vals


ENV = load_env()
SB_URL, SB_KEY = ENV.get("SUPABASE_URL"), ENV.get("SUPABASE_SERVICE_KEY")
HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
           "Content-Type": "application/json", "Prefer": "return=minimal"}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/queue"):
            r = requests.get(
                f"{SB_URL}/rest/v1/candidates",
                headers=HEADERS,
                params={"status": "eq.new", "order": "score.desc.nullslast", "limit": "50",
                        "select": "id,source,title,place_name,category,score,intent,upvotes,url,draft_reply,embeds"},
                timeout=30)
            self._send(r.status_code, r.json() if r.ok else {"error": r.text[:200]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/action"):
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cid, action = body.get("id"), body.get("action")
        patch = {"approve": {"status": "approved", "draft_reply": body.get("draft_reply")},
                 "reject": {"status": "rejected"},
                 "sent": {"status": "sent"}}.get(action)
        if not cid or patch is None:
            return self._send(400, {"error": "bad request"})
        r = requests.patch(f"{SB_URL}/rest/v1/candidates", headers=HEADERS,
                           params={"id": f"eq.{cid}"}, json=patch, timeout=30)
        self._send(204 if r.ok else 500, {} if r.ok else {"error": r.text[:200]})

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    if not SB_URL or not SB_KEY:
        raise SystemExit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY (set ENV_FILE?)")
    print(f"Review queue -> http://localhost:{PORT}")
    webbrowser.open(f"http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
