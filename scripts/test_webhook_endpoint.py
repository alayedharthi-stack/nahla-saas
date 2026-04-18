"""
Simulates app.store.authorize webhook WITHOUT real Salla signature.
Used ONLY in dev/test to manually inject fresh tokens when OAuth is blocked.
Run: python -X utf8 scripts/test_webhook_endpoint.py
"""
import sys, json, urllib.request, urllib.error
sys.stdout.reconfigure(encoding='utf-8')

ENDPOINT = "https://api.nahlah.ai/webhooks/salla"

# Simulated app.store.authorize payload (the same structure Salla sends)
payload = {
    "event":      "app.store.authorize",
    "merchant":   22825873,
    "created_at": "2026-04-18T12:00:00Z",
    "data": {
        "merchant_id":  22825873,
        "store_id":     22825873,
        "access_token":  "__REPLACE_WITH_TOKEN__",
        "refresh_token": "__REPLACE_WITH_REFRESH__",
        "expires_in":    3600,
        "scope":         "offline_access",
        "store": {
            "id":   22825873,
            "name": "متجر نحلة التجريبي"
        }
    }
}

body = json.dumps(payload).encode("utf-8")
req  = urllib.request.Request(
    ENDPOINT,
    data    = body,
    headers = {
        "Content-Type": "application/json",
        "X-Salla-Event": "app.store.authorize",
    },
    method  = "POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"Status : {resp.status}")
        print(f"Body   : {resp.read().decode()}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()}")
except Exception as e:
    print(f"Error: {e}")
