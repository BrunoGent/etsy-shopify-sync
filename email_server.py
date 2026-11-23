"""Local listener for the Etsy email-capture browser extension.

    python email_server.py            # listens on 127.0.0.1:8765 until you close it
    python email_server.py --stats    # show how many emails are stored, then exit

The extension (folder email_capture/) reads buyer emails that Etsy already shows on the Orders
page you have open, and posts them here. Nothing is sent to Etsy. The listener only accepts
connections from this PC, only from the extension (not from web pages), and writes to orders.db:
  - etsy_emails (receipt_id -> email): the table sync.py already reads when it imports an order
  - buyer_emails (buyer_id -> email, name): one row per Etsy buyer, for linking customers later
Emails are for fulfilment and customer service. Do not use them for marketing without consent.
"""
import argparse
import json
import re
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import sync

HOST, PORT = "127.0.0.1", 8765
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_BODY = 1_000_000


def init_tables():
    sync.init_db()
    with closing(sqlite3.connect(sync.DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS buyer_emails (
                buyer_id TEXT PRIMARY KEY,
                email TEXT,
                buyer_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        conn.commit()


def clean_id(value):
    value = str(value or "").strip()
    return value if value.isdigit() else ""


def store(items):
    """Save a list of {order_id, buyer_id, email, name}. Returns how many rows were valid."""
    saved = 0
    with closing(sqlite3.connect(sync.DB_PATH)) as conn:
        for item in items:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            if not EMAIL_RE.match(email) or len(email) > 254:
                continue
            order_id, buyer_id = clean_id(item.get("order_id")), clean_id(item.get("buyer_id"))
            name = str(item.get("name") or "").strip()[:200]
            if order_id:
                conn.execute(
                    "INSERT INTO etsy_emails (receipt_id, email) VALUES (?, ?) "
                    "ON CONFLICT(receipt_id) DO UPDATE SET email = excluded.email, updated_at = CURRENT_TIMESTAMP",
                    (order_id, email))
                saved += 1
            if buyer_id:
                conn.execute(
                    "INSERT INTO buyer_emails (buyer_id, email, buyer_name) VALUES (?, ?, ?) "
                    "ON CONFLICT(buyer_id) DO UPDATE SET email = excluded.email, "
                    "buyer_name = COALESCE(NULLIF(excluded.buyer_name, ''), buyer_name), updated_at = CURRENT_TIMESTAMP",
                    (buyer_id, email, name))
                saved += 1 if not order_id else 0
        conn.commit()
    return saved


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body=b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Refuse anything that comes from a web page; the extension's background script has a
        # chrome-extension:// origin (or none).
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            return self._reply(403)
        if self.path != "/capture":
            return self._reply(404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                return self._reply(413)
            payload = json.loads(self.rfile.read(length))
            items = payload.get("orders") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                return self._reply(400)
        except (ValueError, TypeError):
            return self._reply(400)
        saved = store(items)
        print(f"Captured {saved} email(s) from {len(items)} order(s).")
        self._reply(200, json.dumps({"saved": saved}).encode())

    def do_GET(self):
        self._reply(200, b'{"ok": true}') if self.path == "/ping" else self._reply(404)

    def log_message(self, *args):
        pass


def stats():
    init_tables()
    with closing(sqlite3.connect(sync.DB_PATH)) as conn:
        orders = conn.execute("SELECT COUNT(*) FROM etsy_emails WHERE email <> ''").fetchone()[0]
        buyers = conn.execute("SELECT COUNT(*) FROM buyer_emails").fetchone()[0]
        total = conn.execute("SELECT COUNT(DISTINCT buyer_user_id) FROM synced_orders WHERE buyer_user_id IS NOT NULL").fetchone()[0]
    print(f"Orders with an email: {orders:,}; buyers with an email: {buyers:,} of about {total:,} known buyers.")


def main():
    parser = argparse.ArgumentParser(description="Receive buyer emails from the Etsy capture extension.")
    parser.add_argument("--stats", action="store_true", help="Show how many emails are stored and exit.")
    args = parser.parse_args()
    if args.stats:
        return stats()
    init_tables()
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT} (this PC only). Open your Etsy Orders pages; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
