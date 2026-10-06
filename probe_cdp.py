"""Exit 0 if a real Playwright CDP connection to the debug Chrome works, else 1.
The HTTP /json/version ping can pass while real connections time out (degraded Chrome)."""
import sys
from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=20000)
        _ = b.contexts[0].pages
except Exception:
    sys.exit(1)
sys.exit(0)
