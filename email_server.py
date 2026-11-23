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
# Orders that were really sold: everything except cancelled / fully refunded ones.
SOLD = "status NOT IN ('CANCELLED', 'CANCELLED_AFTER_IMPORT') AND order_date IS NOT NULL"


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


def missing(limit):
    """The highest-spending customers who still have no email stored (always N of them)."""
    init_tables()
    groups, person_has_email = {}, set()
    with closing(sqlite3.connect(sync.DB_PATH)) as conn:
        have_buyer = {r[0] for r in conn.execute("SELECT buyer_id FROM buyer_emails WHERE email <> ''")}
        have_receipt = {r[0] for r in conn.execute("SELECT receipt_id FROM etsy_emails WHERE email <> ''")}
        rows = conn.execute(
            "SELECT etsy_receipt_id, buyer_user_id, buyer_name, postcode, grand_total, order_date FROM synced_orders "
            f"WHERE {SOLD} AND buyer_name IS NOT NULL").fetchall()
    for receipt, uid, name, postcode, total, order_date in rows:
        uid = str(uid) if uid else ""
        person = (name.strip().lower(), (postcode or "").replace(" ", "").upper())
        key = uid or person
        g = groups.setdefault(key, {"person": person, "n": 0, "spent": 0.0, "name": name.strip(), "uid": uid, "last": "", "email": False})
        g["n"] += 1
        g["spent"] += total or 0
        g["last"] = max(g["last"], (order_date or "")[:10])
        if uid in have_buyer or str(receipt) in have_receipt:
            g["email"] = True
            person_has_email.add(person)  # same name and postcode on another Etsy account counts too
    ranked = sorted((g for g in groups.values() if g["name"].lower() != "me"), key=lambda g: -g["spent"])
    for g in ranked:
        g["email"] = g["email"] or g["person"] in person_has_email
    done = sum(1 for g in ranked if g["email"])
    todo = [g for g in ranked if not g["email"]][:limit]
    print(f"{done} customers already have an email. Next {len(todo)} by spend without one:")
    print("Search each name in Etsy Orders (set to All) with the extension on and email_server.py running:")
    print(f"{'Spent':>10}{'Orders':>7}  {'Last order':<11}{'Etsy buyer id':<14}Name")
    for g in todo:
        print(f"{'£{:,.2f}'.format(g['spent']):>10}{g['n']:>7}  {g['last']:<11}{g['uid'] or '-':<14}{g['name']}")


def main():
    parser = argparse.ArgumentParser(description="Receive buyer emails from the Etsy capture extension.")
    parser.add_argument("--stats", action="store_true", help="Show how many emails are stored and exit.")
    parser.add_argument("--missing", nargs="?", type=int, const=25, metavar="N",
                        help="List the next N highest-spending customers (default 25) who still have no email, then exit.")
    args = parser.parse_args()
    if args.stats:
        return stats()
    if args.missing:
        return missing(args.missing)
    init_tables()
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT} (this PC only). Open your Etsy Orders pages; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
