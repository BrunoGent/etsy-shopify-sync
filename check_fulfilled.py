import sync, requests

token = sync.get_shopify_token()
headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}
# Pull fulfilled orders from Shopify
url = f'https://{sync.SHOPIFY_SHOP_URL}/admin/api/2024-01/orders.json?status=any&fulfillment_status=shipped&limit=25'

res = requests.get(url, headers=headers).json()
orders = res.get('orders', [])
found = 0
for o in orders:
    name = o.get('name', '').replace('#', '')
    fulfillments = o.get('fulfillments', [])
    trackings = [f.get('tracking_number') for f in fulfillments if f.get('tracking_number')]
    if trackings:
        found += 1
        print(f"Shopify Order: {name} | Etsy ID candidate: {name} | Tracking: {trackings[0]}")

print(f"\nTotal fulfilled orders with tracking found: {found}")
