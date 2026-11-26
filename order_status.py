"""Look up one Etsy order and show where it stands everywhere. Read-only: it changes nothing.

    python order_status.py 4246519963

It combines three sources: this tool's own database (orders.db), the Shopify order (label and tracking
number) and Etsy (is it dispatched). At the end it says what you can do next. To send the tracking to
Etsy for just that order, use:  python sync.py --only-receipt 4246519963 [--force-dispatch] [--dry-run]
"""
import argparse
import sys
from datetime import date

import sync


def find_shopify_order(receipt_id):
    url = f"https://{sync.SHOPIFY_SHOP_URL}/admin/api/{sync.SHOPIFY_API_VERSION}/orders.json"
    params = {"status": "any", "name": receipt_id, "limit": 5,
              "fields": "id,name,note,tags,fulfillment_status,fulfillments,financial_status,cancelled_at,created_at"}
    for o in sync.shopify_get_all(url, params, "orders", max_items=5):
        if sync.extract_etsy_receipt_id(o) == receipt_id:
            return o
    return None


def describe(receipt_id):
    """Prints the status report; returns the list of things that can be done."""
    row = sync.get_synced_row(receipt_id)
    print(f"ORDER {receipt_id}")
    if row:
        cols = ["buyer_name", "order_date", "items", "grand_total", "currency"]
        got = sync._db_query(f"SELECT {', '.join(cols)} FROM synced_orders WHERE etsy_receipt_id = ?", (receipt_id,))[0]
        pick = dict(zip(cols, got)).get
        print(f"  Customer:      {pick('buyer_name') or '(unknown)'}")
        print(f"  Bought:        {pick('order_date') or '(unknown)'}")
        print(f"  Items:         {pick('items') or '(unknown)'}")
        if pick("grand_total") is not None:
            print(f"  Total:         {pick('grand_total')} {pick('currency') or ''}")
        print(f"  Our database:  {row['status']}")
    else:
        print("  Not in this tool's database (never imported, or not an Etsy order of this shop).")

    shop = find_shopify_order(receipt_id)
    if shop:
        tags = shop.get("tags") or "(none)"
        print(f"  Shopify:       order found (ID {shop['id']}), fulfilment: {shop.get('fulfillment_status') or 'not fulfilled'}, tags: {tags}")
        parcels = sync.collect_parcels(shop)
        for code, company in parcels:
            print(f"  Label:         {code} ({company})")
        if not parcels:
            print("  Label:         none yet (no tracking number on the Shopify order)")
    else:
        parcels = []
        print("  Shopify:       no order found with that number (Shopify only shows the last 60 days)")

    sync.ensure_etsy_token()
    etsy = sync.get_etsy_shipment_state(receipt_id)
    if etsy is None:
        print("  Etsy:          could not be read just now")
    else:
        if etsy["cancelled"]:
            print("  Etsy:          CANCELLED / REFUNDED - do not ship")
        elif etsy["is_shipped"]:
            print(f"  Etsy:          dispatched{' with ' + ', '.join(sorted(etsy['codes'])) if etsy['codes'] else ''}")
        else:
            print("  Etsy:          paid, not dispatched yet")

    state = sync.get_tracking_state(receipt_id) if row else {"codes": set(), "legacy_done": False}
    due = sync.get_dispatch_due_date(receipt_id, shop or {}) if row or shop else None
    if due:
        days = (due - date.today()).days
        when = "today" if days == 0 else (f"in {days} day(s)" if days > 0 else f"{-days} day(s) ago")
        print(f"  Ship by:       {due.strftime('%a %d %b %Y')} ({when})")
    print(f"  Tracking sent: {', '.join(sorted(state['codes'])) if state['codes'] else ('yes (older order)' if state['legacy_done'] else 'no')}")

    print("\nWHAT YOU CAN DO")
    todo = []
    if etsy and etsy["cancelled"]:
        print("  - Nothing. The order is cancelled on Etsy.")
    elif etsy and etsy["is_shipped"]:
        print("  - Nothing. Etsy already shows it dispatched.")
    elif not parcels:
        print("  - Buy the Royal Mail label in Shopify first; then the tracking can be sent.")
    elif state["codes"] or state["legacy_done"]:
        print("  - Nothing. The tracking was already sent.")
    else:
        todo.append("send")
        if due and date.today() < due:
            print(f"  - Send the tracking to Etsy now (early, ship-by is {due.strftime('%d %b')}): tick 'Send even if the ship-by date has not arrived'.")
        else:
            print("  - Send the tracking to Etsy now.")
    return todo


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show the status of one Etsy order (read-only).")
    parser.add_argument("receipt", help="the Etsy order number, e.g. 4246519963")
    args = parser.parse_args(argv)
    receipt = args.receipt.strip().lstrip("#")
    if not receipt.isdigit():
        sys.exit("The order number should be digits only, e.g. 4246519963")
    sync.init_db()
    describe(receipt)


if __name__ == "__main__":
    main()
