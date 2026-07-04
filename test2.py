import requests
import re

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
r = requests.get('https://www.vinted.fr/items/8860166901', headers=headers)
html = r.text

try:
    print('favourite_count:', re.search(r'\\"favourite_count\\":(\d+)', html).group(1))
    print('view_count:', re.search(r'\\"view_count\\":(\d+)', html).group(1))
    
    # created_at_ts might be an int or a string
    ts_match = re.search(r'\\"created_at_ts\\":\\"([^"]+)\\"', html)
    if not ts_match:
        ts_match = re.search(r'\\"created_at_ts\\":([\d.]+)', html)
        
    print('created_at_ts:', ts_match.group(1) if ts_match else 'None')
    
    is_closed = re.search(r'\\"is_closed\\":(true|false)', html)
    print('is_closed:', is_closed.group(1) if is_closed else 'None')
    
    can_buy = re.search(r'\\"can_buy\\":(true|false)', html)
    print('can_buy:', can_buy.group(1) if can_buy else 'None')
    
except Exception as e:
    print("Error:", e)
