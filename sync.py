import os
import re
import html
import sys
import time
import sqlite3
import argparse
import traceback
from contextlib import closing
from datetime import date, datetime, timezone
import requests
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "orders.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DISPATCH_NOTE_PATH = os.path.join(BASE_DIR, "dispatch_note.txt")
# 2026-01 is the version Shopify was already serving this app. Override in .env to test a newer one.
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "").strip("'\" ") or "2026-01"
# Optional: a folder that syncs to the cloud (Google Drive, OneDrive, Dropbox...). See README.
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "backups")
# With no BACKUP_DIR set, backups still happen, into a "backups" folder next to this script.
BACKUP_DIR = os.getenv("BACKUP_DIR", "").strip("'\" ") or DEFAULT_BACKUP_DIR
BACKUP_KEEP = 30
LOG_KEEP_DAYS = 90

# --- Credentials ---
ETSY_KEYSTRING = os.getenv("ETSY_KEYSTRING", "").strip("'\"")
ETSY_SHARED_SECRET = os.getenv("ETSY_SHARED_SECRET", "").strip("'\"")
ETSY_ACCESS_TOKEN = os.getenv("ETSY_ACCESS_TOKEN", "").strip("'\"")
ETSY_REFRESH_TOKEN = os.getenv("ETSY_REFRESH_TOKEN", "").strip("'\"")
ETSY_SHOP_ID = os.getenv("ETSY_SHOP_ID", "").strip("'\"")
try:
    ETSY_TOKEN_EXPIRES_AT = int(os.getenv("ETSY_TOKEN_EXPIRES_AT", "0").strip("'\"") or 0)
except ValueError:
    ETSY_TOKEN_EXPIRES_AT = 0

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "").strip("'\"")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip("'\"")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip("'\"")

cached_shopify_token = None
cached_shopify_token_expires = 0

HOME_COUNTRY = "GB"
EXPRESS_KEYWORDS = ("24", "express", "first", "1st", "next day", "priority")
CANCELLED_TAG = "Etsy Cancelled"

# Order detail columns on synced_orders, filled by save_order_details() for new orders
# (during import) and for past orders (backfill_orders.py). They are a record of the order
# as Etsy described it. The only decision made from them is the repeat-customer flag
# (previous_order_count: buyer_user_id, or name + postcode when that is missing).
ORDER_DETAIL_COLUMNS = {
    "order_date": "TEXT",          # purchase time, UTC, YYYY-MM-DD HH:MM:SS
    "etsy_status": "TEXT",         # Etsy's own status: Paid, Completed, Canceled, ...
    "buyer_name": "TEXT",
    "buyer_user_id": "TEXT",       # Etsy's id for the buyer; the key for repeat-customer checks
    "buyer_email": "TEXT",
    "address_line1": "TEXT",
    "address_line2": "TEXT",
    "city": "TEXT",
    "county": "TEXT",
    "postcode": "TEXT",
    "country": "TEXT",
    "items": "TEXT",               # one line per item: "2x Title [SKU] (Option: value)"
    "item_count": "INTEGER",
    "items_total": "REAL",
    "shipping_cost": "REAL",
    "discount": "REAL",
    "tax": "REAL",
    "grand_total": "REAL",
    "currency": "TEXT",
    "shipping_upgrade": "TEXT",
    "is_gift": "INTEGER",
    "gift_message": "TEXT",
    "buyer_note": "TEXT",
    "shipped_date": "TEXT",        # when Etsy was told it was dispatched, UTC
    "etsy_tracking": "TEXT",       # tracking code(s) Etsy holds for the order
    "details_updated_at": "TIMESTAMP",
}


# =====================================================================
# DATABASE MANAGEMENT (SQLite)
# =====================================================================

def _db_execute(sql, params=(), fetch=False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.execute(sql, params)
        row = cursor.fetchone() if fetch else None
        conn.commit()
        return row


def _db_query(sql, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(sql, params).fetchall()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS synced_orders (
                etsy_receipt_id TEXT PRIMARY KEY,
                shopify_order_id TEXT,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                tracking_number TEXT,
                tracking_pushed INTEGER DEFAULT 0,
                expected_ship_date TEXT,
                tracking_pushed_at TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS etsy_emails (
                receipt_id TEXT PRIMARY KEY,
                email TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("PRAGMA table_info(synced_orders)")
        columns = [col[1] for col in cursor.fetchall()]
        migrations = {
            "tracking_number": "TEXT",
            "tracking_pushed": "INTEGER DEFAULT 0",
            "expected_ship_date": "TEXT",
            "tracking_pushed_at": "TIMESTAMP",
        }
        migrations.update(ORDER_DETAIL_COLUMNS)
        for name, ddl in migrations.items():
            if name not in columns:
                cursor.execute(f"ALTER TABLE synced_orders ADD COLUMN {name} {ddl}")
        conn.commit()


def get_cached_email(receipt_id):
    """Checks local SQLite etsy_emails table if populated by the browser extension."""
    row = _db_execute("SELECT email FROM etsy_emails WHERE receipt_id = ?", (str(receipt_id),), fetch=True)
    return row[0].strip() if row and row[0] else ""


def get_synced_row(receipt_id):
    """Returns {shopify_order_id, status, expected_ship_date} for a receipt, or None."""
    row = _db_execute(
        "SELECT shopify_order_id, status, expected_ship_date FROM synced_orders WHERE etsy_receipt_id = ?",
        (str(receipt_id),), fetch=True)
    if not row:
        return None
    return {"shopify_order_id": row[0], "status": row[1], "expected_ship_date": row[2]}


def is_order_synced(receipt_id):
    return get_synced_row(receipt_id) is not None


def record_synced_order(receipt_id, shopify_order_id, status="SUCCESS", expected_ship_date=None):
    _db_execute("""
        INSERT INTO synced_orders (etsy_receipt_id, shopify_order_id, status, expected_ship_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(etsy_receipt_id) DO UPDATE SET
            shopify_order_id = excluded.shopify_order_id,
            status = excluded.status,
            expected_ship_date = COALESCE(excluded.expected_ship_date, synced_orders.expected_ship_date)
    """, (str(receipt_id), str(shopify_order_id), status, expected_ship_date))


def set_order_status(receipt_id, status):
    _db_execute("UPDATE synced_orders SET status = ? WHERE etsy_receipt_id = ?", (status, str(receipt_id)))


def set_expected_ship_date(receipt_id, expected_ship_date):
    _db_execute("UPDATE synced_orders SET expected_ship_date = ? WHERE etsy_receipt_id = ?",
                (expected_ship_date, str(receipt_id)))


def get_tracking_state(receipt_id):
    """Returns which tracking codes have already been pushed to Etsy for a receipt.

    Rows pushed before multi-parcel support have no tracking_pushed_at; they are
    treated as fully done so old orders are never re-notified.
    """
    row = _db_execute(
        "SELECT tracking_pushed, tracking_number, tracking_pushed_at FROM synced_orders WHERE etsy_receipt_id = ?",
        (str(receipt_id),), fetch=True)
    if not row or row[0] != 1:
        return {"legacy_done": False, "codes": set()}
    codes = {c.strip() for c in (row[1] or "").split(",") if c.strip()}
    return {"legacy_done": row[2] is None, "codes": codes}


def is_tracking_pushed(receipt_id):
    state = get_tracking_state(receipt_id)
    return state["legacy_done"] or bool(state["codes"])


def record_tracking_pushed(receipt_id, tracking_number, shopify_order_id=""):
    """Marks one tracking code as pushed. Further codes for the same receipt are appended."""
    _db_execute("""
        INSERT INTO synced_orders
            (etsy_receipt_id, shopify_order_id, status, tracking_number, tracking_pushed, tracking_pushed_at)
        VALUES (?, ?, 'SHIPPED', ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(etsy_receipt_id) DO UPDATE SET
            tracking_number = CASE
                WHEN synced_orders.tracking_pushed = 1 AND COALESCE(synced_orders.tracking_number, '') <> ''
                    THEN synced_orders.tracking_number || ',' || excluded.tracking_number
                ELSE excluded.tracking_number
            END,
            tracking_pushed = 1,
            tracking_pushed_at = CURRENT_TIMESTAMP,
            status = 'SHIPPED'
    """, (str(receipt_id), str(shopify_order_id), str(tracking_number)))


# =====================================================================
# AUTHENTICATION & HTTP HELPERS
# =====================================================================

def refresh_etsy_tokens(attempts=3):
    global ETSY_ACCESS_TOKEN, ETSY_REFRESH_TOKEN, ETSY_TOKEN_EXPIRES_AT
    print("[Etsy] Refreshing access token...")
    url = "https://api.etsy.com/v3/public/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": ETSY_KEYSTRING,
        "refresh_token": ETSY_REFRESH_TOKEN
    }
    resp = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, data=payload, timeout=30)
            break
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"[Etsy] Token refresh attempt {attempt}/{attempts} failed: {e}")
            if attempt == attempts:
                raise Exception(f"Failed to refresh Etsy token after {attempts} attempts: {e}")
            time.sleep(3)

    if resp.status_code != 200:
        raise Exception(f"Failed to refresh Etsy token: {resp.text}")

    data = resp.json()
    ETSY_ACCESS_TOKEN = data["access_token"]
    ETSY_REFRESH_TOKEN = data["refresh_token"]
    ETSY_TOKEN_EXPIRES_AT = int(time.time()) + int(data.get("expires_in", 3600))

    # Refresh token first: it is the one that cannot be recovered if the run dies here.
    set_key(ENV_PATH, "ETSY_REFRESH_TOKEN", ETSY_REFRESH_TOKEN)
    set_key(ENV_PATH, "ETSY_ACCESS_TOKEN", ETSY_ACCESS_TOKEN)
    set_key(ENV_PATH, "ETSY_TOKEN_EXPIRES_AT", str(ETSY_TOKEN_EXPIRES_AT))
    print("[Etsy] Token refreshed and saved to .env")


def ensure_etsy_token():
    """Refreshes the Etsy token only when it is missing or within 5 minutes of expiry."""
    if ETSY_ACCESS_TOKEN and time.time() < ETSY_TOKEN_EXPIRES_AT - 300:
        return
    refresh_etsy_tokens()


def get_etsy_headers():
    return {
        "x-api-key": f"{ETSY_KEYSTRING}:{ETSY_SHARED_SECRET}",
        "Authorization": f"Bearer {ETSY_ACCESS_TOKEN}"
    }


def etsy_request(method, url, **kwargs):
    """Etsy API call that refreshes the token once and retries on a 401."""
    kwargs.setdefault("timeout", 20)
    resp = requests.request(method, url, headers=get_etsy_headers(), **kwargs)
    if resp.status_code == 401:
        refresh_etsy_tokens()
        resp = requests.request(method, url, headers=get_etsy_headers(), **kwargs)
    return resp


def get_shopify_token():
    global cached_shopify_token, cached_shopify_token_expires
    if cached_shopify_token and time.time() < cached_shopify_token_expires:
        return cached_shopify_token

    print("[Shopify] Requesting Admin API access token via Client Credentials...")
    url = f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token"
    payload = {
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    headers = {"Content-Type": "application/json"}

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to obtain Shopify access token ({resp.status_code}): {resp.text}")

    data = resp.json()
    cached_shopify_token = data["access_token"]
    cached_shopify_token_expires = time.time() + data.get("expires_in", 86399) - 60
    print("[Shopify] Access token obtained successfully.")
    return cached_shopify_token


def get_shopify_headers():
    return {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }


def shopify_request(method, url, **kwargs):
    """Shopify API call that waits and retries when rate limited (429).

    Timeouts are deliberately NOT retried: a timed-out order creation may have succeeded.
    """
    kwargs.setdefault("timeout", 20)
    resp = None
    for _ in range(4):
        resp = requests.request(method, url, headers=get_shopify_headers(), **kwargs)
        if resp.status_code != 429:
            return resp
        try:
            wait = float(resp.headers.get("Retry-After", 2))
        except ValueError:
            wait = 2.0
        time.sleep(min(wait, 10))
    return resp


def shopify_get_all(url, params, key, max_items):
    """Follows Shopify's Link-header pagination. Raises on any non-200 page."""
    items = []
    next_url, next_params = url, params
    while next_url and len(items) < max_items:
        resp = shopify_request("GET", next_url, params=next_params)
        if resp.status_code != 200:
            raise Exception(f"Shopify GET {key} failed ({resp.status_code}): {resp.text}")
        items.extend(resp.json().get(key, []))
        next_url = resp.links.get("next", {}).get("url")
        next_params = None  # the next URL already carries the query
    return items[:max_items]


# =====================================================================
# SHOPIFY SKU LOOKUP & DUPLICATE GUARD
# =====================================================================

def shopify_graphql(query, variables=None):
    """Runs a Shopify GraphQL query and returns its data. Raises on any error."""
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    resp = shopify_request("POST", url, json={"query": query, "variables": variables or {}})
    if resp.status_code != 200:
        raise Exception(f"Shopify GraphQL failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    if body.get("errors"):
        raise Exception(f"Shopify GraphQL error: {body['errors']}")
    return body.get("data") or {}


def build_shopify_sku_map():
    """Maps every Shopify variant SKU to its variant ID. Raises if Shopify cannot be read."""
    query = """
    query variants($cursor: String) {
      productVariants(first: 250, after: $cursor) {
        nodes { sku legacyResourceId }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    sku_map = {}
    cursor = None
    for _ in range(200):  # up to 50,000 variants
        page = shopify_graphql(query, {"cursor": cursor})["productVariants"]
        for node in page["nodes"]:
            sku = (node.get("sku") or "").strip()
            if sku:
                sku_map[sku] = int(node["legacyResourceId"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return sku_map


def extract_etsy_receipt_id(order):
    """Extracts the Etsy Receipt ID from a Shopify order's name, note, or tags."""
    name = str(order.get("name", "")).replace("#", "").strip()
    if name.isdigit() and 10 <= len(name) <= 12:
        return name
    note = str(order.get("note") or "")
    match = re.search(r"Receipt ID:\s*(\d{9,12})\b", note)
    if match:
        return match.group(1)
    # Legacy fallback (orders imported by older tools): a bare 10-digit number.
    tags = str(order.get("tags") or "")
    match = re.search(r"\b(\d{10})\b", f"{note} {tags}")
    if match:
        return match.group(1)
    return None


def build_existing_shopify_receipt_map(max_orders=750):
    """Maps {etsy_receipt_id: shopify_order_id} for recent Shopify orders. Raises on failure,
    because an empty map would disable the duplicate-order guard."""
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "order": "created_at desc",
        "limit": 250,
        "fields": "id,name,note,tags"
    }
    existing = {}
    for order in shopify_get_all(url, params, "orders", max_items=max_orders):
        rid = extract_etsy_receipt_id(order)
        if rid:
            existing.setdefault(rid, order.get("id"))
    return existing


def add_shopify_order_tag(shopify_order_id, tag):
    """Adds a tag to a Shopify order, keeping its existing tags. Returns True on success."""
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders/{shopify_order_id}.json"
    resp = shopify_request("GET", url, params={"fields": "id,tags"})
    if resp.status_code != 200:
        print(f"  [!] Could not read Shopify order {shopify_order_id} to tag it ({resp.status_code}).")
        return False
    tags = [t.strip() for t in (resp.json().get("order", {}).get("tags") or "").split(",") if t.strip()]
    if tag in tags:
        return True
    tags.append(tag)
    resp = shopify_request("PUT", url, json={"order": {"id": shopify_order_id, "tags": ", ".join(tags)}})
    if resp.status_code != 200:
        print(f"  [!] Could not tag Shopify order {shopify_order_id} ({resp.status_code}): {resp.text}")
        return False
    return True


# =====================================================================
# STEP 1: ETSY -> SHOPIFY ORDER IMPORT & DEADLINES
# =====================================================================

def clean_receipt(value):
    """Etsy sends some text HTML-escaped (Queen&#39;s Road, Tom &amp; Jerry). Decodes every
    string in a receipt so addresses, names, notes and titles arrive as plain text."""
    if isinstance(value, str):
        return html.unescape(value)
    if isinstance(value, list):
        return [clean_receipt(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_receipt(v) for k, v in value.items()}
    return value


def fetch_etsy_receipts(max_receipts=500):
    """Fetches all paid, unshipped Etsy receipts (cancelled ones included), 100 per page."""
    url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts"
    receipts = []
    offset = 0
    while len(receipts) < max_receipts:
        page_size = min(100, max_receipts - len(receipts))
        params = {
            "was_paid": "true",
            "was_shipped": "false",
            "limit": page_size,
            "offset": offset
        }
        resp = etsy_request("GET", url, params=params)
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch Etsy orders ({resp.status_code}): {resp.text}")
        results = resp.json().get("results", [])
        receipts.extend(clean_receipt(r) for r in results)
        if len(results) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
    else:
        print(f"  [!] Reached the --limit of {max_receipts} receipts; there may be more on Etsy.")
    return receipts


def is_receipt_cancelled(receipt):
    """True for cancelled or fully refunded receipts. Partially refunded orders still ship."""
    if receipt.get("is_cancelled"):
        return True
    status = str(receipt.get("status", "")).lower()
    if "cancel" in status:
        return True
    return "refund" in status and "partial" not in status


def parse_money(money_obj):
    if not money_obj or not isinstance(money_obj, dict):
        return 0.0
    return money_obj.get("amount", 0) / (money_obj.get("divisor") or 100)


def get_etsy_purchase_iso(receipt):
    """Extracts Etsy's purchase timestamp and returns an ISO 8601 UTC string for Shopify processed_at."""
    created_ts = receipt.get("create_timestamp") or receipt.get("created_timestamp")
    if created_ts:
        return datetime.fromtimestamp(int(created_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def get_etsy_expected_ship_ts(receipt):
    """Etsy's expected_ship_date (unix seconds) from the receipt, or the latest of its transactions."""
    expected_ts = receipt.get("expected_ship_date")
    if not expected_ts:
        ship_dates = [tx.get("expected_ship_date") for tx in receipt.get("transactions", [])
                      if tx.get("expected_ship_date")]
        if ship_dates:
            expected_ts = max(ship_dates)
    return int(expected_ts) if expected_ts else None


def get_etsy_ship_by_iso(receipt):
    """Etsy's expected ship date as ISO 8601 UTC, for Shopify's 'Fulfill by' deadline."""
    expected_ts = get_etsy_expected_ship_ts(receipt)
    if expected_ts:
        return datetime.fromtimestamp(expected_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def get_etsy_ship_by_date(receipt):
    """Etsy's expected ship date as YYYY-MM-DD in this PC's local (UK) time."""
    expected_ts = get_etsy_expected_ship_ts(receipt)
    if expected_ts:
        return datetime.fromtimestamp(expected_ts).strftime("%Y-%m-%d")
    return None


def get_etsy_shipping_choices(receipt):
    """Every shipping option string Etsy attached to the receipt or its line items."""
    choices = []
    for src in [receipt] + list(receipt.get("transactions") or []):
        for key in ("shipping_upgrade", "shipping_method"):
            val = str(src.get(key) or "").strip()
            if val and (key, val) not in choices:
                choices.append((key, val))
    return choices


def detect_shipping(receipt):
    """Works out the Shopify shipping line and tags from Etsy's shipping option.

    Priority/express on ANY line item makes the whole order Tracked 24.
    """
    choices = get_etsy_shipping_choices(receipt)
    values = [val for _, val in choices]
    is_express = any(k in val.lower() for val in values for k in EXPRESS_KEYWORDS)
    upgrade = next((val for key, val in choices if key == "shipping_upgrade"), "")
    country = (receipt.get("country_iso") or HOME_COUNTRY).upper()
    is_international = country != HOME_COUNTRY

    tags = []
    if is_international:
        title = code = values[0] if values else "Royal Mail International Tracked"
        tags.append("International")
        if is_express:
            tags.append("Priority")
    elif is_express:
        title = code = "Royal Mail Tracked 24"
        tags.extend(["Tracked 24", "Priority"])
    else:
        title = code = "Royal Mail Tracked 48"
        if upgrade:
            tags.append(upgrade.replace(",", " ")[:40])

    return {"title": title, "code": code, "tags": tags, "is_express": is_express}


def _utc_str(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def format_items(receipt):
    """One readable line per item: '2x Title [SKU] (Option: value; Personalization: ...)'."""
    lines = []
    for tx in receipt.get("transactions") or []:
        line = f"{tx.get('quantity', 1)}x {tx.get('title') or 'Etsy Item'}"
        sku = (tx.get("sku") or "").strip()
        if sku:
            line += f" [{sku}]"
        options = []
        for var in tx.get("variations") or []:
            name = var.get("formatted_name") or var.get("property_name") or "Option"
            value = var.get("formatted_value") or var.get("value") or ""
            if value:
                options.append(f"{name}: {value}")
        if tx.get("buyer_personalization"):
            options.append(f"Personalization: {tx.get('buyer_personalization')}")
        if options:
            line += f" ({'; '.join(options)})"
        lines.append(line)
    return "\n".join(lines)


def extract_order_details(receipt):
    """Flattens an Etsy receipt into the ORDER_DETAIL_COLUMNS values."""
    receipt_id = str(receipt.get("receipt_id"))
    transactions = receipt.get("transactions") or []
    shipments = receipt.get("shipments") or []
    upgrade = next((val for key, val in get_etsy_shipping_choices(receipt) if key == "shipping_upgrade"), None)
    shipped_ts = [s.get("shipment_notification_timestamp") for s in shipments if s.get("shipment_notification_timestamp")]
    if not shipped_ts:
        shipped_ts = [tx.get("shipped_timestamp") for tx in transactions if tx.get("shipped_timestamp")]
    codes = [str(s.get("tracking_code") or "").strip() for s in shipments]
    email = (receipt.get("buyer_email") or "").strip() or get_cached_email(receipt_id)
    return {
        "order_date": _utc_str(receipt.get("create_timestamp") or receipt.get("created_timestamp")),
        "etsy_status": receipt.get("status"),
        "buyer_name": (receipt.get("name") or "").strip() or None,
        "buyer_user_id": str(receipt["buyer_user_id"]) if receipt.get("buyer_user_id") else None,
        "buyer_email": email or None,
        "address_line1": receipt.get("first_line"),
        "address_line2": receipt.get("second_line"),
        "city": receipt.get("city"),
        "county": receipt.get("state"),
        "postcode": receipt.get("zip"),
        "country": receipt.get("country_iso"),
        "items": format_items(receipt),
        "item_count": sum(int(tx.get("quantity") or 0) for tx in transactions),
        "items_total": round(sum(parse_money(tx.get("price")) * (tx.get("quantity") or 0) for tx in transactions), 2),
        "shipping_cost": round(parse_money(receipt.get("total_shipping_cost")), 2),
        "discount": round(parse_money(receipt.get("discount_amt")), 2),
        "tax": round(parse_money(receipt.get("total_tax_cost")) + parse_money(receipt.get("total_vat_cost")), 2),
        "grand_total": round(parse_money(receipt.get("grandtotal")), 2),
        "currency": (receipt.get("grandtotal") or {}).get("currency_code"),
        "shipping_upgrade": upgrade,
        "is_gift": 1 if receipt.get("is_gift") else 0,
        "gift_message": receipt.get("gift_message") or None,
        "buyer_note": receipt.get("message_from_buyer") or None,
        "shipped_date": _utc_str(min(shipped_ts)) if shipped_ts else None,
        "etsy_tracking": ",".join(c for c in codes if c) or None,
    }


def save_order_details(receipt):
    """Writes the detail columns for a receipt that already has a row in synced_orders.

    Only detail columns are touched: status, Shopify ID and tracking state are left alone.
    A known email is never replaced by a blank one.
    """
    details = extract_order_details(receipt)
    assignments = ", ".join(
        f"{col} = COALESCE(?, {col})" if col == "buyer_email" else f"{col} = ?" for col in details)
    _db_execute(
        f"UPDATE synced_orders SET {assignments}, details_updated_at = CURRENT_TIMESTAMP WHERE etsy_receipt_id = ?",
        tuple(details.values()) + (str(receipt.get("receipt_id")),))


REPEAT_TAG = "Repeat customer"
REPEAT_ORDER_TAG = "Repeat order"   # followed by the order number, e.g. "Repeat order 3"


def _norm_text(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def previous_order_count(receipt, batch=(), when=None):
    """How many EARLIER, non-cancelled Etsy orders the same buyer has.

    The buyer is identified by Etsy's buyer_user_id; if a receipt has none, by name + postcode.
    Counts rows in orders.db (the whole shop history) plus any receipts in `batch` (the orders
    being imported in this same run) that are not in the database yet. Etsy only; other
    channels are not matched. Returns 0 when the order's date is unknown.
    """
    receipt_id = str(receipt.get("receipt_id"))
    when = when or _utc_str(receipt.get("create_timestamp") or receipt.get("created_timestamp"))
    if not when:
        return 0
    uid = str(receipt["buyer_user_id"]) if receipt.get("buyer_user_id") else None
    base = ("SELECT etsy_receipt_id, buyer_name, postcode FROM synced_orders WHERE etsy_receipt_id != ? "
            "AND order_date < ? AND COALESCE(status, '') NOT LIKE 'CANCEL%'")
    if uid:
        rows = _db_query(base + " AND buyer_user_id = ?", (receipt_id, when, uid))
        count = len(rows)
    else:
        name, post = _norm_text(receipt.get("name")), _norm_text(receipt.get("zip"))
        if not name or not post:
            return 0
        rows = _db_query(base, (receipt_id, when))
        count = sum(1 for r in rows if _norm_text(r[1]) == name and _norm_text(r[2]) == post)
    for other in batch:
        oid = str(other.get("receipt_id"))
        if oid == receipt_id or get_synced_row(oid) is not None:
            continue
        other_when = _utc_str(other.get("create_timestamp") or other.get("created_timestamp"))
        if not other_when or other_when >= when:
            continue
        same = (uid and str(other.get("buyer_user_id")) == uid) or (
            not uid and _norm_text(other.get("name")) == _norm_text(receipt.get("name"))
            and _norm_text(other.get("zip")) == _norm_text(receipt.get("zip")))
        if same:
            count += 1
    return count


def repeat_tags(prior):
    """Shopify tags for an order that is the buyer's (prior + 1)th; none for a first order."""
    return [REPEAT_TAG, f"{REPEAT_ORDER_TAG} {prior + 1}"] if prior > 0 else []


def tag_repeat_customers(dry_run=False):
    """One-off: tags the already-imported, not-yet-dispatched orders that are repeat orders."""
    print(f"\n--- Tagging repeat customers on open Shopify orders (Dry Run: {dry_run}) ---")
    rows = _db_query("""
        SELECT etsy_receipt_id, shopify_order_id, buyer_user_id, buyer_name, postcode, order_date
        FROM synced_orders
        WHERE status IN ('SUCCESS', 'EXISTING_IN_SHOPIFY') AND COALESCE(tracking_pushed, 0) = 0
          AND shopify_order_id IS NOT NULL AND shopify_order_id != '' AND order_date IS NOT NULL""")
    tagged = checked = 0
    for rid, shopify_id, uid, name, post, order_date in rows:
        if not uid:
            print(f"  [!] Etsy #{rid} has no buyer id in orders.db; run backfill_orders.py first. Skipped.")
            continue
        checked += 1
        prior = previous_order_count({"receipt_id": rid, "buyer_user_id": uid, "name": name, "zip": post},
                                     when=order_date)
        if prior > 0:
            print(f"  [+] Etsy #{rid}: order {prior + 1} for this buyer -> {', '.join(repeat_tags(prior))}")
            if not dry_run:
                for tag in repeat_tags(prior):
                    add_shopify_order_tag(_numeric_id(shopify_id), tag)
            tagged += 1
    print(f"Checked {checked} open order(s): {tagged} repeat.")


def set_shopify_fulfill_by(shopify_order_id, fulfill_by_iso):
    """Populates the 'Fulfill by' column in Shopify Admin using GraphQL fulfillmentOrdersSetFulfillmentDeadline."""
    if not shopify_order_id or not fulfill_by_iso:
        return

    fo_url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders/{shopify_order_id}/fulfillment_orders.json"
    resp = shopify_request("GET", fo_url)
    if resp.status_code != 200:
        print(f"  [!] Could not fetch fulfillment_orders for Shopify ID {shopify_order_id} ({resp.status_code}): {resp.text}")
        return

    fulfillment_orders = resp.json().get("fulfillment_orders", [])
    open_fo_gids = [
        f"gid://shopify/FulfillmentOrder/{fo['id']}"
        for fo in fulfillment_orders
        if fo.get("status") in ("open", "in_progress")
    ]
    if not open_fo_gids:
        return

    graphql_url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    query = """
    mutation fulfillmentOrdersSetFulfillmentDeadline($fulfillmentDeadline: DateTime!, $fulfillmentOrderIds: [ID!]!) {
      fulfillmentOrdersSetFulfillmentDeadline(
        fulfillmentDeadline: $fulfillmentDeadline
        fulfillmentOrderIds: $fulfillmentOrderIds
      ) {
        success
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "fulfillmentDeadline": fulfill_by_iso,
        "fulfillmentOrderIds": open_fo_gids
    }

    g_resp = shopify_request("POST", graphql_url, json={"query": query, "variables": variables})
    if g_resp.status_code == 200:
        data = g_resp.json()
        result = (data.get("data") or {}).get("fulfillmentOrdersSetFulfillmentDeadline") or {}
        user_errors = result.get("userErrors", [])
        if result.get("success") and not user_errors:
            print(f"  [+] Set 'Fulfill by' deadline to {fulfill_by_iso}")
        else:
            print(f"  [!] GraphQL error setting 'Fulfill by': {user_errors or data.get('errors')}")
    else:
        print(f"  [!] Failed to set 'Fulfill by' deadline ({g_resp.status_code}): {g_resp.text}")


def map_etsy_to_shopify(receipt, sku_map=None, batch=None):
    if sku_map is None:
        sku_map = {}

    receipt_id = str(receipt.get("receipt_id"))
    transactions = receipt.get("transactions", [])
    line_items = []
    items_subtotal = 0.0

    for tx in transactions:
        sku = (tx.get("sku") or "").strip()
        title = tx.get("title") or "Etsy Item"
        quantity = tx.get("quantity", 1)
        price_val = parse_money(tx.get("price"))
        items_subtotal += price_val * quantity

        properties = []
        for var in tx.get("variations", []):
            prop_name = var.get("formatted_name") or var.get("property_name") or "Option"
            prop_val = var.get("formatted_value") or var.get("value") or ""
            if prop_val:
                properties.append({"name": prop_name, "value": prop_val})

        if tx.get("buyer_personalization"):
            properties.append({
                "name": "Personalization",
                "value": tx.get("buyer_personalization")
            })

        item = {
            "title": title,
            "quantity": quantity,
            "price": f"{price_val:.2f}",
            "requires_shipping": True
        }
        if sku:
            item["sku"] = sku
            if sku in sku_map:
                item["variant_id"] = sku_map[sku]

        if properties:
            item["properties"] = properties

        line_items.append(item)

    # 1. Shipping (Tracked 24 vs Tracked 48 vs International)
    shipping = detect_shipping(receipt)

    shipping_cost = parse_money(receipt.get("total_shipping_cost"))
    # Etsy's field is discount_amt; discount_amount is kept as a fallback.
    discount_amt = parse_money(receipt.get("discount_amt") or receipt.get("discount_amount"))
    grandtotal = parse_money(receipt.get("grandtotal")) or max(0.0, items_subtotal + shipping_cost - discount_amt)

    # 2. Build Tags & Extract Dates
    tags = ["Etsy"] + shipping["tags"] + repeat_tags(previous_order_count(receipt, batch or ()))

    ship_by_str = get_etsy_ship_by_date(receipt)
    if ship_by_str:
        tags.append(f"ShipBy:{ship_by_str}")

    processed_at_iso = get_etsy_purchase_iso(receipt)

    # 3. Compile Notes
    # Only things the packer needs to read. The order NAME is the Etsy receipt number (that is how the
    # sync finds the order again) and the discount is on the order itself, so neither goes in the note,
    # which prints on the packing slip. Plain orders therefore have no note at all.
    notes = []

    if receipt.get("is_gift"):
        tags.append("Gift")
        notes.append("🎁 GIFT ORDER (Do not show prices on packing slip)")
        if receipt.get("gift_message"):
            notes.append(f"Gift Message:\n{receipt.get('gift_message')}")

    if receipt.get("message_from_buyer"):
        notes.append(f"Buyer Note:\n{receipt.get('message_from_buyer')}")

    # 4. Customer & Shipping Address
    full_name = (receipt.get("name") or "").strip()
    name_parts = full_name.split()
    first_name = name_parts[0] if name_parts else "Etsy"
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else "."

    shipping_address = {
        "first_name": first_name,
        "last_name": last_name,
        "address1": receipt.get("first_line") or "",
        "address2": receipt.get("second_line") or "",
        "city": receipt.get("city") or "",
        "province": receipt.get("state") or "",
        "zip": receipt.get("zip") or "",
        "country_code": receipt.get("country_iso") or HOME_COUNTRY,
    }

    customer_obj = {
        "first_name": first_name,
        "last_name": last_name,
    }
    buyer_email = (receipt.get("buyer_email") or "").strip() or get_cached_email(receipt_id)
    if buyer_email and "@" in buyer_email:
        customer_obj["email"] = buyer_email
        customer_obj["email_marketing_consent"] = {"state": "not_subscribed"}

    transaction_entry = {
        "kind": "sale",
        "status": "success",
        "amount": f"{grandtotal:.2f}",
        "gateway": "Etsy Payments"
    }
    if processed_at_iso:
        transaction_entry["processed_at"] = processed_at_iso

    order_data = {
        "name": receipt_id,
        "line_items": line_items,
        "customer": customer_obj,
        "shipping_address": shipping_address,
        "billing_address": shipping_address,
        "financial_status": "paid",
        "send_receipt": False,
        "send_fulfillment_receipt": False,
        # The Etsy order is already paid, so never let Shopify stock levels block the import.
        "inventory_behaviour": "decrement_ignoring_policy",
        "shipping_lines": [
            {
                "title": shipping["title"],
                "price": f"{shipping_cost:.2f}",
                "code": shipping["code"]
            }
        ],
        "transactions": [transaction_entry],
        "tags": ", ".join(tags),
    }
    if notes:
        order_data["note"] = "\n\n".join(notes)

    if processed_at_iso:
        order_data["processed_at"] = processed_at_iso

    if buyer_email and "@" in buyer_email:
        order_data["email"] = buyer_email

    if discount_amt > 0:
        order_data["discount_codes"] = [
            {
                "code": "ETSY_DISCOUNT",
                "amount": f"{discount_amt:.2f}",
                "type": "fixed_amount"
            }
        ]

    return {"order": order_data}


def push_to_shopify(order_payload):
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    resp = shopify_request("POST", url, json=order_payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise Exception(f"Shopify order creation failed ({resp.status_code}): {resp.text}")
    return resp.json().get("order", {})


def _numeric_id(value):
    """Returns a stored Shopify order ID if it is a real one (not a placeholder like CANCELLED_ON_ETSY)."""
    value = str(value or "")
    return value if value.isdigit() else None


def handle_cancelled_receipt(receipt, existing_shopify_map, dry_run):
    """Records a cancelled Etsy receipt. Returns True if it had ALREADY been imported into
    Shopify, in which case the Shopify order is tagged so it is not packed by mistake."""
    receipt_id = str(receipt.get("receipt_id"))
    row = get_synced_row(receipt_id)
    status = row["status"] if row else None
    if status in ("CANCELLED", "CANCELLED_AFTER_IMPORT", "SHIPPED"):
        return False

    shopify_id = existing_shopify_map.get(receipt_id) or (_numeric_id(row["shopify_order_id"]) if row else None)
    if shopify_id:
        print(f"  [!!!] Etsy Receipt #{receipt_id} was CANCELLED/REFUNDED on Etsy after it was imported "
              f"(Shopify ID: {shopify_id}). DO NOT SHIP - cancel it in Shopify.")
        if not dry_run and add_shopify_order_tag(shopify_id, CANCELLED_TAG):
            record_synced_order(receipt_id, shopify_id, status="CANCELLED_AFTER_IMPORT")
            print(f"        Tagged the Shopify order '{CANCELLED_TAG}'.")
        return True

    print(f"  [-] Skipping newly detected cancelled Etsy Receipt #{receipt_id}")
    if not dry_run:
        record_synced_order(receipt_id, "CANCELLED_ON_ETSY", status="CANCELLED")
    return False


def is_northern_ireland(receipt):
    """True for a UK delivery address in Northern Ireland: every NI postcode starts with BT
    followed by a digit. The Republic of Ireland (country IE) is not matched."""
    country = (receipt.get("country_iso") or HOME_COUNTRY).strip().upper()
    if country != HOME_COUNTRY:
        return False
    postcode = re.sub(r"\s+", "", str(receipt.get("zip") or "")).upper()
    if re.match(r"BT\d", postcode):
        return True
    return "northern ireland" in str(receipt.get("state") or "").lower()


NI_TAG = "NI - dispatch on Etsy"
NI_HOLD_NOTE = ("Northern Ireland: buy the label and dispatch on Etsy. "
                "This hold is released automatically once Etsy shows it dispatched.")

# Northern Ireland orders still waiting to be dispatched on Etsy, collected during this run
# (imported on hold now, or imported on hold earlier); shown in a banner at the end.
ni_held_orders = []


def hold_shopify_order(shopify_order_id, note=NI_HOLD_NOTE):
    """Puts every open fulfillment order of a Shopify order on hold, so it shows as 'On hold'
    instead of 'Unfulfilled' and no label can be bought for it. Returns True if it is held."""
    fo_url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders/{shopify_order_id}/fulfillment_orders.json"
    open_ids, fos = [], []
    for attempt in range(2):          # Shopify can take a moment to create the fulfillment order
        resp = shopify_request("GET", fo_url)
        if resp.status_code != 200:
            print(f"  [!] Could not read fulfillment orders for Shopify ID {shopify_order_id} ({resp.status_code}): {resp.text}")
            return False
        fos = resp.json().get("fulfillment_orders", [])
        open_ids = [fo["id"] for fo in fos if fo.get("status") in ("open", "in_progress")]
        if open_ids or any(fo.get("status") == "on_hold" for fo in fos):
            break
        time.sleep(2)
    if not open_ids:
        return any(fo.get("status") == "on_hold" for fo in fos)

    mutation = """
    mutation hold($id: ID!, $hold: FulfillmentOrderHoldInput!) {
      fulfillmentOrderHold(id: $id, fulfillmentHold: $hold) {
        fulfillmentHold { id }
        userErrors { field message }
      }
    }"""
    for fo_id in open_ids:
        try:
            data = shopify_graphql(mutation, {
                "id": f"gid://shopify/FulfillmentOrder/{fo_id}",
                "hold": {"reason": "OTHER", "reasonNotes": note, "notifyMerchant": False},
            })
        except Exception as e:
            print(f"  [!] Could not put Shopify ID {shopify_order_id} on hold: {e}")
            return False
        errors = (data.get("fulfillmentOrderHold") or {}).get("userErrors")
        if errors:
            print(f"  [!] Could not put Shopify ID {shopify_order_id} on hold: {errors}")
            return False
    return True


def release_shopify_hold(fulfillment_order_id):
    """Releases the holds on one fulfillment order. Returns True on success."""
    mutation = """
    mutation release($id: ID!) {
      fulfillmentOrderReleaseHold(id: $id) {
        fulfillmentOrder { id status }
        userErrors { field message }
      }
    }"""
    try:
        data = shopify_graphql(mutation, {"id": f"gid://shopify/FulfillmentOrder/{fulfillment_order_id}"})
    except Exception as e:
        print(f"  [!] Could not release the hold on fulfillment order {fulfillment_order_id}: {e}")
        return False
    errors = (data.get("fulfillmentOrderReleaseHold") or {}).get("userErrors")
    if errors:
        print(f"  [!] Could not release the hold on fulfillment order {fulfillment_order_id}: {errors}")
        return False
    return True


def ni_entry(receipt, state):
    ship_by = get_etsy_ship_by_date(receipt)
    return {
        "receipt_id": str(receipt.get("receipt_id")),
        "name": (receipt.get("name") or "").strip() or "unknown buyer",
        "postcode": (receipt.get("zip") or "").strip().upper() or "no postcode",
        "due": datetime.strptime(ship_by, "%Y-%m-%d").strftime("%a %d %b") if ship_by else "no date",
        "state": state,
    }


def print_ni_banner():
    """A hard-to-miss list of Northern Ireland orders that must be dispatched on Etsy."""
    if not ni_held_orders:
        return
    line = "=" * 74
    print(f"\n{line}")
    print(f"  !!!  NORTHERN IRELAND: {len(ni_held_orders)} ORDER(S) TO DISPATCH ON ETSY  !!!")
    print("  Buy the label and mark these as dispatched ON ETSY. Their Shopify orders")
    print("  are ON HOLD (not unfulfilled) and complete themselves once Etsy shows them sent:")
    print()
    for item in ni_held_orders:
        print(f"    Etsy #{item['receipt_id']}  |  {item['name']}  |  {item['postcode']}  |  due {item['due']}")
    failed = [i for i in ni_held_orders if i["state"] == "hold_failed"]
    if failed:
        print()
        print(f"  !!!  {len(failed)} of these could NOT be put on hold in Shopify: do not buy a label there:")
        print("       " + ", ".join(f"#{i['receipt_id']}" for i in failed))
    print()
    print("  (They stay on this list on every import run until they are dispatched on Etsy.)")
    print(line)


def import_etsy_orders(args):
    print(f"\n--- Checking Unshipped Orders: Etsy -> Shopify (Dry Run: {args.dry_run}) ---")
    ni_held_orders.clear()
    # Both lookups raise on failure: importing without them risks duplicates or unlinked items.
    sku_map = build_shopify_sku_map() if args.import_orders else {}
    existing_shopify_map = build_existing_shopify_receipt_map()
    receipts = fetch_etsy_receipts(max_receipts=args.limit)
    orders = [r for r in receipts if not is_receipt_cancelled(r)]
    cancelled = [r for r in receipts if is_receipt_cancelled(r)]
    print(f"Found {len(orders)} active unshipped/paid receipt(s) on Etsy.")

    created_count, backfilled_count, skipped_existing, failed_import = 0, 0, 0, 0
    cancelled_after_import = sum(
        1 for r in cancelled if handle_cancelled_receipt(r, existing_shopify_map, args.dry_run))

    for receipt in orders:
        receipt_id = str(receipt["receipt_id"])
        fulfill_by_iso = get_etsy_ship_by_iso(receipt)
        ship_by_date = get_etsy_ship_by_date(receipt)
        processed_at_iso = get_etsy_purchase_iso(receipt)
        row = get_synced_row(receipt_id)
        # A CANCELLED row for a receipt that is NOT cancelled was a wrongly skipped order
        # (e.g. partially refunded), so it is treated as never imported.
        in_db = row is not None and row["status"] != "CANCELLED"

        # Check if order already exists in Shopify
        if receipt_id in existing_shopify_map or in_db:
            existing_id = existing_shopify_map.get(receipt_id) or (_numeric_id(row["shopify_order_id"]) if row else None)
            if not args.dry_run:
                if existing_id and not in_db:
                    record_synced_order(receipt_id, existing_id, status="EXISTING_IN_SHOPIFY",
                                        expected_ship_date=ship_by_date)
                elif in_db and ship_by_date and row["expected_ship_date"] != ship_by_date:
                    set_expected_ship_date(receipt_id, ship_by_date)
                if in_db or existing_id:
                    save_order_details(receipt)
            if args.exclude_ni and is_northern_ireland(receipt):
                ni_held_orders.append(ni_entry(receipt, "already_in_shopify"))
            if args.backfill_deadlines and existing_id and fulfill_by_iso:
                print(f"  [~] Backfilling 'Fulfill by' ({fulfill_by_iso}) for existing Order #{receipt_id} (Shopify ID: {existing_id})...")
                if not args.dry_run:
                    set_shopify_fulfill_by(existing_id, fulfill_by_iso)
                backfilled_count += 1
            else:
                print(f"  [-] Skipping Receipt #{receipt_id}: already in Shopify/DB.")
                skipped_existing += 1
            continue

        if not args.import_orders:
            continue

        hold_ni = bool(args.exclude_ni and is_northern_ireland(receipt))

        print(f"\nProcessing Etsy Receipt #{receipt_id} for {receipt.get('name')}...")
        if hold_ni:
            print("  [NI] Northern Ireland: importing ON HOLD. Dispatch it on Etsy.")
        if row is not None:
            print(f"  [i] Previously skipped as cancelled, but its Etsy status is '{receipt.get('status')}' - importing.")
        payload = map_etsy_to_shopify(receipt, sku_map=sku_map, batch=orders)
        o_data = payload["order"]
        if hold_ni:
            o_data["tags"] = f"{o_data['tags']}, {NI_TAG}"
        unlinked = [i.get("sku", "no SKU") for i in o_data["line_items"] if "variant_id" not in i]

        if args.dry_run:
            c_name = f"{o_data['customer']['first_name']} {o_data['customer']['last_name']}"
            print(f"  [DRY RUN] Order Name: #{o_data['name']} | Customer: {c_name} | Total: £{o_data['transactions'][0]['amount']}")
            print(f"  [DRY RUN] Purchase Time (processed_at): {processed_at_iso} | Fulfill By: {fulfill_by_iso}")
            print(f"  [DRY RUN] Tags: {o_data['tags']}")
            print(f"  [DRY RUN] Shipping: {o_data['shipping_lines'][0]['title']} (£{o_data['shipping_lines'][0]['price']})")
            print(f"  [DRY RUN] Etsy shipping fields: {get_etsy_shipping_choices(receipt) or 'none sent'}")
            if o_data.get("discount_codes"):
                print(f"  [DRY RUN] Discount: £{o_data['discount_codes'][0]['amount']}")
            if hold_ni:
                print("  [DRY RUN] Northern Ireland: would be imported ON HOLD (tag: " + NI_TAG + ")")
                ni_held_orders.append(ni_entry(receipt, "would_hold"))
            for item in o_data["line_items"]:
                var_info = f" [Variant ID: {item['variant_id']}]" if "variant_id" in item else " [Unlinked SKU]"
                props = f" | Options: {item.get('properties')}" if item.get("properties") else ""
                print(f"    - {item['quantity']}x {item['title']} (SKU: {item.get('sku', 'N/A')}){var_info}{props}")
            continue

        try:
            created_order = push_to_shopify(payload)
        except Exception as e:
            print(f"  [!] Failed to push order #{receipt_id}: {e}")
            failed_import += 1
            continue

        shopify_id = created_order.get("id")
        record_synced_order(receipt_id, shopify_id, status="SUCCESS", expected_ship_date=ship_by_date)
        save_order_details(receipt)
        created_count += 1
        print(f"  [+] Created Shopify Order {created_order.get('name')} (ID: {shopify_id}, Purchased: {processed_at_iso})")
        if unlinked:
            print(f"  [!] {len(unlinked)} line item(s) not linked to a Shopify product: {', '.join(unlinked)}")
        if fulfill_by_iso:
            try:
                set_shopify_fulfill_by(shopify_id, fulfill_by_iso)
            except Exception as e:
                print(f"  [!] Order created, but setting 'Fulfill by' failed ({e}). Run --backfill-deadlines later.")
        if hold_ni:
            if hold_shopify_order(shopify_id):
                print("  [+] Put the order ON HOLD in Shopify (dispatch it on Etsy).")
                ni_held_orders.append(ni_entry(receipt, "held"))
            else:
                print("  [!!!] COULD NOT PUT THIS ORDER ON HOLD. Do not buy a label for it in Shopify; dispatch it on Etsy.")
                ni_held_orders.append(ni_entry(receipt, "hold_failed"))

    print(f"\nOrder Import Summary -> Created: {created_count} | Backfilled: {backfilled_count} | "
          f"Skipped (Existing): {skipped_existing} | Failed: {failed_import} | "
          f"Cancelled after import: {cancelled_after_import}"
          + (f" | Northern Ireland on hold: {len(ni_held_orders)}" if args.exclude_ni else ""))


# =====================================================================
# STEP 2: SHOPIFY -> ETSY TRACKING DISPATCH SYNC
# =====================================================================

def map_carrier_for_etsy(shopify_company):
    company = (shopify_company or "").lower().strip()
    if not company or "royal mail" in company or "click & drop" in company:
        return "royal-mail"
    if "parcelforce" in company:
        return "parcelforce"
    if "evri" in company or "hermes" in company:
        return "hermes-uk"
    if "dpd" in company:
        return "dpd-uk"
    if "dhl" in company:
        return "dhl"
    if "ups" in company:
        return "ups"
    if "fedex" in company:
        return "fedex"
    if "yodel" in company:
        return "yodel"
    return "royal-mail"


def collect_parcels(order):
    """Every distinct (tracking_code, carrier) across an order's non-cancelled fulfillments."""
    parcels = []
    for f in order.get("fulfillments") or []:
        if f.get("status") == "cancelled":
            continue
        numbers = f.get("tracking_numbers") or ([f.get("tracking_number")] if f.get("tracking_number") else [])
        for number in numbers:
            code = str(number or "").strip()
            if code and code not in [p[0] for p in parcels]:
                parcels.append((code, f.get("tracking_company")))
    return parcels


def get_dispatch_due_date(receipt_id, order):
    """The Etsy ship-by date for an order: from orders.db, else the order's ShipBy: tag."""
    row = get_synced_row(receipt_id)
    candidate = row["expected_ship_date"] if row else None
    if not candidate:
        match = re.search(r"ShipBy:(\d{4}-\d{2}-\d{2})", str(order.get("tags") or ""))
        candidate = match.group(1) if match else None
    try:
        return date.fromisoformat(candidate) if candidate else None
    except ValueError:
        return None


def get_etsy_shipment_state(receipt_id):
    """Asks Etsy what it already knows about a receipt before we push tracking.

    Returns {is_shipped, codes, cancelled}, or None if the receipt could not be read.
    This is what stops an order being dispatched (and the buyer emailed) twice when
    orders.db has no record of it, e.g. orders shipped before this tool existed.
    """
    url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts/{receipt_id}"
    try:
        resp = etsy_request("GET", url)
    except requests.RequestException as e:
        print(f"  [!] Could not check Etsy #{receipt_id}: {e}")
        return None
    if resp.status_code != 200:
        print(f"  [!] Could not check Etsy #{receipt_id}: {resp.status_code} -> {resp.text}")
        return None
    data = resp.json()
    codes = {str(s.get("tracking_code") or "").strip() for s in data.get("shipments") or []}
    return {
        "is_shipped": bool(data.get("is_shipped")),
        "codes": codes - {""},
        "cancelled": is_receipt_cancelled(data),
    }


def load_dispatch_note():
    """The optional message sent to the buyer with the dispatch notification.
    Plain text from dispatch_note.txt; no file (or an empty one) means no note."""
    try:
        with open(DISPATCH_NOTE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def is_already_shipped_response(resp):
    text = resp.text.lower()
    return (
        resp.status_code == 409
        or "already shipped" in text
        or (resp.status_code == 400 and "already in use" in text)
    )


def sync_tracking_to_etsy(dry_run=False, limit=500, force_dispatch=False):
    print(f"\n--- Checking Shopify Fulfillments to Dispatch on Etsy (Dry Run: {dry_run}) ---")
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "order": "created_at desc",
        "limit": 250,
        "fields": "id,name,note,tags,fulfillment_status,fulfillments"
    }
    orders = shopify_get_all(url, params, "orders", max_items=limit)
    ensure_etsy_token()
    dispatch_note = load_dispatch_note()
    print(f"Buyer dispatch note: {'ON (dispatch_note.txt)' if dispatch_note else 'off (no dispatch_note.txt)'}")

    today = date.today()
    dispatched, skipped_db, skipped_etsy, failed = 0, 0, 0, 0
    held = []

    for o in orders:
        shopify_id = o.get("id")
        receipt_id = extract_etsy_receipt_id(o)
        if not receipt_id:
            continue

        parcels = collect_parcels(o)
        if not parcels:
            continue

        state = get_tracking_state(receipt_id)
        to_push = [] if state["legacy_done"] else [p for p in parcels if p[0] not in state["codes"]]
        if not to_push:
            skipped_db += 1
            continue

        due = get_dispatch_due_date(receipt_id, o)
        if due and today < due and not force_dispatch:
            held.append((due, receipt_id))
            continue

        etsy_state = get_etsy_shipment_state(receipt_id)
        time.sleep(0.15)
        if etsy_state is None:
            failed += 1
            continue
        if etsy_state["cancelled"] and not etsy_state["is_shipped"]:
            print(f"  [!!!] Etsy #{receipt_id} is CANCELLED/REFUNDED on Etsy but has a label in Shopify. "
                  f"Not dispatched{'' if dry_run else ' (will not be checked again)'}.")
            if not dry_run:
                record_synced_order(receipt_id, shopify_id, status="CANCELLED_AFTER_IMPORT")
                _db_execute("UPDATE synced_orders SET tracking_pushed = 1 WHERE etsy_receipt_id = ?", (receipt_id,))
            failed += 1
            continue
        # Shipped on Etsy with nothing in our DB: it was dispatched outside this tool. Never re-notify.
        shipped_elsewhere = etsy_state["is_shipped"] and not state["codes"]

        etsy_url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts/{receipt_id}/tracking"
        for index, (tracking_code, company) in enumerate(to_push):
            carrier_slug = map_carrier_for_etsy(company)
            is_extra_parcel = bool(state["codes"]) or index > 0
            label = " (additional parcel)" if is_extra_parcel else ""

            if shipped_elsewhere or tracking_code in etsy_state["codes"]:
                if index == 0:
                    print(f"  [-] Etsy #{receipt_id} is already dispatched on Etsy"
                          f"{'' if dry_run else ' (Saved to DB)'}")
                    skipped_etsy += 1
                if not dry_run:
                    record_tracking_pushed(receipt_id, tracking_code, shopify_id)
                continue

            if dry_run:
                print(f"  [DRY RUN] Would dispatch Etsy #{receipt_id} -> {tracking_code} ({carrier_slug}){label}")
                continue

            payload = {
                "tracking_code": tracking_code,
                "carrier_name": carrier_slug,
                "send_bcc": "false"
            }
            if dispatch_note and not is_extra_parcel:
                payload["note_to_buyer"] = dispatch_note
            try:
                r = etsy_request("POST", etsy_url, data=payload)
            except requests.RequestException as e:
                print(f"  [!] Failed #{receipt_id}: {e}")
                failed += 1
                continue

            if r.status_code in (200, 201):
                print(f"  [+] Dispatched Etsy #{receipt_id} with {tracking_code} ({carrier_slug}){label}")
                record_tracking_pushed(receipt_id, tracking_code, shopify_id)
                dispatched += 1
            elif is_already_shipped_response(r):
                print(f"  [-] Etsy #{receipt_id} already had tracking recorded on Etsy (Saved to DB)")
                record_tracking_pushed(receipt_id, tracking_code, shopify_id)
                skipped_etsy += 1
            elif is_extra_parcel and 400 <= r.status_code < 500:
                # Etsy refused a second tracking code; record it so it is not retried every run.
                print(f"  [!] Etsy rejected additional parcel {tracking_code} for #{receipt_id} "
                      f"({r.status_code} -> {r.text}). Not retrying.")
                record_tracking_pushed(receipt_id, tracking_code, shopify_id)
                failed += 1
            else:
                print(f"  [!] Failed #{receipt_id}: {r.status_code} -> {r.text}")
                failed += 1

            time.sleep(0.3)

    if held:
        print(f"\n  Holding {len(held)} order(s) until their Etsy ship-by date "
              f"(run again on the day, or use --force-dispatch):")
        for due, receipt_id in sorted(held):
            print(f"    - Etsy #{receipt_id}: dispatch on {due.strftime('%a %d %b %Y')}")

    print(f"Tracking Sync Summary -> Dispatched: {dispatched} | Held until ship-by date: {len(held)} | "
          f"Skipped (DB): {skipped_db} | Skipped (Etsy): {skipped_etsy} | Failed: {failed}")


# =====================================================================
# STEP 3: ETSY -> SHOPIFY DISPATCH (labels bought on Etsy)
# =====================================================================

def fulfill_shopify_order(shopify_order_id, tracking_code, carrier):
    """Marks every open fulfillment order of a Shopify order as fulfilled, without emailing
    the customer. Returns 'fulfilled', 'already' (nothing left to fulfil) or 'failed'."""
    base = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}"
    resp = shopify_request("GET", f"{base}/orders/{shopify_order_id}/fulfillment_orders.json")
    if resp.status_code != 200:
        print(f"  [!] Could not read fulfillment orders for Shopify ID {shopify_order_id} "
              f"({resp.status_code}): {resp.text}")
        return "failed"
    fulfillment_orders = resp.json().get("fulfillment_orders", [])
    open_ids = [fo["id"] for fo in fulfillment_orders if fo.get("status") in ("open", "in_progress")]
    # Orders held for Northern Ireland: Etsy shows them dispatched, so release the hold first.
    for fo in fulfillment_orders:
        if fo.get("status") == "on_hold":
            if not release_shopify_hold(fo["id"]):
                return "failed"
            open_ids.append(fo["id"])
    if not open_ids:
        return "already"

    fulfillment = {
        "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo_id} for fo_id in open_ids],
        "notify_customer": False,
    }
    if tracking_code:
        fulfillment["tracking_info"] = {"number": tracking_code, "company": carrier or "Other"}
    resp = shopify_request("POST", f"{base}/fulfillments.json", json={"fulfillment": fulfillment})
    if resp.status_code not in (200, 201):
        print(f"  [!] Could not fulfil Shopify ID {shopify_order_id} ({resp.status_code}): {resp.text}")
        return "failed"
    return "fulfilled"


def fetch_recent_shipped_receipts(pages=1):
    """The shop's most recent dispatched receipts (newest first), as {receipt_id: receipt}.

    One request per 100 receipts, instead of one request per waiting order.
    Also returns the purchase time (UTC string) of the oldest receipt fetched.
    """
    url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts"
    found, oldest = {}, None
    for page in range(pages):
        resp = etsy_request("GET", url, params={"was_paid": "true", "was_shipped": "true",
                                                "limit": 100, "offset": page * 100})
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch dispatched Etsy receipts ({resp.status_code}): {resp.text}")
        results = resp.json().get("results", [])
        for receipt in results:
            receipt = clean_receipt(receipt)
            found[str(receipt.get("receipt_id"))] = receipt
            ts = receipt.get("create_timestamp") or receipt.get("created_timestamp")
            if ts:
                stamp = _utc_str(ts)
                oldest = stamp if oldest is None else min(oldest, stamp)
        if len(results) < 100:
            break
        time.sleep(0.2)
    return found, oldest


def sync_etsy_dispatch_to_shopify(dry_run=False):
    """For orders whose label was bought on Etsy: marks the Shopify copy as fulfilled.

    Looks at orders that are in Shopify but have no dispatch recorded here, and checks them
    against Etsy's 100 most recent dispatches (one request in total). Any waiting order older than
    that window is checked individually, so nothing is missed. A dispatched order is
    fulfilled in Shopify with Etsy's tracking number, then recorded as SHIPPED so the
    tracking sync never sends that tracking back to Etsy.
    """
    print(f"\n--- Checking Etsy Dispatches to Mark Fulfilled in Shopify (Dry Run: {dry_run}) ---")
    rows = _db_query("""
        SELECT etsy_receipt_id, shopify_order_id, order_date FROM synced_orders
        WHERE status IN ('SUCCESS', 'EXISTING_IN_SHOPIFY') AND COALESCE(tracking_pushed, 0) = 0
        ORDER BY synced_at DESC LIMIT 300
    """)
    fulfilled, already, failed = 0, 0, 0
    if not rows:
        print("Etsy Dispatch Summary -> Nothing waiting.")
        return

    recent, oldest = fetch_recent_shipped_receipts()

    for receipt_id, shopify_id, order_date in rows:
        shopify_id = _numeric_id(shopify_id)
        if not shopify_id:
            continue
        try:
            receipt = recent.get(str(receipt_id))
            if receipt is None:
                # Not in the recent dispatches. Newer than the window means it is not dispatched;
                # older (or undated) means it could have been missed, so ask Etsy directly.
                if order_date and oldest and order_date >= oldest:
                    continue
                resp = etsy_request("GET", f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts/{receipt_id}")
                time.sleep(0.15)
                if resp.status_code != 200:
                    print(f"  [!] Could not check Etsy #{receipt_id}: {resp.status_code} -> {resp.text}")
                    failed += 1
                    continue
                receipt = clean_receipt(resp.json())
            if not receipt.get("is_shipped"):
                continue

            shipments = [s for s in receipt.get("shipments") or [] if str(s.get("tracking_code") or "").strip()]
            codes = [str(s["tracking_code"]).strip() for s in shipments]
            tracking_code = codes[0] if codes else ""
            carrier = str(shipments[0].get("carrier_name") or "") if shipments else ""
            shown = f"{tracking_code} ({carrier or 'no carrier'})" if tracking_code else "no tracking number"

            if dry_run:
                print(f"  [DRY RUN] Would mark Shopify order for Etsy #{receipt_id} as fulfilled -> {shown}")
                fulfilled += 1
                continue

            result = fulfill_shopify_order(shopify_id, tracking_code, carrier)
            if result == "failed":
                failed += 1
                continue
            if result == "fulfilled":
                print(f"  [+] Etsy #{receipt_id} was dispatched on Etsy; Shopify order marked fulfilled -> {shown}")
                fulfilled += 1
            else:
                print(f"  [-] Etsy #{receipt_id} was dispatched on Etsy and is already fulfilled in Shopify (Saved to DB)")
                already += 1
            # Record every Etsy code as pushed so the tracking sync leaves this order alone.
            for code in codes or [""]:
                record_tracking_pushed(receipt_id, code, shopify_id)
            save_order_details(receipt)
        except Exception as e:
            print(f"  [!] Failed #{receipt_id}: {e}")
            failed += 1

    print(f"Etsy Dispatch Summary -> Marked fulfilled in Shopify: {fulfilled} | "
          f"Already fulfilled: {already} | Failed: {failed}")


# =====================================================================
# HOUSEKEEPING: RUN LOG, DUE LIST, BACKUP
# =====================================================================

class _Tee:
    """Sends everything printed to both the console and the run log."""

    def __init__(self, console, logfile):
        self.console, self.logfile = console, logfile

    def write(self, data):
        try:
            self.console.write(data)
        except UnicodeEncodeError:
            enc = getattr(self.console, "encoding", None) or "ascii"
            self.console.write(data.encode(enc, "replace").decode(enc))
        self.logfile.write(data)

    def flush(self):
        self.console.flush()
        self.logfile.flush()


def start_run_log(argv):
    """Appends this run's output to logs/YYYY-MM-DD.log and deletes logs older than LOG_KEEP_DAYS."""
    os.makedirs(LOG_DIR, exist_ok=True)
    cutoff = time.time() - LOG_KEEP_DAYS * 86400
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        if name.endswith(".log") and os.path.getmtime(path) < cutoff:
            os.remove(path)
    logfile = open(os.path.join(LOG_DIR, f"{date.today().isoformat()}.log"), "a", encoding="utf-8")
    logfile.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} | sync.py {' '.join(argv) or '(tracking only)'} =====\n")
    sys.stdout = _Tee(sys.stdout, logfile)
    return logfile


def print_due_orders():
    """Lists imported orders that are due today or overdue and still not dispatched on Etsy."""
    today = date.today().isoformat()
    rows = _db_query("""
        SELECT etsy_receipt_id, expected_ship_date, buyer_name FROM synced_orders
        WHERE status IN ('SUCCESS', 'EXISTING_IN_SHOPIFY') AND COALESCE(tracking_pushed, 0) = 0
          AND expected_ship_date IS NOT NULL AND expected_ship_date <= ? AND expected_ship_date >= date(?, '-21 days')
        ORDER BY expected_ship_date
    """, (today, today))
    if not rows:
        return
    print(f"\n--- {len(rows)} order(s) due today or overdue, not yet dispatched on Etsy ---")
    for receipt_id, ship_by, name in rows:
        when = "DUE TODAY" if ship_by == today else f"OVERDUE since {ship_by}"
        print(f"  [{when}] Etsy #{receipt_id}{' - ' + name if name else ''}")


def backup_database():
    """Copies orders.db into BACKUP_DIR as orders-YYYY-MM-DD.db and keeps the newest BACKUP_KEEP.
    Never raises: a failed backup must not look like a failed sync."""
    try:
        if BACKUP_DIR == DEFAULT_BACKUP_DIR:
            os.makedirs(BACKUP_DIR, exist_ok=True)
        if not os.path.isdir(BACKUP_DIR):
            print(f"[Backup] Folder not found: {BACKUP_DIR} (check BACKUP_DIR in .env)")
            return
        dest = os.path.join(BACKUP_DIR, f"orders-{date.today().isoformat()}.db")
        tmp = dest + ".tmp"
        with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
        os.replace(tmp, dest)
        backups = sorted(n for n in os.listdir(BACKUP_DIR) if re.fullmatch(r"orders-\d{4}-\d{2}-\d{2}\.db", n))
        for old in backups[:-BACKUP_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, old))
        print(f"[Backup] Saved {dest}")
    except Exception as e:
        print(f"[Backup] FAILED: {e}")


# =====================================================================
# MAIN CLI ENTRYPOINT
# =====================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(description="Etsy <-> Shopify Order & Tracking Sync.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without creating orders, pushing tracking, or writing to orders.db.")
    parser.add_argument("--limit", type=int, default=500, help="Max Etsy receipts to check per run (default: 500).")
    parser.add_argument("--import-orders", action="store_true", help="Import unshipped Etsy orders into Shopify.")
    parser.add_argument("--backfill-deadlines", action="store_true", help="Update 'Fulfill by' deadlines on already-imported unshipped orders.")
    parser.add_argument("--tracking-only", action="store_true", help="Only push fulfilled tracking numbers from Shopify to Etsy (default behavior).")
    parser.add_argument("--force-dispatch", action="store_true", help="Mark orders dispatched on Etsy now, even before their ship-by date.")
    parser.add_argument("--exclude-ni", "--excludeNI", "--hold-ni", dest="exclude_ni", action="store_true",
                        help="Import orders going to Northern Ireland (BT postcodes) ON HOLD instead of unfulfilled, and list them so you can dispatch them on Etsy.")
    parser.add_argument("--tag-repeats", action="store_true",
                        help="Tag already-imported, not-yet-dispatched orders from repeat Etsy buyers (Repeat customer, Repeat order N), then exit.")
    args = parser.parse_args(argv)

    console = sys.stdout
    logfile = start_run_log(sys.argv[1:] if argv is None else argv)
    try:
        init_db()
        if args.tag_repeats:
            tag_repeat_customers(dry_run=args.dry_run)
            return
        ensure_etsy_token()

        if (args.import_orders or args.backfill_deadlines) and not args.tracking_only:
            import_etsy_orders(args)

        sync_etsy_dispatch_to_shopify(dry_run=args.dry_run)
        sync_tracking_to_etsy(dry_run=args.dry_run, force_dispatch=args.force_dispatch)
        print_due_orders()
        if not args.dry_run:
            backup_database()
        print("\n--- Sync Complete ---")
        print_ni_banner()
    except Exception:
        print("\n[!!!] The sync stopped with an error:")
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
    finally:
        sys.stdout = console
        logfile.close()


if __name__ == "__main__":
    main()
