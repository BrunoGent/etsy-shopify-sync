"""Merge Shopify customer records that belong to the same Etsy buyer.

Each imported Etsy order was created before the buyer's email was known, so a repeat buyer ends up on
several customer records and only one of them holds the email. This finds those buyers and merges the
email-less duplicates into the record that holds the email (full order history, one customer).

    python merge_duplicate_customers.py                  # rehearsal: lists the merges, changes nothing
    python merge_duplicate_customers.py --apply --limit 1     # really merge ONE buyer, then check it
    python merge_duplicate_customers.py --apply          # merge them all

Safeguards (anything that does not pass is listed for you to do by hand, never merged):
  * Same Etsy buyer id (from orders.db), not merely the same name.
  * Same surname and same postcode on every record.
  * Exactly one email in the group (or the same email on each); that record is kept.
  * Shopify's own merge preview must say the kept record survives and report no errors.
  * After each merge the result is checked: the kept customer has every order and still has its email.
Merging cannot be undone in Shopify. Needs the app permissions read_customer_merge and
write_customer_merge (Shopify Dev Dashboard, https://dev.shopify.com/dashboard: release a new version of the
app, then approve it in the Shopify admin under Settings > Apps and sales channels).
Safe to run again: merged customers are one record, so they no longer show up.
"""
import argparse
import collections
import time

import sync

BATCH = 100
ORDER_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Order { id customer { id displayName lastName createdAt numberOfOrders
                                 defaultEmailAddress { emailAddress marketingState }
                                 defaultAddress { zip } } }
  }
}"""
PREVIEW_QUERY = """
query($a: ID!, $b: ID!) {
  customerMergePreview(customerOneId: $a, customerTwoId: $b) {
    resultingCustomerId
    customerMergeErrors { errorFields message }
  }
}"""
MERGE_MUTATION = """
mutation($a: ID!, $b: ID!, $o: CustomerMergeOverrideFields) {
  customerMerge(customerOneId: $a, customerTwoId: $b, overrideFields: $o) {
    resultingCustomerId
    job { id done }
    userErrors { field message }
  }
}"""
JOB_QUERY = "query($id: ID!) { job(id: $id) { id done } }"
CUSTOMER_QUERY = """query($id: ID!) { customer(id: $id) { id numberOfOrders defaultEmailAddress { emailAddress marketingState } } }"""
DASHBOARD = "https://dev.shopify.com/dashboard"


def norm_zip(z):
    return "".join(str(z or "").split()).upper()


def read_records():
    """{buyer id: {customer id: record}} for every imported order, read from Shopify in batches."""
    rows = sync._db_query("""
        SELECT etsy_receipt_id, shopify_order_id, buyer_user_id FROM synced_orders
        WHERE shopify_order_id GLOB '[0-9]*' AND status IN ('SUCCESS', 'EXISTING_IN_SHOPIFY', 'SHIPPED')
          AND COALESCE(buyer_user_id, '') <> ''""")
    buyer_of = {str(r[1]): str(r[2]) for r in rows}
    ids = list(buyer_of)
    groups = collections.defaultdict(dict)
    for i in range(0, len(ids), BATCH):
        data = sync.shopify_graphql(ORDER_QUERY, {"ids": [f"gid://shopify/Order/{x}" for x in ids[i:i + BATCH]]})
        for node in data.get("nodes") or []:
            if not node or not node.get("customer"):
                continue
            c = node["customer"]
            email = ((c.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip().lower()
            groups[buyer_of[node["id"].rsplit("/", 1)[1]]][c["id"]] = {
                "id": c["id"], "name": c.get("displayName") or "", "last": (c.get("lastName") or "").strip().lower(),
                "created": c.get("createdAt") or "", "orders": int(c.get("numberOfOrders") or 0), "email": email,
                "zip": norm_zip((c.get("defaultAddress") or {}).get("zip"))}
        time.sleep(0.2)
    return groups


def plan_merges(groups):
    """-> (plans, skipped). plan = {'buyer', 'keep', 'merge': [records]}; skipped = [(buyer, reason, records)]."""
    plans, skipped = [], []
    for buyer, recs in groups.items():
        recs = list(recs.values())
        if len(recs) < 2:
            continue
        emails = {r["email"].lower() for r in recs if r["email"]}   # the same address in a different case is the same email
        if len({r["last"] for r in recs}) != 1 or not recs[0]["last"]:
            skipped.append((buyer, "different surnames", recs))
        elif len({r["zip"] for r in recs}) != 1 or not recs[0]["zip"]:
            skipped.append((buyer, "different or missing postcodes", recs))
        elif len(emails) != 1:
            skipped.append((buyer, "no email, or different emails, in the group", recs))
        else:
            holders = sorted((r for r in recs if r["email"]), key=lambda r: r["created"])
            keep = holders[0]                                   # the oldest record that holds the email
            plans.append({"buyer": buyer, "keep": keep, "merge": [r for r in recs if r["id"] != keep["id"]]})
    return plans, skipped


REC_FIELDS = "id displayName lastName createdAt numberOfOrders defaultEmailAddress { emailAddress } defaultAddress { zip }"


def to_record(c):
    email = ((c.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip().lower()
    return {"id": c["id"], "name": c.get("displayName") or "", "last": (c.get("lastName") or "").strip().lower(),
            "created": c.get("createdAt") or "", "orders": int(c.get("numberOfOrders") or 0), "email": email,
            "zip": norm_zip((c.get("defaultAddress") or {}).get("zip"))}


def plan_placeholder_merges():
    """OrderBridge placeholder customers (etsy-<order>@placeholder...) whose real, captured email already sits on
    ANOTHER customer: merge the placeholder record into that one. Same safeguards as plan_merges (same surname and
    postcode). -> (plans, skipped) in the same shape as plan_merges."""
    import replace_placeholder_emails as rp
    try:
        by_buyer = {str(r[0]): r[1] for r in sync._db_query("SELECT buyer_id, email FROM buyer_emails WHERE email <> ''")}
    except Exception:
        by_buyer = {}
    by_receipt = {str(r[0]): r[1] for r in sync._db_query("SELECT receipt_id, email FROM etsy_emails WHERE email <> ''")}

    placeholders = []
    for c in rp.all_customers():
        mt = rp.PLACEHOLDER_RE.match(((c.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip().lower())
        if mt:
            placeholders.append((c["id"], mt.group(1)))
    recs = {}
    ids = [p[0] for p in placeholders]
    for i in range(0, len(ids), BATCH):
        data = sync.shopify_graphql("query($ids: [ID!]!) { nodes(ids: $ids) { ... on Customer { %s } } }" % REC_FIELDS,
                                    {"ids": ids[i:i + BATCH]})
        for n in data["nodes"]:
            if n:
                recs[n["id"]] = to_record(n)
        time.sleep(0.2)

    holders_of, by_keeper, skipped = {}, collections.OrderedDict(), []
    for pid, receipt in placeholders:
        real = rp.real_email_for(receipt, by_receipt, by_buyer)
        p = recs.get(pid)
        if not real or not p:
            continue                                     # no captured email: nothing to merge with
        key = real.lower()
        if key not in holders_of:
            nodes = sync.shopify_graphql("query($q: String!) { customers(first: 5, query: $q) { nodes { %s } } }" % REC_FIELDS,
                                         {"q": f"email:{real}"})["customers"]["nodes"]
            holders_of[key] = [to_record(n) for n in nodes if to_record(n)["email"] == key]
            time.sleep(0.2)
        holders = [h for h in holders_of[key] if h["id"] != pid]
        if not holders:
            continue                                     # the real email is on no other customer: replace_placeholder_emails.py
        keep = sorted(holders, key=lambda h: h["created"])[0]
        if not p["last"] or p["last"] != keep["last"]:
            skipped.append((receipt, "different surnames", [keep, p]))
        elif not p["zip"] or p["zip"] != keep["zip"]:
            skipped.append((receipt, "different or missing postcodes", [keep, p]))
        else:
            by_keeper.setdefault(keep["id"], {"buyer": receipt, "keep": keep, "merge": []})["merge"].append(p)
    return list(by_keeper.values()), skipped


def short(gid):
    return gid.rsplit("/", 1)[1]


def preview(keep_id, dup_id):
    """Shopify's own preview. Returns (argument order that keeps `keep_id`, problem text or None)."""
    for a, b in ((keep_id, dup_id), (dup_id, keep_id)):
        p = sync.shopify_graphql(PREVIEW_QUERY, {"a": a, "b": b})["customerMergePreview"]
        if p["customerMergeErrors"]:
            return None, f"Shopify says: {p['customerMergeErrors']}"
        if p["resultingCustomerId"] == keep_id:
            return (a, b), None
    return None, "Shopify's preview would not keep the record that holds the email"


def wait_for_job(job):
    if not job or job.get("done"):
        return True
    for _ in range(20):
        time.sleep(1.5)
        if sync.shopify_graphql(JOB_QUERY, {"id": job["id"]})["job"]["done"]:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="Merge Shopify customer records of the same Etsy buyer.")
    ap.add_argument("--apply", action="store_true", help="really merge (default: rehearsal)")
    ap.add_argument("--limit", type=int, default=None, help="stop after merging this many duplicate records")
    ap.add_argument("--placeholders", action="store_true",
                    help="instead: merge OrderBridge placeholder customers into the customer that already holds their real email")
    args = ap.parse_args()

    sync.init_db()
    if args.placeholders:
        print("Looking for OrderBridge placeholder customers whose real email already sits on another customer...")
        plans, skipped = plan_placeholder_merges()
        n_dups = sum(len(p["merge"]) for p in plans)
        print(f"Can be merged safely: {len(plans)} customers keep ({n_dups} placeholder records merged in). "
              f"Left for you to check by hand: {len(skipped)}.\n")
    else:
        print("Reading customers of every imported order from Shopify...")
        groups = read_records()
        plans, skipped = plan_merges(groups)
        n_dups = sum(len(p["merge"]) for p in plans)
        print(f"{len(groups)} Etsy buyers; {len(plans) + len(skipped)} are split across several customer records.")
        print(f"Can be merged safely: {len(plans)} buyers ({n_dups} duplicate records). Left for you to check by hand: {len(skipped)}.\n")
    for p in plans:
        keep = p["keep"]
        print(f"  KEEP  #{short(keep['id'])} {keep['name']} (since {keep['created'][:10]}, {keep['orders']} order(s), has email)")
        for d in p["merge"]:
            print(f"  merge #{short(d['id'])} {d['name']} (since {d['created'][:10]}, {d['orders']} order(s))")
    for buyer, reason, recs in skipped:
        print(f"  SKIP  {recs[0]['name']}: {reason} (customers {', '.join('#' + short(r['id']) for r in recs)})")

    # Shopify's preview needs read_customer_merge; say so plainly if it is not there yet.
    try:
        if plans:
            sync.shopify_graphql(PREVIEW_QUERY, {"a": plans[0]["keep"]["id"], "b": plans[0]["merge"][0]["id"]})
    except Exception as e:
        if "ACCESS_DENIED" in str(e) or "access scope" in str(e):
            print(f"\nShopify's merge permission is NOT granted to the app yet, so nothing can be previewed or merged.\n"
                  f"Add read_customer_merge and write_customer_merge at {DASHBOARD} (release a new version, then approve it in the\n"
                  f"Shopify admin: Settings > Apps and sales channels).")
            return
        raise

    done = merged = 0
    for p in plans:
        keep = p["keep"]
        for d in p["merge"]:
            order, problem = preview(keep["id"], d["id"])
            tag = f"#{short(d['id'])} into #{short(keep['id'])}"
            if problem:
                print(f"  [!] {tag}: not merged. {problem}")
                continue
            if not args.apply:
                print(f"  [REHEARSAL OK] {tag}: Shopify's preview keeps #{short(keep['id'])} and reports no errors.")
                continue
            if args.limit is not None and merged >= args.limit:
                break
            overrides = {"customerIdOfEmailToKeep": keep["id"], "customerIdOfFirstNameToKeep": keep["id"],
                         "customerIdOfLastNameToKeep": keep["id"],
                         "customerIdOfDefaultAddressToKeep": max((keep, d), key=lambda r: r["created"])["id"]}
            res = sync.shopify_graphql(MERGE_MUTATION, {"a": order[0], "b": order[1], "o": overrides})["customerMerge"]
            if res["userErrors"] or res["resultingCustomerId"] != keep["id"]:
                print(f"  [!] {tag}: Shopify refused: {res['userErrors']}")
                continue
            if not wait_for_job(res["job"]):
                print(f"  [!] {tag}: merge still running after 30 s; check Shopify, then run again.")
                continue
            after = sync.shopify_graphql(CUSTOMER_QUERY, {"id": keep["id"]})["customer"]
            ok = int(after["numberOfOrders"]) == keep["orders"] + d["orders"] and bool((after["defaultEmailAddress"] or {}).get("emailAddress"))
            state = (after["defaultEmailAddress"] or {}).get("marketingState")
            print(f"  [{'+' if ok else '!!!'}] merged {tag}: now {after['numberOfOrders']} order(s), email kept, marketing {state}"
                  + ("" if ok else "  <-- CHECK THIS CUSTOMER IN SHOPIFY"))
            keep["orders"] += d["orders"]
            merged += 1
            done += 1
            time.sleep(0.5)
        if args.apply and args.limit is not None and merged >= args.limit:
            break
    print()
    print(f"Merged: {merged}" if args.apply else "REHEARSAL only: nothing was changed. Add --apply (try --limit 1 first) to merge.")


if __name__ == "__main__":
    main()
