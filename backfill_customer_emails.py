"""One-off: add captured buyer emails to the Shopify CUSTOMER records of orders that are already dispatched.

The normal sync only fills in emails on OPEN orders. Customers on older, dispatched orders were created
before the email was known, so they show "No email address provided". This adds the email we captured
(from the Etsy Orders pages via email_capture/) to those customers.

    python backfill_customer_emails.py                 # rehearsal: reads Shopify, changes nothing
    python backfill_customer_emails.py --apply --limit 25    # change the first 25 customers only
    python backfill_customer_emails.py --apply         # change all of them

What it does and does not do:
  * Only a customer with NO email gets one. An existing email is never replaced.
  * Customer records only: the orders themselves are not edited (no order-updated events).
  * Marketing consent is never sent or changed, and Shopify sends nobody anything.
  * If the email already belongs to another Shopify customer (a repeat buyer whose orders ended up on
    separate customer records), that customer is left alone and counted as "shared".
  * Safe to run again: customers that now have an email are skipped.
  * Every change is written to logs/customer_email_backfill_<time>.json (customer id + receipt, no email)
    so it can be traced or undone.
"""
import argparse
import json
import os
import time
from datetime import datetime

import sync

BATCH = 100
ORDER_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Order { id customer { id defaultEmailAddress { emailAddress } } }
  }
}"""


def captured_emails():
    try:
        by_buyer = {str(r[0]): r[1] for r in sync._db_query("SELECT buyer_id, email FROM buyer_emails WHERE email <> ''")}
    except Exception:
        by_buyer = {}
    by_receipt = {str(r[0]): r[1] for r in sync._db_query("SELECT receipt_id, email FROM etsy_emails WHERE email <> ''")}
    return by_receipt, by_buyer


def customers_of(order_ids):
    """{order id: (customer numeric id or None, current email)} read from Shopify in batches."""
    found = {}
    ids = list(order_ids)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        data = sync.shopify_graphql(ORDER_QUERY, {"ids": [f"gid://shopify/Order/{x}" for x in chunk]})
        for node in data.get("nodes") or []:
            if not node:
                continue
            order_id = node["id"].rsplit("/", 1)[1]
            cust = node.get("customer")
            if cust:
                email = ((cust.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip()
                found[order_id] = (cust["id"].rsplit("/", 1)[1], email)
            else:
                found[order_id] = (None, "")
        time.sleep(0.2)
    return found


def main():
    ap = argparse.ArgumentParser(description="Add captured buyer emails to Shopify customers of dispatched orders.")
    ap.add_argument("--apply", action="store_true", help="really change Shopify (default: rehearsal, changes nothing)")
    ap.add_argument("--limit", type=int, default=None, help="stop after changing this many customers")
    args = ap.parse_args()

    sync.init_db()
    by_receipt, by_buyer = captured_emails()
    rows = sync._db_query("""
        SELECT etsy_receipt_id, shopify_order_id, buyer_user_id FROM synced_orders
        WHERE status = 'SHIPPED' AND shopify_order_id GLOB '[0-9]*' ORDER BY order_date DESC""")
    plan = []
    for rid, sid, uid in rows:
        email = (by_receipt.get(str(rid)) or by_buyer.get(str(uid)) or "").strip()
        if email and "@" in email:
            plan.append((str(rid), str(sid), email))
    print(f"{len(rows)} dispatched orders are in Shopify; we hold a captured email for {len(plan)} of them.")
    print("Reading their customers from Shopify...")
    current = customers_of([p[1] for p in plan])

    counts = {"has_email": 0, "no_customer": 0, "not_found": 0, "to_fill": 0, "filled": 0, "shared": 0, "failed": 0}
    assigned = {}            # email -> customer id it was given to in this run
    changed, shared_receipts = [], []
    for rid, sid, email in plan:
        if sid not in current:
            counts["not_found"] += 1
            continue
        cid, cur_email = current[sid]
        if not cid:
            counts["no_customer"] += 1
            continue
        if cur_email:
            counts["has_email"] += 1
            continue
        counts["to_fill"] += 1
        if not args.apply:
            continue
        if args.limit is not None and counts["filled"] >= args.limit:
            break
        key = email.lower()
        if key in assigned and assigned[key] != cid:
            counts["shared"] += 1                 # the same buyer's other customer record already took this email
            shared_receipts.append(rid)
            continue
        resp = sync.shopify_request("PUT", f"https://{sync.SHOPIFY_SHOP_URL}/admin/api/{sync.SHOPIFY_API_VERSION}/customers/{cid}.json",
                                    json={"customer": {"id": int(cid), "email": email}})
        if resp.status_code in (200, 201):
            counts["filled"] += 1
            assigned[key] = cid
            changed.append({"receipt_id": rid, "order_id": sid, "customer_id": cid})
            sync._db_execute("UPDATE synced_orders SET buyer_email = ? WHERE etsy_receipt_id = ? AND COALESCE(buyer_email, '') = ''", (email, rid))
        elif resp.status_code == 422:
            counts["shared"] += 1                 # Shopify: that email already belongs to another customer
            shared_receipts.append(rid)
        else:
            counts["failed"] += 1
            print(f"  [!] Etsy #{rid}: Shopify said {resp.status_code}: {resp.text[:160]}")
        time.sleep(0.25)

    print()
    print(f"Customers that already have an email (left alone): {counts['has_email']}")
    print(f"Orders with no customer record (skipped):          {counts['no_customer']}")
    if args.apply:
        print(f"Customers given an email:                          {counts['filled']}")
        print(f"Email already on another customer (left alone):    {counts['shared']}")
        print(f"Failed:                                            {counts['failed']}")
        if changed:
            os.makedirs(sync.LOG_DIR, exist_ok=True)
            path = os.path.join(sync.LOG_DIR, f"customer_email_backfill_{datetime.now():%Y%m%d_%H%M%S}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"changed": changed, "shared_receipts": shared_receipts}, f, indent=1)
            print(f"Written: {path}")
    else:
        print(f"REHEARSAL: {counts['to_fill']} customers would be given an email. Nothing was changed. Add --apply to do it.")


if __name__ == "__main__":
    main()
