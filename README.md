# Polymarket BTC 5m Trading Bot — v3

Automated trading bot for Polymarket's BTC 5-minute Up/Down markets.
Buys the dominant side (≥95%) in the last 25 seconds, only when price is stable and liquidity exists.

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Trading bot (WebSocket-native, v3) |
| `dashboard.py` | Password-protected web dashboard |
| `setup_credentials.py` | One-time credential check |
| `start.sh` | Runs bot + dashboard together |
| `.env.example` | Template — copy to `.env` |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container for Railway |
| `railway.toml` | Railway deploy config |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env — add your keys (see Credential Guide below)

# 3. Verify setup
python setup_credentials.py

# 4. Run bot + dashboard
bash start.sh
# Dashboard: http://localhost:8080
```

## Credential Guide

### POLYMARKET_PRIVATE_KEY
1. Go to **polymarket.com**
2. Click your avatar (top-right) → **Settings**
3. Scroll to **Export Private Key**
4. Copy the `0x...` hex string

### POLYMARKET_FUNDER_ADDRESS
1. Go to **polymarket.com**
2. Click **Deposit** (top-right)
3. Select **Polygon** network
4. Copy the wallet address shown (starts with `0x`)

> These are two different values. The private key is secret. The funder address is your public proxy wallet address.

## Deploy to Railway (free)

1. Push this folder to a GitHub repo
2. Go to **railway.app** → New Project → Deploy from GitHub
3. Add environment variables in Railway dashboard (Variables tab):
   - `POLYMARKET_PRIVATE_KEY`
   - `POLYMARKET_FUNDER_ADDRESS`
   - `DASH_USER`
   - `DASH_PASS`
   - `DASH_PORT` = `8080`
4. Go to Settings → Networking → Add Port → `8080`
5. Railway gives you a public URL for the dashboard

## Strategy

- Poll via WebSocket (not HTTP) for ~10ms price latency
- Enter when one side ≥ 95% AND time remaining ≤ 25s
- Stability check: std-dev of last 5 price ticks ≤ 0.015
- Liquidity check: ≥ $3 ask depth at entry price
- FOK (Fill-Or-Kill) order — fills instantly or cancels, never queues
- Heartbeat every 5s to keep session alive (required by Polymarket)
- Resolution via WebSocket `market_resolved` event (no polling)
- All trades logged to `trades.json` — dashboard reads this file

## trades.json schema

Each entry:
```json
{
  "cycle_id": "2026-03-14T14:45:00Z",
  "side": "up",
  "entry_price": 0.9623,
  "stake": 1.0,
  "outcome": "win",
  "payout": 1.0392,
  "gross_profit": 0.0392,
  "fee_usdc": 0.000014,
  "net_profit": 0.039186,
  "market_slug": "btc-updown-5m-1773507000",
  "timestamp": "2026-03-14T14:44:36Z"
}
```

Outcome values: `win`, `loss`, `unmatched`, `unstable`, `no_signal`, `no_liquidity`
