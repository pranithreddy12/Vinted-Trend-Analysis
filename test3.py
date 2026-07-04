import requests
import re
import json

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
r = requests.get('https://www.vinted.fr/items/8860166901', headers=headers)
html = r.text

try:
    # Next.js embeds JSON payloads inside script tags.
    # We can try to extract the entire JSON or just use regex on the raw HTML
    
    # 1. Regex approach on raw HTML
    ts_match = re.search(r'\\"created_at_ts\\":\\"([^\\]+)\\"', html)
    if not ts_match:
        ts_match = re.search(r'\\"created_at_ts\\":([\d.]+)', html)
    print('TS:', ts_match.group(1) if ts_match else 'Not found')

    fc = re.search(r'\\"favourite_count\\":(\d+)', html)
    print('FC:', fc.group(1) if fc else 'Not found')

    vc = re.search(r'\\"view_count\\":(\d+)', html)
    print('VC:', vc.group(1) if vc else 'Not found')

    ic = re.search(r'\\"is_closed\\":(true|false)', html)
    print('is_closed:', ic.group(1) if ic else 'Not found')
    
    cb = re.search(r'\\"can_buy\\":(true|false)', html)
    print('can_buy:', cb.group(1) if cb else 'Not found')
    
except Exception as e:
    print(e)

