import requests
import re
import json

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
r = requests.get("https://www.vinted.fr/items/8860166901", headers=headers)
html = r.text

match = re.search(r'"view_count":(\d+)', html)
if match:
    print("view_count:", match.group(1))

match = re.search(r'"created_at_ts":"([^"]+)"', html)
if match:
    print("created_at_ts:", match.group(1))

# Let's find any script tag containing "item" and "view_count"
for script in re.findall(r'<script.*?>.*?</script>', html, re.DOTALL):
    if "view_count" in script:
        print("Script length:", len(script))
        print("Script start:", script[:200])
