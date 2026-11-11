import sync, requests

token = sync.get_shopify_token()
headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}
url = f'https://{sync.SHOPIFY_SHOP_URL}/admin/api/2024-01/orders.json?status=any&limit=5'

res = requests.get(url, headers=headers).json()
for o in res.get('orders', []):
    name = o.get('name')
    note = o.get('note')
    tags = o.get('tags')
    tracking = [f.get('tracking_number') for f in o.get('fulfillments', [])]
    print(f"Order: {name} | Note: {note} | Tags: {tags} | Tracking: {tracking}")
