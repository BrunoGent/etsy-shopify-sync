"""One-off: replace OrderBridge's fake customer emails with the buyer's real, captured email.

The old OrderBridge tool created Shopify customers with a made-up address such as
etsy-4179416659@placeholder.orderbridge.app (4179416659 = the Etsy order number). Those addresses go nowhere.
Where the email capture (email_capture/) has since saved the buyer's real email, this puts it on the customer.

    python replace_placeholder_emails.py                 # rehearsal: reads Shopify, changes nothing
    python replace_placeholder_emails.py --apply --limit 10   # change the first 10 customers
    python replace_placeholder_emails.py --apply         # change all of them

Safeguards:
  * Only customers whose email is a placeholder are touched; a real email is never replaced.
  * Customers only (orders keep whatever email they have), and marketing consent is never sent.
  * A customer that is subscribed, or pending, is skipped: a real address is never put on a marketing state.
  * If the real email already belongs to another Shopify customer, that customer is left alone (merge by hand,
    or with merge_duplicate_customers.py).
  * Placeholders with no captured real email are left as they are.
  * Every change is written to logs/placeholder_email_replace_<time>.json (ids only).
"""
import argparse
import collections
import json
import os
import re
import time
from datetime import datetime

import sync

PLACEHOLDER_RE = re.compile(r"^etsy-(\d+)@placeholder\.")     # etsy-<order number>@placeholder.orderbridge.app (and .com variants)
CUSTOMERS_QUERY = """
query($cursor: String) {
  customers(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id numberOfOrders defaultEmailAddress { emailAddress marketingState } }
  }
}"""


def all_customers():
    cursor, out = None, []
    while True:
        data = sync.shopify_graphql(CUSTOMERS_QUERY, {"cursor": cursor})["customers"]
        out += data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            return out
        cursor = data["pageInfo"]["endCursor"]
        time.sleep(0.2)


def real_email_for(receipt_id, by_receipt, by_buyer):
    """The captured real email for an Etsy receipt (directly, or through the buyer's other orders)."""
    email = by_receipt.get(receipt_id, "")
    if not email:
        row = sync._db_query("SELECT buyer_user_id FROM synced_orders WHERE etsy_receipt_id = ?", (receipt_id,))
        uid = str(row[0][0]) if row and row[0][0] else ""
        email = by_buyer.get(uid, "")
    email = (email or "").strip()
    return email if "@" in email and "@placeholder." not in email.lower() else ""


def main():
    ap = argparse.ArgumentParser(description="Replace OrderBridge placeholder emails on Shopify customers with real captured emails.")
    ap.add_argument("--apply", action="store_true", help="really change Shopify (default: rehearsal)")
    ap.add_argument("--limit", type=int, default=None, help="stop after changing this many customers")
    args = ap.parse_args()

    sync.init_db()
    try:
        by_buyer = {str(r[0]): r[1] for r in sync._db_query("SELECT buyer_id, email FROM buyer_emails WHERE email <> ''")}
    except Exception:
        by_buyer = {}
    by_receipt = {str(r[0]): r[1] for r in sync._db_query("SELECT receipt_id, email FROM etsy_emails WHERE email <> ''")}

    print("Reading every customer from Shopify...")
    customers = all_customers()
    placeholders = []
    for c in customers:
        e = c.get("defaultEmailAddress") or {}
        addr = (e.get("emailAddress") or "").strip()
        m = PLACEHOLDER_RE.match(addr.lower())
        if m:
            placeholders.append({"id": c["id"], "orders": int(c.get("numberOfOrders") or 0), "state": e.get("marketingState"),
                                 "receipt": m.group(1)})
    print(f"{len(customers)} customers in Shopify; {len(placeholders)} have an OrderBridge placeholder email.")
    print("  marketing state of those:", dict(collections.Counter(p["state"] for p in placeholders)))

    plan, no_email, unsafe = [], 0, 0
    for p in placeholders:
        real = real_email_for(p["receipt"], by_receipt, by_buyer) if p["receipt"] else ""
        if not real:
            no_email += 1
        elif p["state"] not in ("NOT_SUBSCRIBED", None):
            unsafe += 1
        else:
            plan.append((p, real))
    print(f"  real email captured and safe to use: {len(plan)}")
    print(f"  no real email captured yet (left as they are): {no_email}")
    print(f"  skipped because the customer is subscribed or pending: {unsafe}")

    if not args.apply:
        print(f"\nREHEARSAL: {len(plan)} customers would get their real email. Nothing was changed. Add --apply (try --limit 10 first) to do it.")
        return

    changed, shared, failed = [], 0, 0
    for p, real in plan:
        if args.limit is not None and len(changed) >= args.limit:
            break
        cid = p["id"].rsplit("/", 1)[1]
        resp = sync.shopify_request("PUT", f"https://{sync.SHOPIFY_SHOP_URL}/admin/api/{sync.SHOPIFY_API_VERSION}/customers/{cid}.json",
                                    json={"customer": {"id": int(cid), "email": real}})
        if resp.status_code in (200, 201):
            changed.append({"customer_id": cid, "receipt": p["receipt"]})
        elif resp.status_code == 422:
            shared += 1                      # the real email already belongs to another customer
        else:
            failed += 1
            print(f"  [!] Etsy #{p['receipt']}: Shopify said {resp.status_code}: {resp.text[:160]}")
        time.sleep(0.25)
    print(f"\nCustomers given their real email: {len(changed)}")
    print(f"Real email already on another customer (left alone): {shared}")
    print(f"Failed: {failed}")
    if changed:
        os.makedirs(sync.LOG_DIR, exist_ok=True)
        path = os.path.join(sync.LOG_DIR, f"placeholder_email_replace_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"changed": changed}, f, indent=1)
        print(f"Written: {path}")


if __name__ == "__main__":
    main()
