import sync
import requests
import time

print("[Etsy] Refreshing access token...")
sync.refresh_etsy_tokens()
etsy_headers = sync.get_etsy_headers()
etsy_headers.pop('Content-Type', None)

token = sync.get_shopify_token()
shopify_headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}

# Fetch up to 50 fulfilled orders from Shopify
url = f'https://{sync.SHOPIFY_SHOP_URL}/admin/api/2024-01/orders.json?status=any&fulfillment_status=shipped&limit=50'
res = requests.get(url, headers=shopify_headers).json()
orders = res.get('orders', [])

success = 0
skipped = 0
failed = 0

for o in orders:
    name = str(o.get('name', '')).replace('#', '').strip()
    
    # Only target 10-digit Etsy receipt IDs
    if not (name.isdigit() and len(name) == 10):
        continue
        
    fulfillments = o.get('fulfillments', [])
    trackings = [f.get('tracking_number') for f in fulfillments if f.get('tracking_number')]
    if not trackings:
        continue
        
    tracking_code = trackings[0]
    receipt_id = name

    # Skip Estelle Bingham (already dispatched)
    if receipt_id == '4216164517':
        continue

    payload = {
        'tracking_code': tracking_code,
        'carrier_name': 'royal-mail',
        'send_bcc': 'false'
    }

    etsy_url = f'https://api.etsy.com/v3/application/shops/{sync.ETSY_SHOP_ID}/receipts/{receipt_id}/tracking'
    r = requests.post(etsy_url, headers=etsy_headers, data=payload)

    if r.status_code == 200:
        print(f"[+] Dispatched #{receipt_id} with {tracking_code}")
        success += 1
    elif r.status_code == 409 or 'already shipped' in r.text.lower():
        print(f"[-] #{receipt_id} already marked shipped on Etsy")
        skipped += 1
    else:
        print(f"[!] Failed #{receipt_id}: {r.status_code} -> {r.text}")
        failed += 1

    time.sleep(0.3)

print(f"\nFinished! Dispatched: {success} | Skipped: {skipped} | Failed: {failed}")