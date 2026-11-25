# Etsy buyer-email capture (Chrome extension + local listener)

Etsy's API hides buyer emails, but your own Orders page in Shop Manager shows them. This pair saves the ones you see, so you can later link Etsy customers to Shopify customers.

- **`email_capture/`** (this folder): a small Chrome extension. It only reads the Orders page you already have open (the normal list and search results). It makes no requests to Etsy, clicks nothing and changes nothing on the page.
- **`../email_server.py`**: a listener on `127.0.0.1:8765`. It accepts connections from this PC only, and only from the extension (web pages are refused). It saves to `orders.db`.

## Setup (once)
1. In Chrome open `chrome://extensions`, switch on **Developer mode**, click **Load unpacked** and choose this `email_capture` folder.
2. After any change to the extension, click its reload arrow there and press F5 on the Etsy tab.

## Use
1. Start the listener and leave it open:
   ```
   python email_server.py
   ```
2. In Etsy go to Shop Manager -> Orders (set to All, 50 per page) and browse. Each order shown is saved; the listener prints `Captured N email(s) from N order(s)`.
3. Etsy only lists the last couple of months, so for older buyers search by name. Search is fuzzy (Sonia also shows Sofia); each row is saved under its own buyer id, so nothing is mixed up.
4. See who is still missing, and totals:
   ```
   python email_server.py --missing
   python email_server.py --stats
   ```
   `--missing` always lists the next 25 highest-spending customers without an email. Another Etsy account with the same name and postcode counts as having one.

If nothing is captured, press F12 -> Console on the Etsy tab and look for `[BeeMine email capture]`. A "not saved" warning means the listener is not running; the page retries by itself. An "extension was reloaded" warning means press F5.

- **Inactive emails:** when Etsy shows "Inactive email" for a buyer (deleted account, no usable address), the extension records that order in `inactive_emails`, and `--missing` skips those customers so the list only shows people you can still get an email for.

## What is stored
- `etsy_emails (receipt_id, email)`: per order. `sync.py` reads this when it imports an order.
- `buyer_emails (buyer_id, email, buyer_name)`: per Etsy buyer, for linking repeat customers.

Some buyers (Sign in with Apple) have a relay address (`...@privaterelay.appleid.com`) that forwards to them but will not match a normal address elsewhere.

## Privacy
Emails are personal data (UK GDPR). Use them for fulfilment and customer service only; marketing needs the customer's consent. `orders.db` and its backups must never be committed or put in a syncing folder.
