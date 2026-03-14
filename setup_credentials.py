"""
Run ONCE before starting the bot.
Verifies your private key + funder address work against the live API.

Usage:
  cp .env.example .env
  # fill in .env
  python setup_credentials.py
"""

import os, time, sys
from dotenv import load_dotenv

load_dotenv()

PK     = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")

print("\n── Polymarket Bot — Credential Check ──────────────────────")

if not PK or "YOUR" in PK:
    print("❌  POLYMARKET_PRIVATE_KEY not set in .env")
    sys.exit(1)
if not FUNDER or "YOUR" in FUNDER:
    print("❌  POLYMARKET_FUNDER_ADDRESS not set in .env")
    sys.exit(1)

print(f"   Key:    {PK[:6]}...{PK[-4:]}  (hidden)")
print(f"   Funder: {FUNDER}")

try:
    from py_clob_client.client import ClobClient
    client = ClobClient(
        "https://clob.polymarket.com",
        key=PK, chain_id=137, signature_type=1, funder=FUNDER,
    )
    creds = client.create_or_derive_api_creds()
    print(f"\n✅  Authenticated!  API key: {creds.api_key[:16]}...")

    server_ts = int(client.get_server_time())
    drift = abs(time.time() - server_ts)
    status = "✅  OK" if drift < 3 else f"⚠️  WARNING: {drift:.1f}s drift — timing may be off"
    print(f"   Clock drift vs server: {drift:.2f}s  {status}")

except Exception as e:
    print(f"\n❌  Authentication failed: {e}")
    print("""
Common fixes:
  • POLYMARKET_PRIVATE_KEY must start with 0x and be the SIGNING key
    (Settings → Export Private Key on polymarket.com)
  • POLYMARKET_FUNDER_ADDRESS is the proxy address shown when you click Deposit
  • Make sure your .env file is saved and not committed to git
""")
    sys.exit(1)

import urllib.request, json
try:
    with urllib.request.urlopen("https://gamma-api.polymarket.com/markets?slug=btc-updown-5m-1773506400", timeout=5) as r:
        data = json.loads(r.read())
        print(f"   Gamma API: ✅  reachable ({len(data)} market returned)")
except Exception as e:
    print(f"   Gamma API: ⚠️  {e}")

print("""
✅  Setup complete.
   Next: python bot.py          (trading bot)
         python dashboard.py    (web dashboard on :8080)
         bash start.sh          (both together)
""")
