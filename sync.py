import os
import re
import sys
import time
import sqlite3
import argparse
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
SHOPIFY_API_VERSION = "2024-10"

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


# =====================================================================
# DATABASE MANAGEMENT (SQLite)
# =====================================================================

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS etsy_emails (
            receipt_id TEXT PRIMARY KEY,
            email TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("PRAGMA table_info(synced_orders)")
    columns = [col[1] for col in cursor.fetchall()]
    if "tracking_number" not in columns:
        cursor.execute("ALTER TABLE synced_orders ADD COLUMN tracking_number TEXT")
    if "tracking_pushed" not in columns:
        cursor.execute("ALTER TABLE synced_orders ADD COLUMN tracking_pushed INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def get_cached_email(receipt_id):
    """Checks local SQLite etsy_emails table if populated by the browser extension."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM etsy_emails WHERE receipt_id = ?", (str(receipt_id),))
    row = cursor.fetchone()
    conn.close()
    return row[0].strip() if row and row[0] else ""


def is_order_synced(receipt_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM synced_orders WHERE etsy_receipt_id = ?", (str(receipt_id),))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def is_tracking_pushed(receipt_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT tracking_pushed FROM synced_orders WHERE etsy_receipt_id = ?", (str(receipt_id),))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def record_synced_order(receipt_id, shopify_order_id, status="SUCCESS"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO synced_orders (etsy_receipt_id, shopify_order_id, status)
        VALUES (?, ?, ?)
        ON CONFLICT(etsy_receipt_id) DO UPDATE SET
            shopify_order_id = excluded.shopify_order_id,
            status = excluded.status
    """, (str(receipt_id), str(shopify_order_id), status))
    conn.commit()
    conn.close()


def record_tracking_pushed(receipt_id, tracking_number, shopify_order_id=""):
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


# =====================================================================
# AUTHENTICATION & HEADERS
# =====================================================================

def refresh_etsy_tokens():
    global ETSY_ACCESS_TOKEN, ETSY_REFRESH_TOKEN
    print("[Etsy] Refreshing access token...")
    url = "https://api.etsy.com/v3/public/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": ETSY_KEYSTRING,
        "refresh_token": ETSY_REFRESH_TOKEN
    }
    resp = requests.post(url, data=payload, timeout=15)
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


# =====================================================================
# SHOPIFY SKU LOOKUP & DUPLICATE GUARD
# =====================================================================

def build_shopify_sku_map():
    sku_map = {}
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=250&fields=id,title,variants"
    try:
        resp = requests.get(url, headers=get_shopify_headers(), timeout=15)
        if resp.status_code == 200:
            for prod in resp.json().get("products", []):
                for v in prod.get("variants", []):
                    sku = (v.get("sku") or "").strip()
                    if sku:
                        sku_map[sku] = v["id"]
    except Exception as e:
        print(f"[Shopify] Warning: Could not build SKU cache ({e}).")
    return sku_map


def extract_etsy_receipt_id(order):
    """Extracts 10-digit Etsy Receipt ID from Shopify order name, note, or tags."""
    name = str(order.get("name", "")).replace("#", "").strip()
    if name.isdigit() and len(name) == 10:
        return name
    note = str(order.get("note") or "")
    tags = str(order.get("tags") or "")
    match = re.search(r"\b(\d{10})\b", f"{note} {tags}")
    if match:
        return match.group(1)
    return None


def build_existing_shopify_receipt_map():
    """Fetches recent 250 Shopify orders and maps {etsy_receipt_id: shopify_order_id}."""
    existing = {}
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "order": "created_at desc",
        "limit": 250,
        "fields": "id,name,note,tags"
    }
    resp = requests.get(url, headers=get_shopify_headers(), params=params, timeout=20)
    if resp.status_code == 200:
        for order in resp.json().get("orders", []):
            rid = extract_etsy_receipt_id(order)
            if rid:
                existing[rid] = order.get("id")
    return existing


# =====================================================================
# STEP 1: ETSY -> SHOPIFY ORDER IMPORT & DEADLINES
# =====================================================================

def fetch_etsy_orders(limit=40):
    url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts"
    params = {
        "was_paid": "true",
        "was_shipped": "false",
        "limit": limit
    }
    resp = requests.get(url, headers=get_etsy_headers(), params=params, timeout=15)
    if resp.status_code == 401:
        refresh_etsy_tokens()
        resp = requests.get(url, headers=get_etsy_headers(), params=params, timeout=15)

    if resp.status_code != 200:
        raise Exception(f"Failed to fetch Etsy orders ({resp.status_code}): {resp.text}")

    raw_results = resp.json().get("results", [])
    valid_orders = []

    for receipt in raw_results:
        rid = str(receipt.get("receipt_id"))
        is_cancelled = receipt.get("is_cancelled", False)
        status = str(receipt.get("status", "")).lower()

        if is_cancelled or "cancel" in status or "refund" in status:
            if not is_order_synced(rid):
                print(f"  [-] Skipping newly detected cancelled Etsy Receipt #{rid} (Saved to DB)")
                record_synced_order(rid, "CANCELLED_ON_ETSY", status="CANCELLED")
            continue

        valid_orders.append(receipt)

    return valid_orders


def parse_money(money_obj):
    if not money_obj or not isinstance(money_obj, dict):
        return 0.0
    return money_obj.get("amount", 0) / money_obj.get("divisor", 100)


def get_etsy_purchase_iso(receipt):
    """Extracts Etsy's purchase timestamp and returns an ISO 8601 UTC string for Shopify processed_at."""
    created_ts = receipt.get("create_timestamp") or receipt.get("created_timestamp")
    if created_ts:
        return datetime.fromtimestamp(int(created_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def get_etsy_ship_by_iso(receipt):
    """Extracts Etsy's expected_ship_date from the receipt or its transactions and returns ISO 8601 UTC."""
    expected_ts = receipt.get("expected_ship_date")
    if not expected_ts:
        txs = receipt.get("transactions", [])
        ship_dates = [tx.get("expected_ship_date") for tx in txs if tx.get("expected_ship_date")]
        if ship_dates:
            expected_ts = max(ship_dates)

    if expected_ts:
        return datetime.fromtimestamp(int(expected_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def set_shopify_fulfill_by(shopify_order_id, fulfill_by_iso):
    """Populates the 'Fulfill by' column in Shopify Admin using GraphQL fulfillmentOrdersSetFulfillmentDeadline."""
    if not shopify_order_id or not fulfill_by_iso:
        return

    headers = get_shopify_headers()
    fo_url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders/{shopify_order_id}/fulfillment_orders.json"
    resp = requests.get(fo_url, headers=headers, timeout=15)
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

    g_resp = requests.post(graphql_url, headers=headers, json={"query": query, "variables": variables}, timeout=15)
    if g_resp.status_code == 200:
        data = g_resp.json()
        result = data.get("data", {}).get("fulfillmentOrdersSetFulfillmentDeadline", {})
        user_errors = result.get("userErrors", [])
        if result.get("success") and not user_errors:
            print(f"  [+] Set 'Fulfill by' deadline to {fulfill_by_iso}")
        else:
            print(f"  [!] GraphQL error setting 'Fulfill by': {user_errors or data.get('errors')}")
    else:
        print(f"  [!] Failed to set 'Fulfill by' deadline ({g_resp.status_code}): {g_resp.text}")


def map_etsy_to_shopify(receipt, sku_map=None):
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

    # 1. Shipping Upgrade Detection (Tracked 24 vs Tracked 48)
    shipping_upgrade = receipt.get("shipping_upgrade")
    if not shipping_upgrade:
        for tx in transactions:
            if tx.get("shipping_upgrade"):
                shipping_upgrade = tx.get("shipping_upgrade")
                break

    shipping_str = str(shipping_upgrade or "")
    is_express = any(k in shipping_str.lower() for k in ("24", "express", "first", "1st", "next day", "priority"))

    if shipping_str:
        shipping_title = shipping_str
        shipping_code = "Royal Mail Tracked 24" if is_express else shipping_str
    else:
        shipping_title = "Royal Mail Tracked 48"
        shipping_code = "Royal Mail Tracked 48"

    shipping_cost = parse_money(receipt.get("total_shipping_cost"))
    discount_amt = parse_money(receipt.get("discount_amount"))
    grandtotal = parse_money(receipt.get("grandtotal")) or max(0.0, items_subtotal + shipping_cost - discount_amt)

    # 2. Build Tags & Extract Dates
    tags = ["Etsy"]
    if is_express:
        tags.extend(["Tracked 24", "Priority"])
    elif shipping_str:
        tags.append(shipping_str)

    expected_ts = receipt.get("expected_ship_date")
    if not expected_ts:
        ship_dates = [tx.get("expected_ship_date") for tx in transactions if tx.get("expected_ship_date")]
        if ship_dates:
            expected_ts = max(ship_dates)

    if expected_ts:
        ship_by_str = datetime.fromtimestamp(int(expected_ts), tz=timezone.utc).strftime("%Y-%m-%d")
        tags.append(f"ShipBy:{ship_by_str}")

    processed_at_iso = get_etsy_purchase_iso(receipt)

    # 3. Compile Notes
    notes = [f"Imported from Etsy. Receipt ID: {receipt_id}"]
    if discount_amt > 0:
        notes[0] += f". Etsy discount: £{discount_amt:.2f}"

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
        "country_code": receipt.get("country_iso") or "GB",
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
        "inventory_behaviour": "decrement_obeying_policy",
        "shipping_lines": [
            {
                "title": shipping_title,
                "price": f"{shipping_cost:.2f}",
                "code": shipping_code
            }
        ],
        "transactions": [transaction_entry],
        "tags": ", ".join(tags),
        "note": "\n\n".join(notes)
    }

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
    resp = requests.post(url, json=order_payload, headers=get_shopify_headers(), timeout=15)
    if resp.status_code not in (200, 201):
        raise Exception(f"Shopify order creation failed ({resp.status_code}): {resp.text}")
    return resp.json().get("order", {})


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


def sync_tracking_to_etsy(dry_run=False, limit=200):
    print(f"\n--- Checking Shopify Fulfillments to Dispatch on Etsy (Dry Run: {dry_run}) ---")
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "order": "created_at desc",
        "limit": limit,
        "fields": "id,name,note,tags,fulfillment_status,fulfillments"
    }

    resp = requests.get(url, headers=get_shopify_headers(), params=params, timeout=20)
    if resp.status_code != 200:
        raise Exception(f"Failed to fetch orders from Shopify: {resp.text}")

    orders = resp.json().get("orders", [])

    refresh_etsy_tokens()
    etsy_headers = get_etsy_headers()
    etsy_headers.pop("Content-Type", None)

    dispatched, skipped_db, skipped_etsy, failed = 0, 0, 0, 0

    for o in orders:
        shopify_id = o.get("id")
        receipt_id = extract_etsy_receipt_id(o)
        if not receipt_id:
            continue

        fulfillments = o.get("fulfillments", [])
        valid_fulfillments = [
            f for f in fulfillments
            if f.get("tracking_number") and f.get("status") != "cancelled"
        ]
        if not valid_fulfillments:
            continue

        if is_tracking_pushed(receipt_id):
            skipped_db += 1
            continue

        tracking_code = str(valid_fulfillments[0].get("tracking_number")).strip()
        carrier_slug = map_carrier_for_etsy(valid_fulfillments[0].get("tracking_company"))

        if dry_run:
            print(f"  [DRY RUN] Would dispatch Etsy #{receipt_id} -> {tracking_code} ({carrier_slug})")
            continue

        payload = {
            "tracking_code": tracking_code,
            "carrier_name": carrier_slug,
            "send_bcc": "false"
        }

        etsy_url = f"https://api.etsy.com/v3/application/shops/{ETSY_SHOP_ID}/receipts/{receipt_id}/tracking"
        r = requests.post(etsy_url, headers=etsy_headers, data=payload, timeout=15)

        if r.status_code == 200:
            print(f"  [+] Dispatched Etsy #{receipt_id} with {tracking_code} ({carrier_slug})")
            record_tracking_pushed(receipt_id, tracking_code, shopify_id)
            dispatched += 1
        elif (
            r.status_code == 409
            or "already shipped" in r.text.lower()
            or (r.status_code == 400 and "already in use" in r.text.lower())
        ):
            print(f"  [-] Etsy #{receipt_id} already had tracking recorded on Etsy (Saved to DB)")
            record_tracking_pushed(receipt_id, tracking_code, shopify_id)
            skipped_etsy += 1
        else:
            print(f"  [!] Failed #{receipt_id}: {r.status_code} -> {r.text}")
            failed += 1

        time.sleep(0.3)

    print(f"Tracking Sync Summary -> Dispatched: {dispatched} | Skipped (DB): {skipped_db} | Skipped (Etsy): {skipped_etsy} | Failed: {failed}")


# =====================================================================
# MAIN CLI ENTRYPOINT
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="Etsy <-> Shopify Order & Tracking Sync.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without creating orders or pushing tracking.")
    parser.add_argument("--limit", type=int, default=100, help="Max Etsy orders to check per run (default: 100).")
    parser.add_argument("--import-orders", action="store_true", help="Import unshipped Etsy orders into Shopify.")
    parser.add_argument("--backfill-deadlines", action="store_true", help="Update 'Fulfill by' deadlines on already-imported unshipped orders.")
    parser.add_argument("--tracking-only", action="store_true", help="Only push fulfilled tracking numbers from Shopify to Etsy (default behavior).")
    args = parser.parse_args()

    init_db()
    refresh_etsy_tokens()

    if args.import_orders or args.backfill_deadlines:
        print(f"\n--- Checking Unshipped Orders: Etsy -> Shopify (Dry Run: {args.dry_run}) ---")
        sku_map = build_shopify_sku_map() if args.import_orders else {}
        existing_shopify_map = build_existing_shopify_receipt_map()
        orders = fetch_etsy_orders(limit=args.limit)
        print(f"Found {len(orders)} active unshipped/paid receipt(s) on Etsy.")

        created_count, backfilled_count, skipped_existing, failed_import = 0, 0, 0, 0

        for receipt in orders:
            receipt_id = str(receipt["receipt_id"])
            fulfill_by_iso = get_etsy_ship_by_iso(receipt)
            processed_at_iso = get_etsy_purchase_iso(receipt)

            # Check if order already exists in Shopify
            if receipt_id in existing_shopify_map or is_order_synced(receipt_id):
                existing_id = existing_shopify_map.get(receipt_id)
                if args.backfill_deadlines and existing_id and fulfill_by_iso:
                    print(f"  [~] Backfilling 'Fulfill by' ({fulfill_by_iso}) for existing Order #{receipt_id} (Shopify ID: {existing_id})...")
                    if not args.dry_run:
                        set_shopify_fulfill_by(existing_id, fulfill_by_iso)
                    backfilled_count += 1
                else:
                    print(f"  [-] Skipping Receipt #{receipt_id}: already in Shopify/DB.")
                    if existing_id and not is_order_synced(receipt_id) and not args.dry_run:
                        record_synced_order(receipt_id, existing_id, status="EXISTING_IN_SHOPIFY")
                    skipped_existing += 1
                continue

            if not args.import_orders:
                continue

            print(f"\nProcessing Etsy Receipt #{receipt_id} for {receipt.get('name')}...")
            payload = map_etsy_to_shopify(receipt, sku_map=sku_map)

            if args.dry_run:
                o_data = payload["order"]
                c_name = f"{o_data['customer']['first_name']} {o_data['customer']['last_name']}"
                print(f"  [DRY RUN] Order Name: #{o_data['name']} | Customer: {c_name} | Total: £{o_data['transactions'][0]['amount']}")
                print(f"  [DRY RUN] Purchase Time (processed_at): {processed_at_iso} | Fulfill By: {fulfill_by_iso}")
                print(f"  [DRY RUN] Tags: {o_data['tags']}")
                print(f"  [DRY RUN] Shipping: {o_data['shipping_lines'][0]['title']} (£{o_data['shipping_lines'][0]['price']})")
                for item in o_data["line_items"]:
                    var_info = f" [Variant ID: {item['variant_id']}]" if "variant_id" in item else " [Unlinked SKU]"
                    props = f" | Options: {item.get('properties')}" if item.get("properties") else ""
                    print(f"    - {item['quantity']}x {item['title']} (SKU: {item.get('sku', 'N/A')}){var_info}{props}")
            else:
                try:
                    created_order = push_to_shopify(payload)
                    shopify_id = created_order.get("id")
                    record_synced_order(receipt_id, shopify_id, status="SUCCESS")
                    print(f"  [+] Created Shopify Order {created_order.get('name')} (ID: {shopify_id}, Purchased: {processed_at_iso})")
                    if fulfill_by_iso:
                        set_shopify_fulfill_by(shopify_id, fulfill_by_iso)
                    created_count += 1
                except Exception as e:
                    print(f"  [!] Failed to push order #{receipt_id}: {e}")
                    failed_import += 1

        print(f"\nOrder Import Summary -> Created: {created_count} | Backfilled: {backfilled_count} | Skipped (Existing): {skipped_existing} | Failed: {failed_import}")

    sync_tracking_to_etsy(dry_run=args.dry_run, limit=200)
    print("\n--- Sync Complete ---")


if __name__ == "__main__":
    main()