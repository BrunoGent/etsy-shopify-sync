import os
import requests
from dotenv import load_dotenv

load_dotenv()
headers = {
    "x-api-key": os.getenv("ETSY_KEYSTRING"),
    "Authorization": f"Bearer {os.getenv('ETSY_ACCESS_TOKEN')}",
}
r = requests.get("https://api.etsy.com/v3/application/users/me", headers=headers)
print("users/me:", r.status_code, r.text)

user_id = os.getenv("ETSY_ACCESS_TOKEN", "").split(".")[0]
r2 = requests.get(f"https://api.etsy.com/v3/application/users/{user_id}/shops", headers=headers)
print("users/{id}/shops:", r2.status_code, r2.text)
