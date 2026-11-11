import os
import re
import time
import sqlite3
import requests
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
SHOPIFY_API_VERSION = "2024-10"

ETSY_KEYSTRING = os.getenv("ETSY_KEYSTRING", "").strip("'\"")
ETSY_SHARED_SECRET = os.getenv("ETSY_SHARED_SECRET", "").strip("'\"")
ETSY_ACCESS_TOKEN = os.getenv("ETSY_ACCESS_TOKEN", "").strip("'\"")
ETSY_REFRESH_TOKEN = os.getenv("ETSY_REFRESH_TOKEN", "").strip("'\"")
ETSY_SHOP_ID = os.getenv("ETSY_SHOP_ID", "").strip("'\"")

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "").strip("'\"")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip("'\"")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip("'\"")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS synced_orders (
            etsy_receipt_id TEXT PRIMARY KEY,
            shopify_order_id TEXT,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            tracking_number TEXT,
            tracking_pushed INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def is_tracking_pushed(receipt_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT tracking_pushed FROM synced_orders WHERE etsy_receipt_id = ?", (str(receipt_id),))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def mark_tracking_pushed(receipt_id, tracking_number, shopify_order_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO synced_orders (etsy_receipt_id, shopify_order_id, status, tracking_number, tracking_pushed)
        VALUES (?, ?, 'SHIPPED', ?, 1)
        ON CONFLICT(etsy_receipt_id) DO UPDATE SET
            tracking_number = excluded.tracking_number,
            tracking_pushed = 1,
            status = 'SHIPPED'
    """, (str(receipt_id), str(shopify_order_id), str(tracking_number)))
    conn.commit()
    conn.close()


def refresh_etsy():
    global ETSY_ACCESS_TOKEN, ETSY_REFRESH_TOKEN
    print("[Etsy] Refreshing token...")
    url = "https://api.etsy.com/v3/public/oauth/token"
    resp = requests.post(url, data={
        "grant_type": "refresh_token",
        "client_id": ETSY_KEYSTRING,
        "refresh_token": ETSY_REFRESH_TOKEN
    }, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to refresh Etsy token: {resp.text}")
    res = resp.json()
    ETSY_ACCESS_TOKEN = res["access_token"]
    ETSY_REFRESH_TOKEN = res["refresh_token"]
    set_key(ENV_PATH, "ETSY_ACCESS_TOKEN", ETSY_ACCESS_TOKEN)
    set_key(ENV_PATH, "ETSY_REFRESH_TOKEN", ETSY_REFRESH_TOKEN)


def get_shopify_token():
    url = f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token"
    payload = {
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Shopify auth failed: {resp.text}")
    return resp.json()["access_token"]


def extract_etsy_receipt_id(order):
    """Extracts 10-digit Etsy Receipt ID from order name, note, or tags."""
    name = str(order.get("name", "")).replace("#", "").strip()
    if name.isdigit() and len(name) == 10:
        return name

    # Fallback: check note or tags for a 10-digit Etsy receipt number
    note = str(order.get("note") or "")
    tags = str(order.get("tags") or "")
    match = re.search(r"\b(\d{10})\b", f"{note} {tags}")
    if match:
        return match.group(1)
    return None


def main():
    init_db()
    refresh_etsy()

    etsy_headers = {
        "x-api-key": f"{ETSY_KEYSTRING}:{ETSY_SHARED_SECRET}",
        "Authorization": f"Bearer {ETSY_ACCESS_TOKEN}"
    }
    shopify_headers = {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }

    # CRITICAL FIX: Do NOT use fulfillment_status=shipped (it hides newly printed Royal Mail labels).
    # Instead, pull the latest 200 orders newest-first and inspect their fulfillments list directly.
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "order": "created_at desc",
        "limit": 200,
        "fields": "id,name,note,tags,fulfillment_status,fulfillments"
    }
    resp = requests.get(url, headers=shopify_headers, params=params, timeout=20)
    orders = resp.json().get("orders", [])

    print(f"\nScanning {len(orders)} recent Shopify orders for fulfilled tracking codes...")

    success, skipped_db, skipped_etsy, failed = 0, 0, 0, 0

    for o in orders:
        receipt_id = extract_etsy_receipt_id(o)
        if not receipt_id:
            continue

        fulfillments = o.get("fulfillments", [])
        # Ignore cancelled fulfillments; grab active tracking numbers
        trackings = [
            str(f.get("tracking_number")).strip()
            for f in fulfillments
            if f.get("tracking_number") and f.get("status") != "cancelled"
        ]
        if not trackings:
            continue

        tracking_code = trackings[0]
        shopify_id = o.get("id")

        # Skip immediately if already marked pushed in local SQLite DB
        if is_tracking_pushed(receipt_id):
            skipped_db += 1
            continue

        payload = {
            "tracking_code": tracking_code,
            "carrier_name": "royal-mail",
            "send_bcc": "false"
        }

        etsy_url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts/{receipt_id}/tracking"
        r = requests.post(etsy_url, headers=etsy_headers, data=payload, timeout=15)

        if r.status_code == 200:
            print(f"[+] Dispatched #{receipt_id} with {tracking_code}")
            mark_tracking_pushed(receipt_id, tracking_code, shopify_id)
            success += 1
        elif (
            r.status_code == 409
            or "already shipped" in r.text.lower()
            or (r.status_code == 400 and "already in use" in r.text.lower())
        ):
            print(f"[-] #{receipt_id} already marked on Etsy (Saved to DB)")
            mark_tracking_pushed(receipt_id, tracking_code, shopify_id)
            skipped_etsy += 1
        else:
            print(f"[!] Failed #{receipt_id}: {r.status_code} -> {r.text}")
            failed += 1

        time.sleep(0.3)

    print(f"\nFinished! Dispatched: {success} | Skipped (Already in DB): {skipped_db} | Skipped (On Etsy): {skipped_etsy} | Failed: {failed}")


if __name__ == "__main__":
    main()