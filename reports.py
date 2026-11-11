"""Simple reports from orders.db. Reads the local database only; nothing is sent anywhere.

    python reports.py                    # summary, monthly sales, top products, repeat customers
    python reports.py --months 36        # show more months
    python reports.py --export orders.csv   # save every order to a spreadsheet file

Run backfill_orders.py first if you want the shop's full history included.
"""
import argparse
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing

import sync

# Orders that were really sold: everything except cancelled / fully refunded ones.
SOLD = "status NOT IN ('CANCELLED', 'CANCELLED_AFTER_IMPORT') AND order_date IS NOT NULL"
ITEM_LINE = re.compile(r"^(\d+)x (.*?)(?: \[(.*?)\])?(?: \(.*\))?$")


def query(sql, params=()):
    with closing(sqlite3.connect(sync.DB_PATH)) as conn:
        return conn.execute(sql, params).fetchall()


def summary():
    total, revenue, first, last = query(
        f"SELECT COUNT(*), COALESCE(SUM(grand_total), 0), MIN(order_date), MAX(order_date) FROM synced_orders WHERE {SOLD}")[0]
    cancelled = query("SELECT COUNT(*) FROM synced_orders WHERE status IN ('CANCELLED', 'CANCELLED_AFTER_IMPORT')")[0][0]
    print("=== Summary ===")
    if not total:
        print("No order details in the database yet. Run: python backfill_orders.py")
        return False
    print(f"Orders sold:      {total:,}  ({(first or '')[:10]} to {(last or '')[:10]})")
    print(f"Total takings:    £{revenue:,.2f}  (what buyers paid, including postage)")
    print(f"Average order:    £{revenue / total:,.2f}")
    print(f"Cancelled orders: {cancelled:,}")
    return True


def monthly(months):
    rows = query(f"""
        SELECT substr(order_date, 1, 7), COUNT(*), SUM(grand_total), SUM(item_count)
        FROM synced_orders WHERE {SOLD} GROUP BY 1 ORDER BY 1 DESC LIMIT ?""", (months,))
    print(f"\n=== Sales by month (latest {months}) ===")
    print(f"{'Month':<9}{'Orders':>8}{'Items':>8}{'Takings':>13}")
    for month, orders, revenue, items in rows:
        print(f"{month:<9}{orders:>8,}{items or 0:>8,}{'£{:,.2f}'.format(revenue or 0):>13}")


def top_products(limit):
    units, orders, names = Counter(), Counter(), {}
    for (items,) in query(f"SELECT items FROM synced_orders WHERE {SOLD} AND items IS NOT NULL"):
        for line in items.splitlines():
            m = ITEM_LINE.match(line.strip())
            if not m:
                continue
            qty, title, sku = int(m.group(1)), m.group(2), m.group(3)
            key = sku or title
            units[key] += qty
            orders[key] += 1
            names[key] = title
    print(f"\n=== Top {limit} products by units sold (grouped by SKU) ===")
    print(f"{'Units':>7}{'Orders':>8}  Product")
    for key, count in units.most_common(limit):
        label = f"[{key}] {names[key]}" if key != names[key] else names[key]
        print(f"{count:>7,}{orders[key]:>8,}  {label[:90]}")


def repeat_customers(limit):
    groups = defaultdict(lambda: [0, 0.0, "", ""])
    rows = query(f"SELECT buyer_name, postcode, grand_total, order_date FROM synced_orders WHERE {SOLD} AND buyer_name IS NOT NULL")
    for name, postcode, total, order_date in rows:
        key = (name.strip().lower(), (postcode or "").replace(" ", "").upper())
        g = groups[key]
        g[0] += 1
        g[1] += total or 0
        g[2] = name.strip()
        g[3] = max(g[3], (order_date or "")[:10])
    repeats = sorted((g for g in groups.values() if g[0] > 1), key=lambda g: (-g[0], -g[1]))
    print(f"\n=== Repeat customers (same name and postcode): {len(repeats):,} of {len(groups):,} customers ===")
    print(f"{'Orders':>7}{'Spent':>12}  {'Last order':<12}Name")
    for count, spent, name, last in repeats[:limit]:
        print(f"{count:>7}{'£{:,.2f}'.format(spent):>12}  {last:<12}{name}")


def export_csv(path):
    with closing(sqlite3.connect(sync.DB_PATH)) as conn:
        cursor = conn.execute("SELECT * FROM synced_orders ORDER BY order_date DESC")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([col[0] for col in cursor.description])
            count = 0
            for row in cursor:
                writer.writerow(row)
                count += 1
    print(f"Saved {count:,} orders to {path} (contains customer names and addresses - keep it private)")


def main():
    parser = argparse.ArgumentParser(description="Reports from the local orders database.")
    parser.add_argument("--months", type=int, default=13, help="How many months to list (default: 13).")
    parser.add_argument("--top", type=int, default=20, help="How many products / customers to list (default: 20).")
    parser.add_argument("--export", metavar="FILE.csv", help="Save every order to a CSV file for Excel / Google Sheets.")
    args = parser.parse_args()

    sync.init_db()
    if args.export:
        export_csv(args.export)
        return
    if summary():
        monthly(args.months)
        top_products(args.top)
        repeat_customers(args.top)


if __name__ == "__main__":
    main()
