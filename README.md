# Etsy ↔ Shopify Sync

A free, local tool for sellers who take orders on **Etsy** but pack, print labels and manage stock in **Shopify**.

It copies new Etsy orders into Shopify, and when you buy a shipping label in Shopify it sends the tracking number back to Etsy and marks the order as dispatched. It runs on your own PC, when you tell it to. There is no subscription, no server and no third party holding your customer data.

Built for a UK shop shipping with Royal Mail, but the carrier names and shipping rules are a few lines of Python and easy to change.

> **Use at your own risk.** This is a personal tool shared in case it helps other sellers. It is not affiliated with Etsy or Shopify. Always try `--dry-run` first.

---

## What it does

### 1. Etsy → Shopify: order import

`python sync.py --import-orders` (or double-click `run_sync_orders.bat`)

- Fetches every paid, not-yet-dispatched Etsy order.
- Creates a matching order in Shopify, marked as paid, with:
  - line items linked to your Shopify products **by SKU**, including options and personalisation text
  - the buyer's name and delivery address
  - the original Etsy purchase date
  - the Etsy discount, so the Shopify total equals what the buyer actually paid
  - the **gift message** and the buyer's message in the order **notes** (a gift order with no message gets only the tag `Gift`; one with a message also gets the tag `Gift message` and the note line `GIFT MESSAGE (print from Etsy):`). A plain order has no note. The order is named with the Etsy receipt number, which is how the sync finds it again
  - tags: `Etsy`, `ShipBy:YYYY-MM-DD`, plus `Tracked 24, Priority` for upgraded postage, `Gift`, `International`
  - **repeat buyers:** `Repeat customer` and `Repeat order N` (N = this buyer's Nth order). Etsy gives no buyer email, so Shopify cannot tell a buyer has ordered before; the sync works it out from its own order history instead (see [Repeat customers](#repeat-customers))
- Sets Shopify's **"Fulfill by"** date from Etsy's dispatch deadline, so you can sort by what is due.
- **Never creates duplicates.** Each order is checked against the local database and against recent Shopify orders.
- **Handles cancellations.**
  - Cancelled and fully refunded Etsy orders are not imported.
  - If an order is cancelled on Etsy *after* it was imported, you get a `DO NOT SHIP` warning and the Shopify order is tagged `Etsy Cancelled`.
- Buyers are created as **not subscribed** to email marketing, and Shopify sends them no order or shipping emails (Etsy already does).

### 2. Shopify → Etsy: tracking and dispatch

`python sync.py` (or double-click `run_tracking.bat`)

- Looks at recent Shopify orders that came from Etsy and have a tracking number.
- Sends the tracking number and carrier to Etsy, which marks the order dispatched and emails the buyer.
- **Holds dispatch until the ship-by date.** If you buy labels early, the order is not marked dispatched on Etsy until its Etsy ship-by date. Every run lists what is being held and until when. Use `--force-dispatch` to send everything now.
  - Because the tool only runs when you start it, **you must run it on the ship-by date**, otherwise Etsy will show the order as late.
- **Never dispatches twice.** Before sending, it asks Etsy whether the order is already dispatched.
- **Optional note to the buyer** sent with the dispatch email (see [Dispatch note](#dispatch-note)).
- Supports several parcels per order: each tracking number is sent once.

### 3. Etsy → Shopify: labels bought on Etsy

Runs automatically at the start of every sync, with either shortcut.

- If you buy a label on Etsy instead of Shopify (or mark an order dispatched there by hand), the Shopify copy of the order is marked **fulfilled**, with the tracking number Etsy holds.
- Shopify sends the buyer no email for this.
- The order is recorded as shipped, so that tracking number is never sent back to Etsy.
- `--dry-run` lists the orders that would be marked fulfilled without changing Shopify.

### 4. A local order database

Everything is recorded in `orders.db` (SQLite), one row per Etsy order: status, Shopify order ID, tracking, order date, buyer name, address, items, totals, discount, postage upgrade, gift details and dispatch date.

- `python backfill_orders.py` downloads your **whole Etsy order history** into it. It only reads from Etsy and never touches Shopify. Safe to re-run.
- `python reports.py` prints sales by year and month, best-selling products (packs sold and total pieces, e.g. 10 packs of 20 tealights = 200 pieces), top customers by spend, and repeat customers. `--top N` sets the list length.
- `python reports.py --export orders.csv` saves every order to a spreadsheet file.

Etsy's API does not provide buyer email addresses, so the email column starts empty. Nothing in the sync needs it.

**Optional: capture buyer emails as you browse (`email_server.py` + `email_capture/`).** Etsy shows the buyer's email on your own Orders page. The Chrome extension in `email_capture/` reads what is already on the page you have open and sends it to a listener on your own PC. It makes no requests to Etsy and does not click or visit anything; it only sees pages you open yourself.
1. Run `python email_server.py` and leave it open (it only accepts connections from this PC and only from the extension).
2. In Chrome: `chrome://extensions` -> Developer mode -> Load unpacked -> choose the `email_capture` folder.
3. Open Etsy -> Orders. Each page you view (use the 50-per-page setting) is captured. `python email_server.py --stats` shows the totals, and `python email_server.py --missing` lists the next 25 highest-spending customers who still have no email (always 25, so the list refills as you capture) (search each name in Orders to capture it; Etsy only lists about the last two months of orders, so older buyers need that search).
4. After changing the extension, click its reload arrow on `chrome://extensions` and refresh (F5) the Etsy tab. Search results are captured too (the page rebuilds without reloading; the extension rescans every few seconds and retries if the listener was not running). Press F12 -> Console on the Etsy tab to see `[BeeMine email capture] saved N order(s)`.

Emails land in `etsy_emails` (per order, which the sync already reads) and `buyer_emails` (per Etsy buyer, for linking repeat customers later). Use them for fulfilment and customer service; marketing needs the customer's consent.

### 5. Housekeeping on every run

- **Due list:** orders due today or overdue that are still not dispatched on Etsy.
- **Report order:** all reports come together at the very end of the run (RESULT, due list, buyer emails, gift messages, Northern Ireland). RESULT lists only counts that are not zero; warnings (failed, cancelled after import, held, NI on hold) appear in colour and only when there is something to see.
- **Buyer emails:** one line saying how many of the current orders (imported, not yet dispatched) have a buyer email saved, listing any without one (see the optional email capture below).
- **Item weight:** items whose SKU is not a Shopify product are imported with the weight (grams) listed for that SKU in `sku_weights.json` (not case sensitive), so the label popup shows it instead of 0 kg. The run prints `Weight N g set for SKU ...` for each such item. Items linked to a Shopify product use the product's own weight. New imports only; the `[Weights]` report lists SKUs missing from the file.
- **SKU matching** ignores capitals (`1KG BW` = `1kg bw`). If two Shopify variants have SKUs that differ only by capitals, the sync warns and leaves orders with that SKU unlinked rather than guess.
- **Weights report:** after an import, items that are not Shopify products and got no weight are listed (yellow, `[Weights]`) so you can add the SKU to `sku_weights.json`. When a SKU is on several Shopify variants, an active product wins over an archived one.
- **eBay / web-store orders (for costing):** every run copies all non-Etsy Shopify orders into `orders.db` (read-only on Shopify): table `channel_orders` (channel `ebay` / `web` / `shopify_draft_order`, dates, buyer name, email, phone, address, items, totals, shipping, discount, tax, refunded, tracking) and `channel_order_items` (one row per line: SKU, quantity, unit price, discount, grams) so costing can total per SKU across channels. Only changes since the last run are read. Shopify returns just the last 60 days unless the app has the `read_all_orders` scope, so older history needs that scope adding in the Dev Dashboard. `--channel-full` re-reads everything Shopify will give. eBay's own selling fees are not in Shopify. Contains personal data: same care as the Etsy details.
- **Late emails:** an open Shopify order created before the buyer's email was captured gets the email added at the end of the next run (order and customer; only fills a blank, never changes marketing consent, so a customer who subscribes on the store stays subscribed). If the email already belongs to another Shopify customer the order gets the email but is not linked; merge those by hand in Shopify; the run prints that order's Etsy number (`[i] Etsy #...`). It checks Shopify itself on every run (until 25 Nov 2026 it trusted the local database, so emails saved here could miss Shopify). The normal run leaves already-dispatched orders alone; to fill the customers on those, run `python backfill_customer_emails.py` (rehearsal) then `python backfill_customer_emails.py --apply`. It only fills customers that have no email, edits customers only (not orders), never touches marketing consent, and writes what it changed to `logs/`. Repeat buyers whose email already sits on another Shopify customer are left alone (merge them by hand).
- **Repeat buyers join their existing customer:** when a new order is imported for an Etsy buyer the sync has seen before, it attaches the order to that buyer's existing Shopify customer instead of creating a duplicate (so Shopify shows one customer with all their orders). Only when it is safely the same person: the same Etsy buyer id, the same surname and postcode, and no conflicting email. If Shopify refuses, the order is created the old way. `--no-link-customers` turns it off. Customers created before this can be joined with `merge_duplicate_customers.py`.
- **Combine:** open orders from the same buyer to the same address (any gap in time, until dispatched) are listed in a yellow box and tagged `Combine` / `Combine with #...` in Shopify, so you can buy one label and give both Etsy orders the same tracking. Nothing is combined automatically.
- **Gift messages:** at the very end, a box lists imported orders with a gift message that are not yet dispatched, as a reminder to print the gift message from Etsy.
- **Run log:** everything printed is also saved to `logs/YYYY-MM-DD.log` (kept for 90 days).
- **Backup:** `orders.db` is copied to a `backups` folder as `orders-YYYY-MM-DD.db` and the newest 30 are kept. You can point this at a cloud folder instead (see [Backups](#backups)).

---

## A typical day

1. **Before packing:** run `run_sync_orders.bat`. New Etsy orders appear in Shopify. Orders for Northern Ireland are imported **on hold** (so they are not on your pack list) and listed in a boxed warning at the end of the run. Buy those labels on Etsy and dispatch them there; the Shopify order then completes itself.
2. Pack, and buy your labels in Shopify as usual.
3. **After buying labels:** run `run_tracking.bat`. Etsy orders due today are marked dispatched; ones bought early are held.

### Optional: run it automatically every day

`run_sync_scheduled.bat` runs the normal import (`python sync.py --import-orders --exclude-ni`) with no pause, and `run_sync_hidden.vbs` starts it without showing a window. Point a **Windows Task Scheduler** task at `wscript.exe "<folder>\run_sync_hidden.vbs"` (for example daily at 08:00) to have the sync run by itself. It does exactly what the manual run does, including marking orders dispatched on Etsy once their ship-by date arrives.

- Nothing is shown on screen. Read `logs/YYYY-MM-DD.log` to see what happened, and look for the Northern Ireland warning there.
- Task Scheduler only runs it while you are signed in to the PC (unless you set the task up otherwise).
- Do not start a manual run at the same moment as a scheduled one.
- `run_sync_hidden.vbs` contains the folder path `C:\beemine-automations\etsy-shopify-sync`. Edit it if you keep the project somewhere else.

---

## Repeat customers

Etsy marks repeat buyers with a star; Shopify cannot, because Etsy hides buyer emails and Shopify matches customers by email. So the sync does it:

- Every order saved in `orders.db` keeps Etsy's `buyer_user_id` (an id Etsy does share). `backfill_orders.py` fills it in for past orders.
- When an order is imported, the sync counts the same buyer's **earlier, non-cancelled** orders in `orders.db` (and any other orders in the same import run). If there are any, the Shopify order gets the tags `Repeat customer` and `Repeat order N`. A receipt with no buyer id falls back to buyer name + postcode.
- Only Etsy orders are compared; other channels are not matched.
- `python sync.py --tag-repeats` (add `--dry-run` to preview) tags orders that were imported earlier and are not yet dispatched.
- To print it on the packing slip, the shop's packing slip template reads these tags; it is kept in its own repository, `shopify-packing-slip` (see `docs/shopify-packing-slip-repeat.txt`).

## Commands

Day to day you only need the two `.bat` shortcuts. Everything else is typed into a terminal.

### All the tools at a glance

Every script in this folder. "Changes anything?" says what it can alter. Every tool that changes Shopify does a **rehearsal by default** (it only shows what it would do); you must add `--apply` to make it act.

| Tool | What it is for | Changes anything? | Safe first step |
| --- | --- | --- | --- |
| `run_sync_orders.bat` (menu: Sync) | The daily run: bring in new Etsy orders, put Northern Ireland ones on hold, send tracking | Yes: Shopify, Etsy, `orders.db` | `python sync.py --import-orders --dry-run` |
| `run_tracking.bat` | Send tracking to Etsy only | Yes: Etsy, `orders.db` | `python sync.py --tracking-only --dry-run` |
| `Force tracking.bat` | Send tracking now, ignoring ship-by dates | Yes: Etsy | add `--dry-run` |
| `sync.py` | The program behind those three; every option is listed below | Depends on options | `--dry-run` |
| `backfill_orders.py` | Download your whole Etsy order history into `orders.db` | Only `orders.db` (reads Etsy) | `--dry-run` |
| `reports.py` | Sales summary, best sellers, repeat customers, CSV export | No (reads `orders.db`; `--export` writes a file) | none needed |
| `email_server.py` + `email_capture/` | Saves buyer emails shown on your Etsy Orders pages | Only `orders.db` | `--stats` |
| `backfill_customer_emails.py` | One-off: add captured emails to Shopify customers of dispatched orders | Yes: Shopify customers | run it with no options |
| `replace_placeholder_emails.py` | One-off: replace OrderBridge's made-up emails with real captured ones | Yes: Shopify customers | run it with no options |
| `merge_duplicate_customers.py` | Merge a repeat buyer's duplicate Shopify customers (`--placeholders`: OrderBridge ones) | Yes: Shopify customers, **cannot be undone** | run it with no options |
| `auth.py` | One-time Etsy sign-in | Only `.env` (tokens) | none |

### How to type a command

1. Open the folder containing `sync.py` in File Explorer.
2. Click the address bar, type `cmd` and press Enter. A terminal opens, already in the right folder.
3. Type the command and press Enter.

Every command has the same shape:

```
python  sync.py  --import-orders  --dry-run
   │       │            └──────┬───────┘
   │       │                 options
   │       └─ which script to run
   └─ always first
```

**Options** are the words starting with two dashes. Each one switches a behaviour on. A few rules:

- Two dashes, no space after them, and dashes (not spaces) between the words: `--dry-run`, not `-- dry run` or `—dry-run`.
- Put a space between each option.
- The order does not matter. `--import-orders --dry-run` and `--dry-run --import-orders` are the same.
- You can combine as many as you need.
- Some options take a value, written after a space: `--limit 1000`.
- Leaving an option out means "behave normally".
- `python sync.py --help` lists every option. This works for the other scripts too.

### Options for `sync.py`

With **no options at all**, `python sync.py` only syncs tracking (Shopify → Etsy).

| Option | What it does | When to use it |
| --- | --- | --- |
| `--import-orders` | Copies new Etsy orders into Shopify **first**, then syncs tracking as usual. | Before packing. This is what `run_sync_orders.bat` runs. |
| `--exclude-ni` | Used with `--import-orders`. Imports orders going to Northern Ireland (postcodes starting `BT`) **on hold** instead of unfulfilled, tagged `NI - dispatch on Etsy`, and lists them in a boxed warning at the end of every run until they are dispatched. Buy the label and dispatch on Etsy; the next sync then releases the hold and marks the Shopify order fulfilled. Also accepted as `--excludeNI` or `--hold-ni`. | Royal Mail labels bought through Shopify cannot be used for Northern Ireland. `run_sync_orders.bat` already includes it. |
| `--tracking-only` | Syncs tracking and nothing else, even if other options ask for an import. | After buying labels. This is what `run_tracking.bat` runs. |
| `--dry-run` | A rehearsal. Prints exactly what *would* happen, but creates no orders, sends nothing to Etsy and saves nothing. | Any time you are unsure, after changing a setting, or on first use. Add it to any command. |
| `--force-dispatch` | Marks orders dispatched on Etsy **now**, even if their ship-by date is still in the future. | You posted early and want buyers to get their tracking today. |
| `--backfill-deadlines` | Re-applies the "Fulfill by" date to Etsy orders that are already in Shopify. Does not import anything new unless `--import-orders` is also given. | The "Fulfill by" column is empty for some orders. |
| `--limit N` | How many Etsy orders to look at per run. Default 500. | Only if a run says it reached the limit. |
| `--no-link-customers` | Switches off the default where a repeat Etsy buyer's new order is attached to their existing Shopify customer. Orders then each create a new customer, as before. | Only if linking ever misbehaves. |
| `--tag-repeats` | Tags orders already in Shopify (not yet dispatched) from repeat Etsy buyers: `Repeat customer`, `Repeat order N`. Add `--dry-run` to preview. It then stops without importing anything. | After a history backfill, or if open orders are missing their repeat tags. |

### Common combinations

| You want to… | Type |
| --- | --- |
| See what an import would do, without doing it | `python sync.py --import-orders --dry-run` |
| Import new orders | `python sync.py --import-orders` |
| Import new orders, holding Northern Ireland ones | `python sync.py --import-orders --exclude-ni` |
| See which orders would be marked dispatched | `python sync.py --tracking-only --dry-run` |
| Send tracking for orders that are due | `python sync.py --tracking-only` |
| Send tracking for everything with a label, due or not | `python sync.py --tracking-only --force-dispatch` |
| Preview that before doing it | `python sync.py --tracking-only --force-dispatch --dry-run` |
| Fix missing "Fulfill by" dates | `python sync.py --backfill-deadlines` |

### Options for the other scripts

**`backfill_orders.py`** downloads past Etsy orders into the database. It only reads from Etsy.

| Option | What it does |
| --- | --- |
| *(none)* | Downloads your whole order history. |
| `--dry-run` | Counts what would be saved, saves nothing. |
| `--max N` | Only the N most recent orders, for example `--max 500`. |

**`reports.py`** reads the local database only.

| Option | What it does |
| --- | --- |
| *(none)* | Summary, sales by month, top products, repeat customers. |
| `--months N` | How many months to list. Default 13. |
| `--top N` | How many products and customers to list. Default 20. |
| `--export FILE.csv` | Saves every order to a spreadsheet file instead, for example `--export orders.csv`. |

**`email_server.py`** listens for the buyer emails the Chrome extension captures from your Etsy Orders pages (see the email capture section above).

| Option | What it does |
| --- | --- |
| *(none)* | Starts the listener and keeps running; close its window or press Ctrl+C to stop. |
| `--stats` | Shows how many emails are stored, then stops. |
| `--missing [N]` | Lists the next N (default 25) highest-spending buyers with no email yet, then stops. |

**`backfill_customer_emails.py`** adds captured emails to the Shopify customers of orders that are already dispatched (the daily run only fills open orders).

| Option | What it does |
| --- | --- |
| *(none)* | Rehearsal: reads Shopify, prints how many customers would get an email, changes nothing. |
| `--apply` | Does it. Only customers with no email are filled; customers only (not orders); marketing consent is never touched. |
| `--limit N` | With `--apply`: stop after N customers (use `--apply --limit 10` as a trial). |

**`replace_placeholder_emails.py`** replaces OrderBridge's made-up customer emails (`etsy-<order number>@placeholder...`) with the real captured email.

| Option | What it does |
| --- | --- |
| *(none)* | Rehearsal: counts the placeholders and how many have a real email on file. |
| `--apply` | Does it. A real email is never replaced, subscribed customers are skipped, consent is never touched. |
| `--limit N` | With `--apply`: stop after N customers. |

**`merge_duplicate_customers.py`** merges Shopify customer records that belong to the same Etsy buyer. **Merging cannot be undone.** It needs the app permissions `read_customer_merge` and `write_customer_merge`.

| Option | What it does |
| --- | --- |
| *(none)* | Rehearsal: lists every safe merge and asks Shopify's own merge preview about each. Changes nothing. |
| `--apply` | Does the merges. Each is checked afterwards (orders added up, email kept). |
| `--limit N` | With `--apply`: stop after N merges (`--apply --limit 1` is the trial). |
| `--placeholders` | Instead of repeat buyers, merge an OrderBridge placeholder customer into the customer that already holds that buyer's real email. |

**`auth.py`** takes no options. It is the one-time Etsy sign-in.

### Changing what the shortcuts do

The `.bat` files are plain text. Right-click one, choose **Edit**, and change the `python sync.py …` line. For example, to make `run_tracking.bat` always send tracking straight away, change it to `python sync.py --tracking-only --force-dispatch`.

The last line, `pause`, is what keeps the window open until you press a key. Replace it with `timeout /t 20` to have the window close by itself after 20 seconds. Nothing is lost: every run is also saved in the `logs` folder.

---

## Setup

You need Python 3.10 or newer, an Etsy shop and a Shopify store.

### 1. Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env`. You will fill it in over the next steps. **Never share or commit `.env`.**

### 2. Etsy app

1. Create an app at <https://www.etsy.com/developers/your-apps>.
2. Set its redirect URI to `http://localhost:3003/oauth/redirect`.
3. Put the keystring and shared secret in `.env` as `ETSY_KEYSTRING` and `ETSY_SHARED_SECRET`, and your numeric shop ID as `ETSY_SHOP_ID`.
4. Run `python auth.py`. Your browser opens, you approve access, and the tokens are saved to `.env`. They refresh themselves from then on.

The app asks for these Etsy permissions: `transactions_r`, `transactions_w`, `shops_r`, `email_r`.

### 3. Shopify app

1. In the Shopify Dev Dashboard, create a custom app for your store.
2. Give it these Admin API scopes, then release and install it on the store:
   - `read_orders`, `write_orders`
   - `read_products`
   - `read_customers`, `write_customers`
   - `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders`
   - `read_assigned_fulfillment_orders`, `write_assigned_fulfillment_orders`
   - `read_third_party_fulfillment_orders`, `write_third_party_fulfillment_orders`
3. Put the store address (`your-store.myshopify.com`), client ID and client secret in `.env`.

### 4. Match your SKUs

An Etsy item is linked to a Shopify product when **the SKU is identical on both sides**. Items with no matching SKU are still imported, as plain line items not linked to a product.

### 5. Try it

```bash
python sync.py --import-orders --dry-run
```

Check the output looks right, then run it without `--dry-run`.

---

## Settings

### Dispatch note

To send buyers a message with the dispatch email, create a file called `dispatch_note.txt` next to `sync.py` (copy `dispatch_note.example.txt` to start). Delete the file, or leave it empty, to send no note.

- **Plain text only.** No bold, links or other formatting. Line breaks are fine.
- Keep it short and within Etsy's rules: you may thank the buyer and invite a review, but do not offer anything in exchange for a review, do not ask for a particular rating, and do not direct buyers to buy outside Etsy.

### Backups

After every real run (not a `--dry-run`), `orders.db` is copied to a `backups` folder next to `sync.py`. There is one file per day and the newest 30 are kept. To restore, copy the backup you want over `orders.db`.

A backup on the same disk does not protect you if the PC fails. For that, set `BACKUP_DIR` in `.env` to a folder that your cloud app uploads automatically, for example a folder inside Google Drive for desktop, OneDrive or Dropbox:

```
BACKUP_DIR=G:\My Drive\EtsySyncBackups
```

Keep the working `orders.db` where it is and only point `BACKUP_DIR` at the cloud folder. Putting the live database inside a syncing folder can corrupt it.

### Shipping rules

Postage upgrades are detected from the name of the Etsy shipping upgrade the buyer chose. Any upgrade whose name contains `24`, `express`, `first`, `1st`, `next day` or `priority` becomes **Royal Mail Tracked 24**; everything else is **Royal Mail Tracked 48**. Change `EXPRESS_KEYWORDS`, `detect_shipping()` and `map_carrier_for_etsy()` in `sync.py` for other carriers or countries.

---

## Your customers' data

`orders.db`, its backups, the `logs/` folder and any CSV export contain customer names and addresses. They are excluded from Git by `.gitignore`. Keep them private, protect the cloud account you back up to with two-step verification, and only use the data to fulfil orders.

---

## Files

| File | Purpose |
| --- | --- |
| `sync.py` | The sync itself: import, tracking, housekeeping |
| `backfill_orders.py` | Downloads past Etsy orders into the database |
| `backfill_customer_emails.py` | One-off: adds captured buyer emails to the Shopify customers of already-dispatched orders (rehearsal by default; `--apply`, `--limit N`) |
| `merge_duplicate_customers.py` | Merges Shopify customer records of the same Etsy buyer (same buyer id, surname and postcode) into the record that holds the email. Rehearsal by default; `--apply`, `--limit N`. Needs the app permissions `read_customer_merge` and `write_customer_merge`; merging cannot be undone |
| `merge_duplicate_customers.py --placeholders` | Instead merges an OrderBridge placeholder customer into the customer that already holds that buyer's real email (same surname and postcode). Same rehearsal-first rules (`--apply`, `--limit N`); merging cannot be undone |
| `replace_placeholder_emails.py` | One-off: replaces the made-up `etsy-<number>@placeholder.orderbridge.app` emails that the old OrderBridge tool put on Shopify customers with the buyer's real captured email. Rehearsal by default; `--apply`, `--limit N`. Customers only; never touches marketing consent; skips subscribed customers |
| `reports.py` | Reports and CSV export |
| `email_server.py`, `email_capture/` (own README) | Optional: local listener and Chrome extension that save buyer emails shown on your Etsy Orders pages |
| `auth.py` | One-time Etsy sign-in |
| `push_all_tracking.py` | Old name for the tracking sync; now just calls `sync.py --tracking-only` |
| `run_sync_orders.bat`, `run_tracking.bat` | Double-click shortcuts for Windows |
| `Force tracking.bat` | Shortcut for `--tracking-only --force-dispatch`: mark everything with a label as dispatched on Etsy now |
| `run_sync_scheduled.bat`, `run_sync_hidden.vbs` | The windowless run used by Windows Task Scheduler |
| `.env.example` | Template for your settings |
| `dispatch_note.example.txt` | Template for the buyer dispatch note |

## Troubleshooting

- **"Failed to refresh Etsy token"**: run `python auth.py` again to sign in.
- **An item shows `[Unlinked SKU]`**: the SKU on Etsy does not exactly match one in Shopify.
- **A repeat buyer is not tagged**: run `python backfill_orders.py` once so every past order has a buyer id, then `python sync.py --tag-repeats` for orders already in Shopify.
- **An order is "held"**: its ship-by date is in the future. Run again on that date or use `--force-dispatch`.
- **Something looks wrong**: open the latest file in `logs/` to see exactly what the last run did.
