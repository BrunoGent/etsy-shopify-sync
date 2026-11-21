"""Fills orders.db with the shop's past Etsy orders.

Read-only against Etsy (it only downloads receipts) and never touches Shopify.

    python backfill_orders.py --dry-run     # show what would be saved, write nothing
    python backfill_orders.py               # save everything
    python backfill_orders.py --max 500     # only the 500 most recent orders

Rules that keep the normal sync safe:
  * An order already in synced_orders keeps its status, Shopify ID and tracking state;
    only its detail columns (name, address, items, totals, ...) are filled in.
  * An order NOT yet in synced_orders is added only if it is finished on Etsy:
    dispatched -> SHIPPED, cancelled / fully refunded -> CANCELLED.
  * Paid orders still waiting to be dispatched are left out, so sync.py imports them
    into Shopify as normal.

Safe to run as often as you like: rows are updated, never duplicated.
"""
import argparse
import time

import sync

PAGE_SIZE = 100


def fetch_all_receipts(max_receipts=None):
    """Yields every receipt in the shop, newest first, 100 per request."""
    url = f"https://api.etsy.com/v3/application/shops/{sync.ETSY_SHOP_ID}/receipts"
    offset = 0
    fetched = 0
    while max_receipts is None or fetched < max_receipts:
        resp = sync.etsy_request("GET", url, params={"limit": PAGE_SIZE, "offset": offset})
        if resp.status_code == 429:
            print("  Etsy asked us to slow down; waiting 10 seconds...")
            time.sleep(10)
            continue
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch Etsy receipts at offset {offset} ({resp.status_code}): {resp.text}")
        data = resp.json()
        results = data.get("results", [])
        for receipt in results:
            if max_receipts is not None and fetched >= max_receipts:
                return
            fetched += 1
            yield sync.clean_receipt(receipt)
        print(f"  ...read {fetched} of {data.get('count', '?')} orders")
        if len(results) < PAGE_SIZE:
            return
        offset += PAGE_SIZE
        time.sleep(0.5)


def backfill_receipt(receipt, dry_run):
    """Returns what happened to this receipt: updated, added_shipped, added_cancelled or left_for_sync."""
    receipt_id = str(receipt.get("receipt_id"))
    row = sync.get_synced_row(receipt_id)

    if row is not None:
        outcome = "updated"
    elif sync.is_receipt_cancelled(receipt) and not receipt.get("is_shipped"):
        outcome = "added_cancelled"
        if not dry_run:
            sync.record_synced_order(receipt_id, "CANCELLED_ON_ETSY", status="CANCELLED")
    elif receipt.get("is_shipped"):
        outcome = "added_shipped"
        if not dry_run:
            codes = [str(s.get("tracking_code") or "").strip() for s in receipt.get("shipments") or []]
            # tracking_pushed = 1 with no tracking_pushed_at marks it as dispatched before this
            # tool existed, so the tracking sync will never push or re-notify it.
            sync._db_execute("""
                INSERT INTO synced_orders (etsy_receipt_id, shopify_order_id, status, tracking_number, tracking_pushed)
                VALUES (?, '', 'SHIPPED', ?, 1)
            """, (receipt_id, ",".join(c for c in codes if c) or None))
    else:
        return "left_for_sync"

    if not dry_run:
        sync.save_order_details(receipt)
    return outcome


def main():
    parser = argparse.ArgumentParser(description="Fill orders.db with past Etsy orders (read-only on Etsy).")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be saved without writing anything.")
    parser.add_argument("--max", type=int, default=None, help="Only read the N most recent orders (default: all).")
    args = parser.parse_args()

    sync.init_db()
    sync.ensure_etsy_token()

    print(f"--- Backfilling past Etsy orders into orders.db (Dry Run: {args.dry_run}) ---")
    counts = {"updated": 0, "added_shipped": 0, "added_cancelled": 0, "left_for_sync": 0}
    with_email = 0
    for receipt in fetch_all_receipts(args.max):
        counts[backfill_receipt(receipt, args.dry_run)] += 1
        if sync.extract_order_details(receipt)["buyer_email"]:
            with_email += 1

    total = sum(counts.values())
    verb = "Would save" if args.dry_run else "Saved"
    print(f"\n{verb} details for {total - counts['left_for_sync']} of {total} orders:")
    print(f"  Already in database (details filled in): {counts['updated']}")
    print(f"  Added as SHIPPED:                        {counts['added_shipped']}")
    print(f"  Added as CANCELLED:                      {counts['added_cancelled']}")
    print(f"  Left for the normal sync (not yet sent): {counts['left_for_sync']}")
    print(f"  Orders with an email address:            {with_email} of {total}")


if __name__ == "__main__":
    main()
