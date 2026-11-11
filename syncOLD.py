import os
import sys
import time
import sqlite3
import argparse
import requests
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(ENV_PATH)

DB_PATH = os.path.join(os.path.dirname(__file__), "orders.db")

# --- Credentials ---
ETSY_KEYSTRING = os.getenv("ETSY_KEYSTRING", "").strip("'\"")
ETSY_SHARED_SECRET = os.getenv("ETSY_SHARED_SECRET", "").strip("'\"")
ETSY_ACCESS_TOKEN = os.getenv("ETSY_ACCESS_TOKEN", "").strip("'\"")
ETSY_REFRESH_TOKEN = os.getenv("ETSY_REFRESH_TOKEN", "").strip("'\"")
ETSY_SHOP_ID = os.getenv("ETSY_SHOP_ID", "").strip("'\"")

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "").strip("'\"")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip("'\"")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip("'\"")

cached_shopify_token = None
cached_shopify_token_expires = 0


def init_db():
    """Initializes local SQLite database to prevent duplicate order imports."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS synced_orders (
            etsy_receipt_id TEXT PRIMARY KEY,
            shopify_order_id TEXT,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_order_synced(receipt_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM synced_orders WHERE etsy_receipt_id = ?", (str(receipt_id),))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def record_synced_order(receipt_id, shopify_order_id, status="SUCCESS"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO synced_orders (etsy_receipt_id, shopify_order_id, status)
        VALUES (?, ?, ?)
    """, (str(receipt_id), str(shopify_order_id), status))
    conn.commit()
    conn.close()


def refresh_etsy_tokens():
    """Refreshes Etsy OAuth token using the refresh token."""
    global ETSY_ACCESS_TOKEN, ETSY_REFRESH_TOKEN
    print("[Etsy] Refreshing access token...")
    url = "https://api.etsy.com/v3/public/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": ETSY_KEYSTRING,
        "refresh_token": ETSY_REFRESH_TOKEN
    }
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        raise Exception(f"Failed to refresh Etsy token: {resp.text}")

    data = resp.json()
    ETSY_ACCESS_TOKEN = data["access_token"]
    ETSY_REFRESH_TOKEN = data["refresh_token"]
    expires_at = str(int(time.time()) + data.get("expires_in", 3600))

    set_key(ENV_PATH, "ETSY_ACCESS_TOKEN", ETSY_ACCESS_TOKEN)
    set_key(ENV_PATH, "ETSY_REFRESH_TOKEN", ETSY_REFRESH_TOKEN)
    set_key(ENV_PATH, "ETSY_TOKEN_EXPIRES_AT", expires_at)
    print("[Etsy] Token refreshed and saved to .env")


def get_etsy_headers():
    return {
        "x-api-key": f"{ETSY_KEYSTRING}:{ETSY_SHARED_SECRET}",
        "Authorization": f"Bearer {ETSY_ACCESS_TOKEN}"
    }


def get_shopify_token():
    """Obtains 24-hr Admin API token via Dev Dashboard client_credentials grant."""
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
    
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Failed to obtain Shopify access token ({resp.status_code}): {resp.text}")

    data = resp.json()
    cached_shopify_token = data["access_token"]
    cached_shopify_token_expires = time.time() + data.get("expires_in", 86399) - 60
    print("[Shopify] Access token obtained successfully.")
    return cached_shopify_token


def fetch_etsy_orders(limit=10):
    """Fetches paid and unshipped orders from Etsy."""
    url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts"
    params = {
        "was_paid": "true",
        "was_shipped": "false",
        "limit": limit
    }
    
    resp = requests.get(url, headers=get_etsy_headers(), params=params)
    if resp.status_code == 401:
        refresh_etsy_tokens()
        resp = requests.get(url, headers=get_etsy_headers(), params=params)

    if resp.status_code != 200:
        raise Exception(f"Failed to fetch Etsy orders ({resp.status_code}): {resp.text}")

    return resp.json().get("results", [])


def map_etsy_to_shopify(receipt):
    """Maps an Etsy receipt structure into a Shopify Order creation payload."""
    line_items = []
    total_price = 0.0

    for tx in receipt.get("transactions", []):
        sku = tx.get("sku") or ""
        title = tx.get("title") or "Etsy Item"
        quantity = tx.get("quantity", 1)
        price_val = tx.get("price", {}).get("amount", 0) / tx.get("price", {}).get("divisor", 100)
        total_price += price_val * quantity

        item = {
            "title": title,
            "quantity": quantity,
            "price": f"{price_val:.2f}",
            "requires_shipping": True
        }
        if sku:
            item["sku"] = sku
        line_items.append(item)

    # Parse full name
    full_name = (receipt.get("name") or "").strip()
    name_parts = full_name.split(" ")
    first_name = name_parts[0] if name_parts else "Etsy"
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else "."

    shipping_address = {
        "first_name": first_name,
        "last_name": last_name,
        "address1": receipt.get("first_line", ""),
        "address2": receipt.get("second_line", ""),
        "city": receipt.get("city", ""),
        "zip": receipt.get("zip", ""),
        "country": receipt.get("country_iso", "GB"),
    }

    # Explicit Customer block so Shopify links/creates the customer
    customer = {
        "first_name": first_name,
        "last_name": last_name,
        "email": receipt.get("buyer_email")
    }

    order_payload = {
        "order": {
            "line_items": line_items,
            "customer": customer,
            "shipping_address": shipping_address,
            "billing_address": shipping_address,
            "email": receipt.get("buyer_email"),
            "financial_status": "paid",
            "transactions": [
                {
                    "kind": "sale",
                    "status": "success",
                    "amount": f"{total_price:.2f}",
                    "gateway": "Etsy Payments"
                }
            ],
            "tags": f"Etsy Import, Etsy Receipt #{receipt.get('receipt_id')}",
            "note": f"Imported automatically from Etsy (Receipt ID: {receipt.get('receipt_id')}). Payment processed via Etsy Payments."
        }
    }
    return order_payload


def push_to_shopify(order_payload):
    """Creates an order in Shopify via Admin REST API."""
    token = get_shopify_token()
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/2026-10/orders.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }
    resp = requests.post(url, json=order_payload, headers=headers)
    if resp.status_code not in (200, 201):
        raise Exception(f"Shopify order creation failed ({resp.status_code}): {resp.text}")
    return resp.json().get("order", {})


def main():
    parser = argparse.ArgumentParser(description="Sync orders from Etsy to Shopify.")
    parser.add_argument("--dry-run", action="store_true", help="Print mapped payloads without writing to Shopify.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of orders to fetch.")
    args = parser.parse_args()

    init_db()

    print(f"--- Starting Sync (Dry Run: {args.dry_run}) ---")
    orders = fetch_etsy_orders(limit=args.limit)
    print(f"Found {len(orders)} unshipped/paid receipt(s) on Etsy.")

    for receipt in orders:
        receipt_id = receipt["receipt_id"]
        if is_order_synced(receipt_id):
            print(f"Skipping Receipt #{receipt_id}: already synced.")
            continue

        print(f"\nProcessing Etsy Receipt #{receipt_id} for {receipt.get('name')}...")
        payload = map_etsy_to_shopify(receipt)

        if args.dry_run:
            print("  [DRY RUN] Prepared Shopify payload:")
            for item in payload["order"]["line_items"]:
                print(f"    - {item['quantity']}x {item['title']} (SKU: {item.get('sku', 'N/A')}) @ £{item['price']}")
            print(f"    Shipping to: {payload['order']['shipping_address']['city']}, {payload['order']['shipping_address']['zip']}")
        else:
            try:
                created_order = push_to_shopify(payload)
                shopify_id = created_order.get("id")
                record_synced_order(receipt_id, shopify_id, status="SUCCESS")
                print(f"  [SUCCESS] Created Shopify Order #{created_order.get('name')} (ID: {shopify_id})")
            except Exception as e:
                print(f"  [ERROR] Failed to push order: {e}")

    print("\n--- Sync Complete ---")


if __name__ == "__main__":
    main()