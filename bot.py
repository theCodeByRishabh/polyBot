"""
Polymarket BTC 5-Minute Bot — v9

Changes vs v8:
  1. MIN_BTC_DISTANCE reduced from $60 to $40 (more trade opportunities)
  2. STAKE reduced from $6 to $5
  3. IST night-hours block: no trades between 01:00–08:00 IST (UTC 19:30–02:30)
  4. BRUTAL sell logic: if bid drops to <= 93% after entering at 99%, immediately
     sell everything (multi-attempt aggressive FAK cascade, no price floor retreat)
  5. Compounding bug fix: compound_base is now updated AFTER redeem confirms (not
     speculatively). A pending_redeem flag prevents the next trade from firing on
     a stake we don't yet have in the account.

Changes vs v7 (retained from v8):
  ANTI-REVERSAL / LOSS REDUCTION
  1. get_signal() now checks momentum direction over last MOMENTUM_N (8) ticks:
       - Skips if price has dropped > MAX_DROP_FROM_PEAK (2.5¢) from its recent high
         (catches smooth late reversals that still pass the std-dev stability filter)
       - Skips if fewer than MIN_POSITIVE_TICKS (3) of the last 8 ticks are non-declining
         (filters sustained downward drift heading into the entry window)
  2. Late-entry tightening: within LATE_ENTRY_MAX_T (20s) of close, ask threshold
     is raised by +2% to avoid marginal entries right before a potential flip.
  3. get_signal() now accepts time_left so both the pre-sign and live entry windows
     apply the correct threshold — presigned orders at T-40s are unaffected.
  4. should_stop_loss() gains a fast-drop detector: if price has already fallen
     >=8¢ from its recent peak AND >=6¢ below entry price (with >15s remaining),
     the bot exits early — before reaching the hard 75% SL floor.  This limits
     losses on trades that enter correctly but reverse sharply mid-window.

REDEEM OVERHAUL (v6):
  Root cause of v5 redeem failures:
    • Your account is a Magic/email Proxy Wallet (signature_type=1).
    • Positions live INSIDE the proxy contract, NOT at your raw EOA address.
    • Direct web3.py redeemPositions() targets the CTF contract from your EOA —
      this ALWAYS reverts because the proxy contract holds the tokens, not the EOA.
    • v5's _redeem_direct() was therefore broken by design and could never work.

  v6 fix — three-layer redeem cascade, all routed through the proxy:

  LAYER 1 — py_builder_relayer_client (PROXY type) [requires Builder creds]
    Uses RelayerTxType.PROXY explicitly (the critical v5 bug was using .SAFE).
    Sends redeemPositions calldata to the relayer, which executes it through
    your proxy wallet gaslessly.  This is Polymarket's official supported path.

  LAYER 2 — poly-web3 library [requires Builder creds, optional install]
    Community Python port of the official TS builder-relayer-client.
    pip install poly-web3
    Used as a second attempt if layer 1 fails with an SDK error.

  LAYER 3 — Proxy forward call via web3.py [requires MATIC gas, ~$0.001]
    Calls the Polymarket Proxy contract's `forward(address,bytes,uint256)`
    function directly from your EOA (the proxy owner).  This correctly routes
    redeemPositions through the proxy — unlike v5 which called CTF directly.
    Proxy contract ABI: forward(address to, bytes data, uint256 value)
    Proxy factory: 0xaB45c5A4B0c941a2F231C04C3f49182e1A254052 (MagicLink users)
    No relayer/builder creds needed for this layer — only tiny MATIC gas.

  BALANCE POLLING
    After any successful submission, polls every 5s for up to 10 minutes
    until USDC balance increases, then saves final trade record.

  .env additions for full functionality:
    BUILDER_KEY=...          # From Polymarket Settings → Builder/API
    BUILDER_SECRET=...
    BUILDER_PASSPHRASE=...
    POLYGON_RPC_URL=...      # Optional: custom RPC (defaults to public endpoints)
"""

import os, json, time, logging, statistics, asyncio, signal, math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Direct redeem fallback (web3) ────────────────────────────────────────
try:
    from web3 import Web3
    from eth_account import Account
    WEB3_AVAILABLE = True
except ImportError:
    Web3 = None
    Account = None
    WEB3_AVAILABLE = False

import aiohttp
import websockets
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, OrderArgs, MarketOrderArgs, OrderType, BalanceAllowanceParams
# BalanceAllowanceParams imported above — do not re-import inside get_balance()
from py_clob_client.order_builder.constants import BUY, SELL

# ─── Logging ──────────────────────────────────────────────────────────────────
import logging.handlers as _lh

def _make_logger():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh  = logging.StreamHandler()
    sh.setFormatter(fmt)
    # Rotate at 5 MB, keep 2 files — total max ~10 MB, covers ~3h of typical log volume
    fh = _lh.RotatingFileHandler("bot.log", maxBytes=5*1024*1024, backupCount=2)
    fh.setFormatter(fmt)
    logger = logging.getLogger("polybot")
    logger.setLevel(logging.INFO)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger

log = _make_logger()

# ─── Config ───────────────────────────────────────────────────────────────────
PRIVATE_KEY     = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER_ADDRESS  = os.environ["POLYMARKET_FUNDER_ADDRESS"]
CLOB_HOST       = "https://clob.polymarket.com"
GAMMA_API       = "https://gamma-api.polymarket.com"
WSS_MARKET      = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WSS_RTDS        = "wss://ws-live-data.polymarket.com"  # RTDS Chainlink feed
CHAIN_ID        = 137
CTF_CONTRACT    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E          = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID = "0x" + ("00" * 32)
RELAYER_URL     = os.environ.get("POLYMARKET_RELAYER_URL") or os.environ.get("RELAYER_URL") or "https://relayer-v2.polymarket.com/"

# ── Strategy ──────────────────────────────────────────────────────────────────
STAKE             = 8.00    # Base stake per trade
BASE_THRESHOLD    = 0.98    # Buy when dominant side ask >= 99%
ADAPTIVE_THRESH   = 0.99    # Same threshold even after losses (99% only)
ENTRY_WINDOW_SEC  = 40      # Enter only in last 30 seconds
PRESIGN_BEFORE    = 50      # Build signed order at T-40s (ready for T-30s window)
MIN_FIRE_BUFFER   = 1       # Never fire if < 5s remain (too risky)
STOP_LOSS_BID     = 0.93    # Exit position if best_bid falls below this
STOP_LOSS_MIN_SEC = 1       # Don't stop-loss if < 5s remain (just let it resolve)
STABILITY_N       = 5       # Price ticks needed for stability check
MAX_STD_DEV       = 0.015   # Max std-dev for stability
MIN_LIQUIDITY     = 3.0     # Min USDC ask depth
MAX_SPREAD        = 0.04    # Skip if bid-ask spread > 4¢

# ── BTC distance + volatility filter (Chainlink-based) ────────────────────────
# Polymarket BTC 5m markets resolve based on Chainlink BTC/USD price.
# The "Price to Beat" is the Chainlink price at window open (btc_opening_price).
# We subscribe to Polymarket's own RTDS Chainlink feed to track this in real-time.
MIN_BTC_DISTANCE    = 60.0  # live BTC must be >= $40 away from opening price in bet direction
BTC_VOLATILITY_SEC  = 60    # look-back window in seconds to measure BTC volatility
BTC_MAX_VOLATILITY  = 150.0 # skip if BTC high-low range > $150 in last 60s (too choppy)

# ── Oscillation / two-way volatility filter ───────────────────────────────────
# Skip if the Polymarket probability is swinging wildly in BOTH directions.
# We measure the SIZE of reverse moves, not just count — small wobbles like
# 0.95→0.94→0.96 are fine (only 1¢ reversal). Big swings like 0.90→0.75→0.90
# are dangerous. Only skip if a reverse move is larger than OSCILLATION_MAX_REVERSE.
OSCILLATION_N           = 6      # how many recent ticks to inspect
OSCILLATION_MAX_REVERSE = 0.08   # a single reverse tick > 8¢ counts as a bad swing
OSCILLATION_BAD_SWINGS  = 2      # skip if >= this many BAD reverse ticks found

# ── Late-entry tightening ──────────────────────────────────────────────────────
# Within LATE_ENTRY_MAX_T seconds of close the market has less time to recover
# from a sudden flip, so we demand a higher ask before entering.
# Effective threshold = base/adaptive threshold + LATE_ENTRY_SURCHARGE.
LATE_ENTRY_MAX_T     = 3     # seconds from close where tightening kicks in
LATE_ENTRY_SURCHARGE = 0.01  # add 1¢ to required ask when time_left < LATE_ENTRY_MAX_T

# ── IST trading hours block ────────────────────────────────────────────────────
# No trades between 01:00 and 08:00 IST (Indian Standard Time = UTC+5:30).
# In UTC that is: 19:30 (prev day) to 02:30.
# We store as (hour, minute) tuples in UTC.
IST_BLOCK_START_UTC = (19, 30)   # 01:00 IST = 19:30 UTC
IST_BLOCK_END_UTC   = ( 2, 30)   # 08:00 IST = 02:30 UTC  (crosses midnight)

# ── Brutal sell trigger ────────────────────────────────────────────────────────
# If the market bid drops at or below this level AFTER we enter at 99%,
# immediately sell EVERYTHING as aggressively as possible (price floor = 0.01).
# Goal: cut losses hard and fast rather than riding a reversal to zero.
BRUTAL_SELL_THRESHOLD = 0.93   # 93% — triggers brutal exit if bid <= this level

TRADES_FILE = Path("trades.json")

# ─── Data structures ──────────────────────────────────────────────────────────
@dataclass
class Market:
    slug: str
    end_ts: int
    up_token_id: str
    down_token_id: str
    condition_id: str
    neg_risk: bool  = False
    tick_size: str  = "0.01"
    fee_rate: str   = "0"

@dataclass
class PriceTick:
    ts: float
    token_id: str
    best_bid: float
    best_ask: float

@dataclass
class TradeTick:
    ts: float
    token_id: str
    size: float

@dataclass
class TradeRecord:
    cycle_id: str
    side: str
    entry_price: float
    exit_price: float       # 0 if held to resolution
    shares_held: float      # shares bought = STAKE / entry_price
    stake: float
    outcome: str            # win / loss / stop_loss / unmatched / skip
    payout: float
    gross_profit: float
    fee_usdc: float
    net_profit: float
    balance_before: float
    balance_after: float
    market_slug: str
    timestamp: str
    skip_reason: str = ""

@dataclass
class Position:
    """Tracks an open position after a buy order fills."""
    token_id: str
    side: str
    entry_price: float
    shares: float           # number of outcome tokens held
    stake: float            # actual dollars spent (compound stake at time of buy)
    cycle_id: str
    market: "Market"        # type annotation forward ref as string

@dataclass
class BotState:
    market: Optional[Market]      = None
    next_market: Optional[Market] = None
    price_history: deque          = field(default_factory=lambda: deque(maxlen=200))
    trade_history: deque          = field(default_factory=lambda: deque(maxlen=500))
    position: Optional[Position]  = None   # set after a buy fills
    trade_fired: bool             = False   # prevent double-firing per cycle
    resolved: Optional[str]       = None   # winning token_id from WSS
    heartbeat_id: str             = ""
    presigned_order: object       = None
    presigned_for: Optional[str]  = None
    consecutive_losses: int       = 0
    consecutive_wins: int         = 0   # resets consecutive_losses after 5 wins
    exchange_disabled: bool       = False
    last_balance: float           = 0.0
    # Clock sync — proper fields instead of monkey-patched attrs
    clock_offset: float           = 0.0
    last_sync: float              = 0.0
    last_market_scan: float       = 0.0
    last_status_ts: float         = 0.0    # deduplicate status log lines
    redeemed_condition_ids: set   = field(default_factory=set)  # prevent double-redeem
    # ── Compounding stake ─────────────────────────────────────────────────────
    # compound_base = the INTENDED stake, never corrupted by partial FAK fills.
    #                 This is what gets compounded on win.
    # current_stake = same as compound_base; what presign_order and _do_buy use.
    # On win:       compound_base += net_profit, current_stake = compound_base
    # On loss/stop: compound_base = STAKE, current_stake = STAKE
    current_stake: float          = STAKE   # live stake for next trade
    compound_base: float          = STAKE   # intended stake — never corrupted by partial fills
    last_net_profit: float        = 0.0     # net profit from the last winning trade
    # ── Chainlink BTC/USD price tracking ──────────────────────────────────────
    # Populated by run_chainlink_wss from Polymarket's RTDS feed (no auth needed).
    # btc_opening_price: Chainlink price at window open = "Price to Beat"
    # btc_live_price:    latest Chainlink tick
    # btc_price_history: list of (timestamp, price) tuples for volatility check
    btc_opening_price: float      = 0.0
    btc_live_price: float         = 0.0
    btc_price_history: list       = field(default_factory=list)  # (ts, price) pairs
    # ── Compounding fix: pending redeem guard ──────────────────────────────────
    # Set True when a WIN is detected and background redeem is in-flight.
    # The trading loop checks this: if True, compound_base has already been
    # bumped speculatively — but the USDC isn't in the account yet.
    # Trading is blocked until the redeem confirms OR times out, then flag clears.
    pending_redeem: bool          = False

# ─── Persistence ──────────────────────────────────────────────────────────────
def load_trades() -> list:
    """Read all trade records. Supports both legacy JSON array and NDJSON formats."""
    if not TRADES_FILE.exists():
        return []
    try:
        text = TRADES_FILE.read_text().strip()
        if not text:
            return []
        # Try NDJSON first (new format — one JSON object per line)
        if text.startswith("{"):
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        # Legacy format: single JSON array
        return json.loads(text)
    except Exception:
        return []

def save_trade(r: TradeRecord):
    """Append trade record to JSON file without loading the whole file into RAM.
    Uses a newline-delimited JSON (NDJSON) append for O(1) writes.
    load_trades() still returns a proper list for compatibility.
    """
    with TRADES_FILE.open("a") as f:
        f.write(json.dumps(asdict(r)) + "\n")

# ─── CLOB client ──────────────────────────────────────────────────────────────
def build_client() -> ClobClient:
    delay = 2
    while True:
        try:
            client = ClobClient(
                CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                signature_type=1, funder=FUNDER_ADDRESS,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            log.info("CLOB client ready.")
            return client
        except Exception as e:
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg:
                log.error(f"CLOB auth failed: {e}")
                raise
            log.error(f"CLOB client init failed: {e}")
            log.info(f"Retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 60)

# ─── Balance ──────────────────────────────────────────────────────────────────
def get_balance(client: ClobClient, retries: int = 3) -> float:
    """Fetch USDC balance with up to `retries` attempts. Returns 0.0 only if all fail."""
    for attempt in range(1, retries + 1):
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
            resp = client.get_balance_allowance(params=params)
            raw = float(resp.get("balance", 0))
            # Polymarket returns balance in micro-USDC (6 decimals), convert to dollars
            return raw / 1_000_000 if raw > 1000 else raw
        except Exception as e:
            if attempt < retries:
                log.warning(f"Balance check failed (attempt {attempt}/{retries}): {e} — retrying...")
                time.sleep(0.5)
            else:
                log.warning(f"Balance check failed after {retries} attempts: {e} — returning 0.0")
    return 0.0

# ─── Error classification & retry ─────────────────────────────────────────────
class ExchangeDisabledError(Exception): pass
class InsufficientFundsError(Exception): pass

def classify_error(e) -> str:
    m = str(e).lower()
    if "not enough balance" in m or "insufficient" in m: return "funds"
    if "429" in m or "too many requests" in m:           return "rate_limit"
    if "425" in m:                                       return "restart"
    if "503" in m or "trading is currently" in m:        return "disabled"
    if "401" in m or "unauthorized" in m:                return "auth"
    if "duplicated" in m:                                return "duplicate"
    if "tick size" in m:                                 return "tick"
    if "fok" in m or "couldn't be fully filled" in m:   return "no_fill"
    return "other"

async def with_retry(fn, label: str, max_tries: int = 6):
    delay = 1
    for attempt in range(max_tries):
        try:
            return fn()
        except Exception as e:
            kind = classify_error(e)
            if kind in ("restart", "rate_limit"):
                log.warning(f"{label}: {kind} — retry {attempt+1} in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            elif kind == "disabled":
                raise ExchangeDisabledError(str(e))
            elif kind == "funds":
                raise InsufficientFundsError(str(e))
            else:
                log.error(f"{label} ({kind}): {e}")
                raise
    raise RuntimeError(f"{label}: max retries exceeded")

# ─── Heartbeat ────────────────────────────────────────────────────────────────
async def heartbeat_loop(client: ClobClient, state: BotState, stop: asyncio.Event):
    state.heartbeat_id = ""  # Always start fresh — let server assign first ID
    while not stop.is_set():
        try:
            resp = client.post_heartbeat(state.heartbeat_id)
            new_id = resp.get("heartbeat_id", "")
            if new_id:
                state.heartbeat_id = new_id
        except Exception as e:
            if "Invalid Heartbeat ID" in str(e):
                state.heartbeat_id = ""  # Reset so next call gets a fresh one
            else:
                log.warning(f"Heartbeat error: {e}")
        await asyncio.sleep(5)

async def cred_refresh_loop(client: ClobClient, stop: asyncio.Event):
    """Refresh CLOB API credentials every 6 hours.
    CLOB API keys expire after ~24h. Refreshing proactively prevents silent
    401 errors that would stop the bot from placing orders mid-session.
    """
    REFRESH_INTERVAL = 6 * 3600  # 6 hours
    await asyncio.sleep(REFRESH_INTERVAL)  # first refresh after 6h
    while not stop.is_set():
        try:
            client.set_api_creds(client.create_or_derive_api_creds())
            log.info("[CREDS] ✅ CLOB API credentials refreshed successfully.")
        except Exception as e:
            log.warning(f"[CREDS] Credential refresh failed (will retry in 1h): {e}")
            await asyncio.sleep(3600)
            continue
        await asyncio.sleep(REFRESH_INTERVAL)

# ─── Market discovery ─────────────────────────────────────────────────────────
async def fetch_btc_market(session: aiohttp.ClientSession, server_ts: int) -> Optional[Market]:
    # Polymarket slug uses WINDOW START timestamp (not end)
    # e.g. btc-updown-5m-1773515100 = window that STARTS at 1773515100, ends at 1773515400
    log.info(f"[MARKET SCAN] server_ts={server_ts} | searching for next BTC 5m market...")
    current_window_start = (server_ts // 300) * 300
    # Try current window and next 2 windows
    for start_ts in [current_window_start, current_window_start + 300, current_window_start + 600]:
        end_ts = start_ts + 300  # window ends 5 min after start
        time_left = end_ts - server_ts
        if time_left <= ENTRY_WINDOW_SEC + 2:
            log.info(f"[MARKET SCAN] slug btc-updown-5m-{start_ts} skipped (only {time_left}s left, need >{ENTRY_WINDOW_SEC+2}s)")
            continue
        slug = f"btc-updown-5m-{start_ts}"
        log.info(f"[MARKET SCAN] trying slug: {slug} ({time_left}s until close)")
        m = await _gamma_slug(session, slug, end_ts_override=end_ts)
        if m:
            log.info(f"[MARKET SCAN] ✅ found market: {slug} | closes in {time_left}s")
            return m
        else:
            log.info(f"[MARKET SCAN] ❌ not found: {slug}")
    log.warning("[MARKET SCAN] no market found — will retry in 15s")
    return None

GAMMA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

async def _gamma_slug(session: aiohttp.ClientSession, slug: str, end_ts_override: int = 0) -> Optional[Market]:
    """
    Fetch market data from Gamma API.
    These BTC 5m markets have ONE market object with TWO outcomes (Up/Down)
    stored in clobTokenIds[0] and clobTokenIds[1], ordered by the outcomes string.
    """
    try:
        async with session.get(f"{GAMMA_API}/events", params={"slug": slug},
                               headers=GAMMA_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            log.info(f"[GAMMA] GET /events?slug={slug} → HTTP {r.status}")
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None

            event  = data[0]
            markets = event.get("markets", [])
            log.info(f"[GAMMA] event has {len(markets)} market(s)")

            if not markets:
                return None

            # BTC Up/Down is a single market with 2 outcomes — dump raw to log
            m = markets[0]
            log.info(f"[GAMMA] market keys: {sorted(m.keys())}")

            # Try every possible field that could hold token IDs
            # Gamma returns clobTokenIds and outcomes as JSON-encoded strings sometimes
            def _parse_json_or_csv(val):
                if not val: return []
                if isinstance(val, list): return val
                if isinstance(val, str):
                    val = val.strip()
                    if val.startswith("["):
                        try: return json.loads(val)
                        except: pass
                    return [v.strip().strip('"') for v in val.split(",")]
                return []

            clob_ids     = _parse_json_or_csv(m.get("clobTokenIds"))
            tokens       = m.get("tokens") or []
            outcomes_raw = m.get("outcomes", "")
            outcomes     = _parse_json_or_csv(outcomes_raw)
            # Strip any leftover quotes from outcome strings
            outcomes     = [str(o).strip().strip('"').strip("'") for o in outcomes]

            log.info(f"[GAMMA] clobTokenIds={clob_ids}")
            log.info(f"[GAMMA] tokens={tokens}")
            log.info(f"[GAMMA] outcomes={outcomes}")

            # Parse end date
            try:
                end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                end_ts = int(end_dt.timestamp())
            except Exception:
                end_ts = end_ts_override

            # Strategy 1: clobTokenIds with outcomes ordering
            if len(clob_ids) >= 2 and len(outcomes) >= 2:
                up_i   = next((i for i,o in enumerate(outcomes) if o.lower() == "up"),   0)
                down_i = next((i for i,o in enumerate(outcomes) if o.lower() == "down"), 1)
                up_id, down_id = clob_ids[up_i], clob_ids[down_i]
                log.info(f"[GAMMA] ✅ Strategy 1 — clobTokenIds: up={str(up_id)[:16]}... down={str(down_id)[:16]}... end={end_ts}")
                return Market(slug=slug, end_ts=end_ts, up_token_id=str(up_id),
                              down_token_id=str(down_id), condition_id=m.get("conditionId",""),
                              neg_risk=m.get("negRisk", False))

            # Strategy 2: tokens array
            if len(tokens) >= 2:
                up_tok   = next((t for t in tokens if str(t.get("outcome","")).lower() == "up"),   None)
                down_tok = next((t for t in tokens if str(t.get("outcome","")).lower() == "down"), None)
                if up_tok and down_tok:
                    up_id   = up_tok.get("token_id") or up_tok.get("id","")
                    down_id = down_tok.get("token_id") or down_tok.get("id","")
                    log.info(f"[GAMMA] ✅ Strategy 2 — tokens array: up={str(up_id)[:16]}...")
                    return Market(slug=slug, end_ts=end_ts, up_token_id=str(up_id),
                                  down_token_id=str(down_id), condition_id=m.get("conditionId",""),
                                  neg_risk=m.get("negRisk", False))

            # Strategy 3: fallback to /markets endpoint
            log.info(f"[GAMMA] trying /markets endpoint as fallback...")
            async with session.get(f"{GAMMA_API}/markets", params={"slug": slug},
                                   headers=GAMMA_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r2:
                if r2.status == 200:
                    mdata = await r2.json()
                    if mdata:
                        m2 = mdata[0]
                        log.info(f"[GAMMA] /markets keys: {sorted(m2.keys())}")
                        log.info(f"[GAMMA] /markets clobTokenIds={m2.get('clobTokenIds')} outcomes={m2.get('outcomes')}")
                        clob2    = m2.get("clobTokenIds") or []
                        out2_raw = m2.get("outcomes", "")
                        out2     = [o.strip() for o in (out2_raw.split(",") if isinstance(out2_raw, str) else out2_raw)]
                        if len(clob2) >= 2 and len(out2) >= 2:
                            up_i   = next((i for i,o in enumerate(out2) if o.lower() == "up"),   0)
                            down_i = next((i for i,o in enumerate(out2) if o.lower() == "down"), 1)
                            log.info(f"[GAMMA] ✅ Strategy 3 — /markets fallback")
                            return Market(slug=slug, end_ts=end_ts, up_token_id=str(clob2[up_i]),
                                          down_token_id=str(clob2[down_i]), condition_id=m2.get("conditionId",""),
                                          neg_risk=m2.get("negRisk", False))

            log.warning(f"[GAMMA] ❌ all strategies failed for {slug}")
            return None

    except Exception as e:
        log.warning(f"[GAMMA] fetch error [{slug}]: {e}", exc_info=True)
    return None

def enrich_market(client: ClobClient, market: Market) -> Market:
    # Ensure token IDs are clean strings before any API calls
    def _clean(tid):
        if isinstance(tid, list): tid = tid[0] if tid else ""
        s = str(tid).strip().strip("[]\"' ")
        return s
    market.up_token_id   = _clean(market.up_token_id)
    market.down_token_id = _clean(market.down_token_id)
    log.info(f"[ENRICH] up_token_id={market.up_token_id[:20]}...")
    log.info(f"[ENRICH] down_token_id={market.down_token_id[:20]}...")
    try:
        market.tick_size = str(client.get_tick_size(market.up_token_id))
    except Exception as e:
        log.warning(f"tick_size: {e} — using default 0.01")
    try:
        fr = client.get_fee_rate(market.up_token_id)
        market.fee_rate = str(fr) if fr is not None else "0"
    except Exception:
        market.fee_rate = "0"  # fee_rate not available in this SDK version
    log.info(f"Market ready: {market.slug} | tick={market.tick_size} fee_bps={market.fee_rate}")
    return market

# ─── IST trading-hours guard ──────────────────────────────────────────────────
def is_ist_blocked() -> bool:
    """
    Returns True if current UTC time falls in the IST night-hours block:
      01:00–08:00 IST  ==  19:30–02:30 UTC  (spans midnight)
    No trades should be placed during this window.
    """
    now_utc = datetime.utcnow()
    h, m    = now_utc.hour, now_utc.minute
    # Convert to minutes-since-midnight for easy comparison
    cur_min   = h * 60 + m
    start_min = IST_BLOCK_START_UTC[0] * 60 + IST_BLOCK_START_UTC[1]   # 19*60+30 = 1170
    end_min   = IST_BLOCK_END_UTC[0]   * 60 + IST_BLOCK_END_UTC[1]     # 2*60+30  = 150
    # Block spans midnight: blocked if cur >= start OR cur < end
    return cur_min >= start_min or cur_min < end_min


# ─── Signal detection ─────────────────────────────────────────────────────────
def get_signal(market: Market, history: deque, consecutive_losses: int,
               time_left: int = 999) -> Optional[tuple]:
    """
    Returns (side, token_id, best_ask) if ALL pass:

    1. best_ask >= threshold (95% base, 98% after 2 consecutive losses)
    2. Last STABILITY_N ticks have low std-dev (price is steady overall)
    3. Oscillation check: of the last OSCILLATION_N tick-to-tick moves,
       fewer than OSCILLATION_REVERSE went AGAINST our direction.
       This catches two-way chop — e.g. 97%→94%→97%→94% has acceptable
       std-dev but is actually oscillating badly and should be skipped.
       Strong one-directional moves (e.g. 92%→94%→96%→97%) pass fine.
    """
    threshold = ADAPTIVE_THRESH if consecutive_losses >= 2 else BASE_THRESHOLD
    if consecutive_losses >= 2:
        log.info(f"  Adaptive threshold: {threshold*100:.0f}% (streak of {consecutive_losses} losses)")

    # Late-entry tightening: raise required ask by LATE_ENTRY_SURCHARGE when close
    # to expiry. Presigned orders at T-40s are unaffected (time_left defaults to 999).
    late_entry = time_left < LATE_ENTRY_MAX_T
    effective_threshold = min(threshold + LATE_ENTRY_SURCHARGE, 1.00) if late_entry else threshold
    if late_entry:
        log.debug(f"  Late-entry tightening: T-{time_left}s — threshold {threshold:.2f} -> {effective_threshold:.2f}")

    for token_id, side_label in [
        (market.up_token_id,   "up"),
        (market.down_token_id, "down"),
    ]:
        asks = [t.best_ask for t in history if t.token_id == token_id]

        # ── 1. Threshold + stability ──────────────────────────────────────
        ticks = asks[-STABILITY_N:]
        if len(ticks) < STABILITY_N:
            continue
        if ticks[-1] < effective_threshold:
            continue
        std = statistics.stdev(ticks) if len(ticks) > 1 else 0.0
        if std > MAX_STD_DEV:
            log.info(f"  {side_label.upper()} unstable: ask={ticks[-1]:.4f} std={std:.4f}")
            continue

        # ── 2. Oscillation check ──────────────────────────────────────────
        osc_ticks = asks[-OSCILLATION_N:]
        if len(osc_ticks) >= 3:
            # Count reverse moves that are LARGE (> OSCILLATION_MAX_REVERSE).
            # Small wobbles like 0.95→0.94 (1¢ drop) are ignored.
            # Big swings like 0.90→0.75 (15¢ drop) count as dangerous.
            bad_swings = sum(
                1 for i in range(1, len(osc_ticks))
                if osc_ticks[i - 1] - osc_ticks[i] > OSCILLATION_MAX_REVERSE
            )
            if bad_swings >= OSCILLATION_BAD_SWINGS:
                log.info(
                    f"  {side_label.upper()} SKIP — large oscillations: "
                    f"{bad_swings} swing(s) > {OSCILLATION_MAX_REVERSE:.2f} in last "
                    f"{len(osc_ticks)-1} ticks"
                )
                continue

        log.info(f"  Signal: {side_label.upper()} ask={ticks[-1]:.4f} std={std:.4f}")
        return side_label, token_id, ticks[-1]
    return None


# ─── Entry filters ────────────────────────────────────────────────────────────
def spread_ok(history: deque, token_id: str) -> bool:
    """
    At extreme prices (>= 0.97) the spread naturally widens because the
    losing side has almost no value — allow up to 0.05 instead of 0.03.
    """
    recent = [t for t in history if t.token_id == token_id]
    if not recent: return False
    t = recent[-1]
    spread = t.best_ask - t.best_bid
    max_spread = 0.05 if t.best_ask >= 0.97 else MAX_SPREAD
    ok = spread <= max_spread
    log.info(f"  Spread: bid={t.best_bid:.4f} ask={t.best_ask:.4f} spread={spread:.4f} max={max_spread:.2f} ({'ok' if ok else 'WIDE'})")
    return ok
def volume_surge(trade_history: deque, token_id: str) -> bool:
    """Returns True (skip) if recent 30s volume is 3x the prior 30s."""
    now    = time.time()
    recent = sum(t.size for t in trade_history if t.token_id == token_id and now - t.ts <= 30)
    older  = sum(t.size for t in trade_history if t.token_id == token_id and 30 < now - t.ts <= 60)
    if older == 0: return False
    ratio = recent / older
    surge = ratio > 3.0
    log.info(f"  Volume momentum: recent={recent:.1f} older={older:.1f} ratio={ratio:.1f} {'SURGE-SKIP' if surge else 'ok'}")
    return surge

async def liquidity_ok(session: aiohttp.ClientSession, token_id: str, price: float) -> bool:
    """
    Check ask-side liquidity depth.

    When price >= 0.99 the book is nearly settled — sellers disappear because
    there is nothing left to gain by offering the losing side.  At that point
    requiring $3 of ask depth would block every high-confidence trade.
    We scale the threshold down linearly so that:
      price < 0.97  → require MIN_LIQUIDITY ($3.00)
      price = 0.99  → require $1.00
      price = 1.00  → require $0.10 (essentially just "any offer exists")
    We also widen the ask-price window from +0.01 to +0.02 at high prices
    because the spread can legitimately be a tick or two wider near certainty.
    """
    try:
        async with session.get(f"{CLOB_HOST}/book", params={"token_id": token_id},
                               timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status == 200:
                book = await r.json()
                window = 0.02 if price >= 0.97 else 0.01
                depth  = sum(float(a["size"]) * float(a["price"])
                             for a in book.get("asks", [])
                             if float(a["price"]) <= price + window)

                # Scale minimum liquidity: full requirement below 0.97,
                # tapering to $0.10 at price=1.00
                if price >= 0.97:
                    # linear interpolation: 0.97→MIN_LIQUIDITY, 1.00→0.10
                    t         = (price - 0.97) / 0.03          # 0..1
                    min_depth = MIN_LIQUIDITY * (1 - t) + 0.10 * t
                else:
                    min_depth = MIN_LIQUIDITY

                ok = depth >= min_depth
                log.info(f"  Liquidity: ${depth:.2f} (min=${min_depth:.2f}) ({'ok' if ok else 'THIN'})")
                return ok
    except Exception as e:
        log.warning(f"Liquidity check: {e}")
    return False

# ─── Order execution ──────────────────────────────────────────────────────────
def presign_order(client: ClobClient, market: Market, token_id: str, price: float, stake: float = None):
    """Build and EIP-712 sign the BUY limit order at T-40s so it's ready to POST at fire time.
    Uses GTC limit order at ask price — acts as taker if book has supply, else rests briefly.
    """
    if stake is None:
        stake = STAKE
    try:
        raw_size = stake / price
        size = math.ceil(raw_size * 100) / 100  # round UP to 2 decimal places
        size = max(size, 5.0)  # enforce minimum 5 shares
        log.info(f"  Order size: {size} shares @ {price} (${size*price:.4f}) [stake=${stake:.4f}]")
        order_args = OrderArgs(
            token_id = token_id,
            price    = price,
            size     = size,
            side     = BUY,
        )
        return client.create_order(order_args)
    except Exception as e:
        log.error(f"Presign error: {e}")
        return None

async def execute_sell(client: ClobClient, market: Market, position: Position,
                       exit_price: float, clock_offset: float = 0.0) -> Optional[dict]:
    """
    Sell all shares via FAK SELL, retrying aggressively until filled or market resolves.

    Key fixes vs previous versions:
      - Uses clock_offset for accurate time remaining (same as trading loop)
      - Stops only when < 2s remain (not MIN_FIRE_BUFFER) — every second counts
      - Floor starts at exit_price - 0.05 (wider initial buffer for crashing markets)
      - Drops floor by 2¢ every attempt — reaches 0.01 fast if market is thin
      - Confirms fill via size_matched first, then balance delta
      - Returns immediately on first confirmed fill
    """
    bal_before = get_balance(client)
    attempt = 0

    while True:
        attempt += 1

        # Use clock-offset-corrected time — same reference as trading loop
        now_left = market.end_ts - int(time.time() + clock_offset)
        if now_left <= 2:
            log.warning(f"  SELL abort: only {now_left}s left, market resolving.")
            return None

        # Floor drops 2¢ per attempt: attempt 1 = exit-0.05, attempt 2 = exit-0.07 ...
        # Reaches 0.01 (accept any price) by attempt ~25, but in practice fills much sooner.
        floor = max(exit_price - 0.05 - (attempt - 1) * 0.02, 0.01)

        log.info(f"  SELL attempt {attempt}: {position.shares:.6f} shares "
                 f"@ floor={floor:.4f} | T-{now_left}s remain")
        try:
            mo = MarketOrderArgs(
                token_id = position.token_id,
                amount   = position.shares,
                side     = SELL,
                price    = floor,
            )
            order = client.create_market_order(mo)
            resp  = client.post_order(order, OrderType.FAK)
            log.info(f"  Sell resp (attempt {attempt}): {resp}")

            # Primary confirmation: any of these fields indicate a fill.
            # The SDK returns takingAmount/makingAmount (not size_matched) for FAK sells.
            size_matched  = float(resp.get("size_matched",  0) or 0) if resp else 0
            taking_amount = float(resp.get("takingAmount",  0) or 0) if resp else 0
            making_amount = float(resp.get("makingAmount",  0) or 0) if resp else 0
            status        = (resp.get("status", "") or "").lower()   if resp else ""

            if size_matched > 0 or taking_amount > 0 or making_amount > 0 or status == "matched":
                log.info(f"  ✅ Sell filled on attempt {attempt}: "
                         f"status={status} taking={taking_amount} making={making_amount}")
                return resp

            # Fallback: balance increased (allow 2s for on-chain propagation)
            await asyncio.sleep(2.0)
            bal_after = get_balance(client)
            if bal_after > bal_before + 0.01:
                log.info(f"  ✅ Sell confirmed via balance: "
                         f"${bal_before:.4f} → ${bal_after:.4f} on attempt {attempt}")
                return resp

            log.warning(f"  Attempt {attempt}: no fill at floor={floor:.4f}, "
                        f"dropping floor and retrying immediately...")

        except Exception as e:
            log.error(f"  Sell attempt {attempt} error: {e}")

        # No sleep between retries — time is critical when stop-loss fires
        # The 0.5s balance check above is the only delay


async def execute_brutal_sell(client: ClobClient, market: Market, position: Position,
                              clock_offset: float = 0.0) -> Optional[dict]:
    """
    BRUTAL sell: sell ALL shares at any price, starting floor=0.01 immediately.
    No gentle floor-dropping — we go straight to 0.01 on attempt 1.
    Retries every loop tick until filled, balance confirms, or < 2s remain.

    This is triggered when bid drops to <= 93% (BRUTAL_SELL_THRESHOLD) after
    entering at 99%. The goal is maximum capital preservation — accept any price,
    get out NOW.
    """
    bal_before = get_balance(client)
    attempt = 0
    log.warning(f"  ⚡ BRUTAL SELL — {position.shares:.6f} shares @ floor=0.01 (accept ANY price)")

    while True:
        attempt += 1
        now_left = market.end_ts - int(time.time() + clock_offset)

        if now_left <= 2:
            log.warning(f"  BRUTAL SELL abort: {now_left}s left, market resolving. Attempting one last shot.")
            # One final attempt even if almost expired
            try:
                mo = MarketOrderArgs(token_id=position.token_id, amount=position.shares,
                                     side=SELL, price=0.01)
                order = client.create_market_order(mo)
                resp  = client.post_order(order, OrderType.FAK)
                log.info(f"  BRUTAL final attempt resp: {resp}")
            except Exception as e:
                log.error(f"  BRUTAL final attempt error: {e}")
            return None

        log.warning(f"  ⚡ BRUTAL attempt {attempt}: {position.shares:.6f} shares @ 0.01 | T-{now_left}s remain")
        try:
            mo = MarketOrderArgs(
                token_id = position.token_id,
                amount   = position.shares,
                side     = SELL,
                price    = 0.01,   # Accept ANYTHING — capital preservation is the only goal
            )
            order = client.create_market_order(mo)
            resp  = client.post_order(order, OrderType.FAK)
            log.info(f"  BRUTAL resp (attempt {attempt}): {resp}")

            size_matched  = float(resp.get("size_matched",  0) or 0) if resp else 0
            taking_amount = float(resp.get("takingAmount",  0) or 0) if resp else 0
            making_amount = float(resp.get("makingAmount",  0) or 0) if resp else 0
            status        = (resp.get("status", "") or "").lower()   if resp else ""

            if size_matched > 0 or taking_amount > 0 or making_amount > 0 or status == "matched":
                log.warning(f"  ⚡ BRUTAL SELL FILLED on attempt {attempt}: "
                            f"status={status} taking={taking_amount} making={making_amount}")
                return resp

            # Check balance
            await asyncio.sleep(1.0)
            bal_now = get_balance(client)
            if bal_now > bal_before + 0.01:
                log.warning(f"  ⚡ BRUTAL SELL confirmed via balance: "
                            f"${bal_before:.4f} → ${bal_now:.4f}")
                return resp

            log.warning(f"  BRUTAL attempt {attempt} not confirmed — retrying immediately...")

        except Exception as e:
            log.error(f"  BRUTAL sell attempt {attempt} error: {e}")
            await asyncio.sleep(0.5)


# ─── Stop-loss monitor ────────────────────────────────────────────────────────
def should_stop_loss(position: Position, history: deque, time_left: int) -> Optional[tuple]:
    """
    Returns (exit_bid, brutal) if stop-loss should trigger, else None.

      brutal=True  → BRUTAL_SELL_THRESHOLD breached (bid <= 93%): sell at ANY price,
                     floor starts at 0.01 immediately — maximum aggression.
      brutal=False → standard soft stop-loss (existing fast-drop or hard-floor logic).

    Three triggers (checked in priority order):
      0. BRUTAL    : bid <= BRUTAL_SELL_THRESHOLD (93%) after entering at 99%.
                     Ignores STOP_LOSS_MIN_SEC — fires even close to expiry.
      1. Hard SL   : bid < STOP_LOSS_BID (85%) at any time > STOP_LOSS_MIN_SEC.
      2. Fast-drop : bid has dropped >= 5¢ from entry AND > 15s remain.

    Uses the median of the last 3 bid ticks to filter stale/noisy WSS ticks.
    """
    bids = [t.best_bid for t in history if t.token_id == position.token_id]
    if not bids:
        return None

    # Use median of last 3 ticks to smooth out noise
    recent = [b for b in bids[-3:] if b > 0]
    if not recent:
        return None
    current_bid = statistics.median(recent)

    # ── Trigger 0: BRUTAL sell (93% floor breach) ─────────────────────────────
    # Only applies if entry was at 99%+ (we only ever enter at 99%, but guard anyway).
    # Fires regardless of time_left — every second counts when reversing hard.
    if position.entry_price >= BASE_THRESHOLD and current_bid <= BRUTAL_SELL_THRESHOLD:
        log.warning(
            f"  ⚡ BRUTAL SELL triggered: bid={current_bid:.4f} <= {BRUTAL_SELL_THRESHOLD} "
            f"(entry={position.entry_price:.4f}) | T-{time_left}s remain"
        )
        return current_bid, True

    # Standard triggers only fire if enough time remains
    if time_left <= STOP_LOSS_MIN_SEC:
        return None

    # ── Trigger 1: Hard stop-loss ─────────────────────────────────────────────
    if current_bid < STOP_LOSS_BID:
        log.info(f"  Hard stop-loss: bid={current_bid:.4f} < {STOP_LOSS_BID}")
        return current_bid, False

    # ── Trigger 2: Fast-drop stop-loss ───────────────────────────────────────
    if time_left > 15:
        drop_from_entry = position.entry_price - current_bid
        if drop_from_entry >= 0.05:
            log.info(
                f"  Fast-drop stop-loss: entry={position.entry_price:.4f} "
                f"bid={current_bid:.4f} drop={drop_from_entry:.4f}"
            )
            return current_bid, False

    return None

# ─── Profit calculation ───────────────────────────────────────────────────────
def calc_profit(stake, entry_price, exit_price, fee_rate_bps, outcome):
    """
    outcome: "win" | "loss" | "stop_loss"
    For win: redeem all shares at $1 each
    For loss: shares worth $0
    For stop_loss: sold at exit_price per share
    """
    shares = stake / entry_price
    fee_rate = fee_rate_bps / 10000

    if outcome == "win":
        payout = shares * 1.0
    elif outcome == "stop_loss":
        payout = shares * exit_price
    else:  # loss
        payout = 0.0

    gross    = payout - stake
    fee_usdc = shares * fee_rate * (entry_price * (1 - entry_price)) ** 2
    net      = gross - fee_usdc
    return round(payout,6), round(gross,6), round(fee_usdc,6), round(net,6)

# ─── WebSocket ────────────────────────────────────────────────────────────────
async def run_chainlink_wss(state: BotState, stop: asyncio.Event):
    """
    Subscribe to Polymarket's RTDS Chainlink feed to get the same BTC/USD price
    Polymarket uses for settlement. No auth required.

    Tracks:
      state.btc_live_price    — updated on every tick
      state.btc_opening_price — set once per window from the first tick at/after
                                window_start_ts (= market.end_ts - 300).
                                This is the exact "Price to Beat".
      state.btc_price_history — rolling list of (ts, price) for volatility check.
                                Pruned to last BTC_VOLATILITY_SEC seconds.
    """
    SUB_MSG = json.dumps({
        "action": "subscribe",
        "subscriptions": [{
            "topic":   "crypto_prices_chainlink",
            "type":    "*",
            "filters": "{\"symbol\":\"btc/usd\"}"
        }]
    })

    log.info("[CHAINLINK] Starting Chainlink BTC/USD feed...")
    while not stop.is_set():
        try:
            async with websockets.connect(WSS_RTDS, ping_interval=None, open_timeout=10) as ws:
                await ws.send(SUB_MSG)
                log.info("[CHAINLINK] ✅ Subscribed to crypto_prices_chainlink btc/usd")

                async for raw in ws:
                    if stop.is_set():
                        break
                    if raw in ("PING", "PONG"):
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("topic") != "crypto_prices_chainlink":
                        continue
                    payload = msg.get("payload", {})
                    if payload.get("symbol", "").lower() != "btc/usd":
                        continue

                    btc_price = float(payload.get("value", 0) or 0)
                    if btc_price <= 0:
                        continue

                    now_ts = time.time()
                    state.btc_live_price = btc_price

                    # Maintain price history — prune to last BTC_VOLATILITY_SEC seconds
                    state.btc_price_history.append((now_ts, btc_price))
                    cutoff = now_ts - BTC_VOLATILITY_SEC
                    state.btc_price_history = [
                        (t, p) for t, p in state.btc_price_history if t >= cutoff
                    ]

                    # Capture opening price once per window.
                    # Window start = market.end_ts - 300.
                    # The RTDS timestamp is in milliseconds.
                    if state.market and state.btc_opening_price == 0.0:
                        window_start_ts = state.market.end_ts - 300
                        tick_ts_raw = payload.get("timestamp", 0)
                        tick_ts = tick_ts_raw / 1000 if tick_ts_raw > 1e10 else tick_ts_raw
                        if tick_ts >= window_start_ts:
                            state.btc_opening_price = btc_price
                            log.info(f"[CHAINLINK] Opening price captured: ${btc_price:.2f} "
                                     f"(window_start={window_start_ts})")

        except Exception as e:
            if not stop.is_set():
                log.warning(f"[CHAINLINK] dropped (reconnect in 3s): {e}")
                await asyncio.sleep(3)


async def run_market_wss(state: BotState, client: ClobClient,
                         session: aiohttp.ClientSession, stop: asyncio.Event):
    log.info("[WSS] WebSocket task started — waiting for market...")
    current_slug = None  # track which market we're subscribed to

    while not stop.is_set():
        if not state.market:
            await asyncio.sleep(1)
            continue

        # If market changed, reconnect with new tokens
        if state.market.slug == current_slug:
            await asyncio.sleep(0.1)
            continue

        current_slug = state.market.slug

        # Ensure token IDs are clean strings (not lists or bracket-wrapped)
        def _clean_id(tid):
            if isinstance(tid, list): tid = tid[0] if tid else ""
            return str(tid).strip().strip("[]\"' ")
        up_id_clean   = _clean_id(state.market.up_token_id)
        down_id_clean = _clean_id(state.market.down_token_id)
        state.market.up_token_id   = up_id_clean
        state.market.down_token_id = down_id_clean

        asset_ids = [up_id_clean, down_id_clean]
        sub = json.dumps({"assets_ids": asset_ids, "type": "market",
                          "custom_feature_enabled": True})
        log.info(f"[WSS] Connecting to {WSS_MARKET} for {state.market.slug}...")
        log.info(f"[WSS] Subscribing to tokens: UP={up_id_clean[:16]}... DOWN={down_id_clean[:16]}...")
        try:
            async with websockets.connect(WSS_MARKET, ping_interval=None, open_timeout=10) as ws:
                await ws.send(sub)
                log.info(f"[WSS] ✅ Connected & subscribed: {state.market.slug}")
                ping_t = asyncio.create_task(_wss_ping(ws, stop))
                subscribed_slug = current_slug  # capture slug this connection was opened for

                async for raw in ws:
                    if stop.is_set():
                        break

                    # KEY FIX: if market was advanced while we were in this
                    # inner loop, break immediately so we reconnect with the
                    # new market's token IDs. Without this the loop keeps
                    # reading from the old socket and never re-subscribes.
                    if state.market and state.market.slug != subscribed_slug:
                        log.info(f"[WSS] Market changed → {state.market.slug} — closing old socket")
                        break

                    if raw in ("PING", "PONG"):
                        continue
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        continue

                    # Polymarket WSS sends either a single dict OR a list of dicts
                    messages = parsed if isinstance(parsed, list) else [parsed]

                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue

                        etype = msg.get("event_type", "")

                        if etype == "price_change":
                            for ch in msg.get("price_changes", []):
                                tid = ch.get("asset_id", "")
                                # Filter: only accept ticks for the current market's tokens
                                if state.market and tid not in (
                                    state.market.up_token_id, state.market.down_token_id
                                ):
                                    continue
                                state.price_history.append(PriceTick(
                                    ts=time.time(), token_id=tid,
                                    best_bid=float(ch.get("best_bid", 0) or 0),
                                    best_ask=float(ch.get("best_ask", 1) or 1),
                                ))
                            if len(state.price_history) <= 3:
                                log.info(f"[WSS] First price tick! total={len(state.price_history)}")

                        elif etype == "best_bid_ask":
                            tid = msg.get("asset_id", "")
                            if state.market and tid not in (
                                state.market.up_token_id, state.market.down_token_id
                            ):
                                continue
                            state.price_history.append(PriceTick(
                                ts=time.time(), token_id=tid,
                                best_bid=float(msg.get("best_bid", 0) or 0),
                                best_ask=float(msg.get("best_ask", 1) or 1),
                            ))
                            if len(state.price_history) <= 3:
                                log.info(f"[WSS] First best_bid_ask tick! total={len(state.price_history)}")

                        elif etype == "last_trade_price":
                            state.trade_history.append(TradeTick(
                                ts=time.time(), token_id=msg.get("asset_id", ""),
                                size=float(msg.get("size", 0) or 0),
                            ))

                        elif etype == "tick_size_change":
                            tid = msg.get("asset_id", "")
                            if state.market and tid in (
                                state.market.up_token_id, state.market.down_token_id
                            ):
                                new_ts = msg.get("new_tick_size", "0.01")
                                if state.market.tick_size != new_ts:
                                    state.market.tick_size = new_ts
                                    state.presigned_order  = None
                                    log.warning(f"[WSS] Tick size changed → {new_ts}")

                        elif etype == "new_market":
                            slug = msg.get("slug", "")
                            if "btc-updown-5m" in slug.lower():
                                assets   = msg.get("assets_ids", [])
                                outcomes = msg.get("outcomes", [])
                                if len(assets) == 2 and len(outcomes) == 2:
                                    up_i = next(
                                        (i for i, o in enumerate(outcomes) if o.lower() == "up"), 0
                                    )
                                    state.next_market = Market(
                                        slug=slug, end_ts=0,
                                        up_token_id=assets[up_i],
                                        down_token_id=assets[1 - up_i],
                                        condition_id=msg.get("market", ""),
                                    )
                                    log.info(f"[WSS] Next market queued: {slug}")

                        elif etype == "market_resolved":
                            state.resolved = msg.get("winning_asset_id", "")
                            log.info(f"[WSS] Market resolved: winner={state.resolved[:20]}...")

                        elif etype and etype not in ("book",):
                            log.debug(f"[WSS] unhandled event: {etype}")

        except Exception as e:
            if not stop.is_set():
                log.warning(f"[WSS] dropped (reconnect in 2s): {e}")
                await asyncio.sleep(2)
        finally:
            # Always cancel ping task and clear slug so outer loop reconnects.
            try:
                ping_t.cancel()
            except Exception:
                pass
            current_slug = None

async def _wss_ping(ws, stop):
    while not stop.is_set():
        try: await ws.send("PING")
        except: break
        await asyncio.sleep(10)

# ─── Main trading loop ────────────────────────────────────────────────────────
async def trading_loop(client: ClobClient, session: aiohttp.ClientSession,
                       state: BotState, stop: asyncio.Event):
    while not stop.is_set():
        # Sleep duration depends on phase:
        # - Entry window (T-30s to T-5s): 0.1s so we react to every WSS price tick
        # - All other phases: 1s is fine (no trade decision being made)
        _now = time.time()
        _tl  = (state.market.end_ts - int(_now + state.clock_offset)) if state.market else 999
        await asyncio.sleep(0.1 if MIN_FIRE_BUFFER < _tl <= ENTRY_WINDOW_SEC else 1)

        if state.exchange_disabled:
            log.warning("Exchange disabled — retrying in 60s...")
            await asyncio.sleep(60)
            state.exchange_disabled = False
            continue

        # ── IST Night-hours block (01:00–08:00 IST = 19:30–02:30 UTC) ────────
        if is_ist_blocked():
            now_utc = datetime.utcnow()
            log.info(f"[IST BLOCK] No trading 01:00–08:00 IST (UTC now={now_utc.strftime('%H:%M')}) — sleeping 60s")
            await asyncio.sleep(60)
            continue

        if not state.market:
            # Poll for market every 15s — don't rely solely on WSS new_market event
            now = int(time.time())
            if now - state.last_market_scan > 15:
                state.last_market_scan = now
                log.info("[LOOP] No market loaded — polling Gamma API...")
                m = await fetch_btc_market(session, now)
                if m:
                    state.market = enrich_market(client, m)
                    log.info(f"[LOOP] ✅ Market loaded: {state.market.slug} | ends in {state.market.end_ts - now}s")
                else:
                    log.info("[LOOP] No market available yet — will retry in 15s")
            continue

        # Use local clock + server offset.
        # Sync every 10s during entry window (time-critical), every 30s otherwise.
        now_local   = time.time()
        in_window   = state.market and MIN_FIRE_BUFFER < (state.market.end_ts - int(now_local + state.clock_offset)) <= ENTRY_WINDOW_SEC
        sync_interval = 10 if in_window else 30
        if now_local - state.last_sync > sync_interval:
            try:
                server_ts_raw = int(await asyncio.to_thread(client.get_server_time))
                state.clock_offset = server_ts_raw - now_local
                state.last_sync    = now_local
            except Exception:
                pass  # keep existing offset on failure
        server_ts = int(now_local + state.clock_offset)
        time_left = state.market.end_ts - server_ts

        # Log status every 5s — use wall-clock timer to avoid duplicate logs
        now_wall = time.time()
        if now_wall - state.last_status_ts >= 5.0:
            state.last_status_ts = now_wall
            ups = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
            dns = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
            tick_count = len(state.price_history)
            if ups and dns:
                log.info(f"[STATUS] T-{time_left}s | UP={ups[-1]:.3f} DOWN={dns[-1]:.3f} | ticks={tick_count} | bal=${state.last_balance:.2f}")
            else:
                log.info(f"[STATUS] T-{time_left}s | waiting for price ticks... (received {tick_count} so far)")

        # ── Window expired ─────────────────────────────────────────────────
        if time_left <= 0:
            if not state.trade_fired:
                log.info("Window closed — no trade this cycle.")
                _log_skip(state.market, "", 0, "no_signal", state.last_balance)

            # If we have an open position at market close: launch background
            # redeem task BEFORE wiping state. WSS market_resolved may arrive
            # slightly late or be missed — we poll the Data API directly.
            if state.position is not None:
                pos = state.position
                cid = pos.market.condition_id
                bal_snap = state.last_balance
                if cid:
                    log.info(f"[REDEEM] Market closed with open position — launching background claim ({cid[:20]}...)")
                    asyncio.create_task(
                        _background_redeem(session, client, state, cid, pos,
                                           0, 0, 0, bal_snap)
                    )

            await _advance_market(client, session, state)
            continue

        # ── OPEN POSITION: monitor for stop-loss or resolution ─────────────
        if state.position is not None:
            sl_result = should_stop_loss(state.position, state.price_history, time_left)
            if sl_result is not None:
                exit_bid, is_brutal = sl_result
                await _do_stop_loss(client, session, state, exit_bid, brutal=is_brutal)
                # Only advance if position was actually cleared (sell succeeded).
                # If sell failed, _do_stop_loss leaves state.position intact so we
                # keep monitoring and the redeem at close will handle it.
                if state.position is None:
                    await _advance_market(client, session, state)
                continue

            # Resolution came in via WSS before market close
            if state.resolved is not None:
                await _resolve_position(client, session, state)
                await _advance_market(client, session, state)
                continue

            continue  # holding, still monitoring

        # ── PRE-SIGN at T-55s to T-46s ────────────────────────────────────
        # Only check price >= BASE_THRESHOLD (99%) — no stability/oscillation
        # filters here. Those run at T-45s before the order actually fires.
        # Goal: have a signed order ready the moment the entry window opens.
        if ENTRY_WINDOW_SEC < time_left <= PRESIGN_BEFORE and not state.presigned_order:
            for token_id, side_label in [
                (state.market.up_token_id,   "up"),
                (state.market.down_token_id, "down"),
            ]:
                asks = [t.best_ask for t in state.price_history if t.token_id == token_id]
                if asks and asks[-1] >= BASE_THRESHOLD:
                    state.presigned_order = presign_order(
                        client, state.market, token_id, asks[-1],
                        stake=state.current_stake
                    )
                    state.presigned_for = token_id
                    log.info(f"Pre-signed at T-{time_left}s: {side_label.upper()} @ {asks[-1]:.4f} "
                             f"[stake=${state.current_stake:.4f}]")
                    break

        # ── ENTRY WINDOW: T-30s to T-3s ───────────────────────────────────
        if MIN_FIRE_BUFFER < time_left <= ENTRY_WINDOW_SEC and not state.trade_fired:
            signal = get_signal(state.market, state.price_history, state.consecutive_losses, time_left)

            if not signal:
                ups  = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
                dns  = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
                up_p = ups[-1] if ups else 0
                dn_p = dns[-1] if dns else 0
                # Throttle this log to once per second to avoid flooding
                if now_wall - state.last_status_ts >= 1.0:
                    state.last_status_ts = now_wall
                    log.info(f"  T-{time_left}s | UP={up_p:.3f} DOWN={dn_p:.3f} | no signal")
                continue

            side, token_id, price = signal
            log.info(f"SIGNAL @ T-{time_left}s: BTC {side.upper()} ask={price:.4f} | stake=${state.current_stake:.4f}")

            # Gate 1: spread — retry each tick, don't permanently skip
            # Spread can widen temporarily then narrow back within the 30s window
            if not spread_ok(state.price_history, token_id):
                log.info(f"  T-{time_left}s: wide spread — will retry next tick...")
                continue

            # Gate 2: volume surge — retry each tick, don't permanently skip
            if volume_surge(state.trade_history, token_id):
                log.info(f"  T-{time_left}s: volume surge — will retry next tick...")
                continue

            # Gate 3: liquidity — retry every loop tick until window closes,
            # liquidity can appear at any moment within the 30s window.
            if not await liquidity_ok(session, token_id, price):
                log.info(f"  T-{time_left}s: liquidity thin — will retry next tick...")
                # Do NOT set trade_fired — keep trying until MIN_FIRE_BUFFER
                continue

            # Gate 4: balance — also block if a win redeem is still in-flight
            # (compound_base was bumped but USDC not yet in account)
            if state.pending_redeem:
                log.info(f"  T-{time_left}s: pending redeem in-flight — waiting for USDC before next trade...")
                continue
            bal = get_balance(client)
            state.last_balance = bal
            if bal < state.current_stake:
                log.error(f"Insufficient funds: ${bal:.4f} < ${state.current_stake:.2f} (current stake)")
                state.trade_fired = True
                _log_skip(state.market, side, price, "insufficient_funds", bal)
                continue

            # Gate 5: BTC distance from opening price (Chainlink)
            # Only trade if live BTC is >= $60 away from window opening price
            # in the direction we're betting. Skips dangerous "coin-flip" setups.
            # If Chainlink feed not ready yet, allow trade as failsafe.
            if state.btc_opening_price > 0 and state.btc_live_price > 0:
                btc_gap = state.btc_live_price - state.btc_opening_price
                if side == "up" and btc_gap < MIN_BTC_DISTANCE:
                    log.info(f"  T-{time_left}s: BTC too close for UP — "
                             f"live=${state.btc_live_price:.2f} open=${state.btc_opening_price:.2f} "
                             f"gap=+${btc_gap:.2f} (need +${MIN_BTC_DISTANCE:.0f}) — retrying...")
                    continue
                elif side == "down" and (-btc_gap) < MIN_BTC_DISTANCE:
                    log.info(f"  T-{time_left}s: BTC too close for DOWN — "
                             f"live=${state.btc_live_price:.2f} open=${state.btc_opening_price:.2f} "
                             f"gap=-${-btc_gap:.2f} (need -${MIN_BTC_DISTANCE:.0f}) — retrying...")
                    continue
                else:
                    log.info(f"  BTC distance OK: live=${state.btc_live_price:.2f} "
                             f"open=${state.btc_opening_price:.2f} gap=${btc_gap:+.2f}")
            else:
                log.info(f"  BTC distance: Chainlink not ready — skipping check "
                         f"(live={state.btc_live_price:.2f} open={state.btc_opening_price:.2f})")

            # Gate 6: BTC volatility (Chainlink)
            # Skip if BTC has been swinging too wildly (high-low range > $150
            # in last 60 seconds). Wild BTC movement means the Polymarket price
            # can flip even at 99% — the outcome isn't settled yet.
            if len(state.btc_price_history) >= 3:
                recent_prices = [p for _, p in state.btc_price_history]
                btc_range = max(recent_prices) - min(recent_prices)
                if btc_range > BTC_MAX_VOLATILITY:
                    log.info(f"  T-{time_left}s: BTC too volatile — "
                             f"range=${btc_range:.2f} over last {BTC_VOLATILITY_SEC}s "
                             f"(max=${BTC_MAX_VOLATILITY:.0f}) — retrying...")
                    continue
                else:
                    log.info(f"  BTC volatility OK: range=${btc_range:.2f} "
                             f"over last {BTC_VOLATILITY_SEC}s")

            # All gates passed — fire and keep retrying until filled or window closes
            state.trade_fired = True
            await _do_buy(client, session, state, side, token_id, price, bal, time_left)

        elif time_left > ENTRY_WINDOW_SEC:
            # Waiting for entry window — log every 10s via wall-clock
            if now_wall - state.last_status_ts >= 10.0:
                ups = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
                dns = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
                log.info(f"  T-{time_left}s | UP={ups[-1]:.3f} DOWN={dns[-1]:.3f}" if ups and dns else f"  T-{time_left}s | waiting for price ticks...")

# ─── Trade actions ────────────────────────────────────────────────────────────
async def _do_buy(client, session, state: BotState, side, token_id, price, bal_before, time_left=30):
    """
    Retry buy until filled, time runs out, or we hit MIN_FIRE_BUFFER.
    - Attempt 1: uses presigned_order if available (saves ~20-50ms signing latency)
    - Attempts 2+: re-signs fresh order each time (signature may have expired)
    - Stops if time_left drops to MIN_FIRE_BUFFER or position filled
    """
    stake = state.current_stake  # snapshot compound stake for this trade
    attempt = 0
    deadline = time.time() + max(time_left - MIN_FIRE_BUFFER, 2)
    last_order_status = ""   # track final accepted status to skip cancel on matched orders

    while time.time() < deadline:
        attempt += 1
        now_left = state.market.end_ts - int(time.time() + state.clock_offset)

        if now_left <= MIN_FIRE_BUFFER:
            log.info(f"  Buy abort: only {now_left}s left, too close to close.")
            break

        # Attempt 1: use presigned order if it was built for this token
        # IMPORTANT: verify the price is still >= BASE_THRESHOLD before using it.
        # If price dropped since presign time, discard and re-sign at current price.
        if attempt == 1 and state.presigned_order and state.presigned_for == token_id:
            if price >= BASE_THRESHOLD:
                order = state.presigned_order
                log.info(f"  Buy attempt 1 (presigned) @ T-{now_left}s price={price:.4f}")
            else:
                log.warning(f"  Presigned order discarded — current price {price:.4f} < {BASE_THRESHOLD} threshold. Re-signing.")
                state.presigned_order = None
                state.presigned_for   = None
                order = presign_order(client, state.market, token_id, price, stake=stake)
                if not order:
                    log.warning(f"  Attempt {attempt}: presign failed, retrying in 1s...")
                    await asyncio.sleep(1)
                    continue
                log.info(f"  Buy attempt {attempt} (fresh sign) @ T-{now_left}s price={price:.4f}")
        else:
            # Re-sign fresh order for all retries
            order = presign_order(client, state.market, token_id, price, stake=stake)
            if not order:
                log.warning(f"  Attempt {attempt}: presign failed, retrying in 1s...")
                await asyncio.sleep(1)
                continue
            log.info(f"  Buy attempt {attempt} (fresh sign) @ T-{now_left}s price={price:.4f}")
        try:
            resp = client.post_order(order, OrderType.GTC)
            size_matched = float(resp.get("size_matched", 0)) if resp else 0
            status = resp.get("status", "") if resp else ""
            log.info(f"  Attempt {attempt} resp: status={status} size_matched={size_matched}")

            if status in ("live", "matched", "filled") or size_matched > 0:
                log.info(f"  ✅ Order accepted on attempt {attempt} (status={status}) — stopping retries")
                last_order_status = status
                break  # Order is in the book or filled — do NOT retry

            # Rate limit: 1s between retries (well under Polymarket's 10 req/s limit)
            await asyncio.sleep(1)

        except Exception as e:
            err = str(e)
            if "429" in err or "too many" in err.lower():
                log.warning(f"  Rate limited on attempt {attempt}, waiting 3s...")
                await asyncio.sleep(3)
            elif "order couldn" in err.lower() or "no orders found" in err.lower():
                log.info(f"  Attempt {attempt}: no match yet, retrying in 1s...")
                await asyncio.sleep(1)
            elif "invalid signature" in err.lower():
                log.warning(f"  Attempt {attempt}: signature error, re-signing...")
                await asyncio.sleep(0.5)
            else:
                log.error(f"  Attempt {attempt} error: {e}")
                await asyncio.sleep(1)
        continue

    # ── Post-loop fill detection ──────────────────────────────────────────────
    # When the order lands as "live" or "matched" (resting GTC), the balance
    # won't have changed yet.  Poll for up to ~25s so a passive fill is detected
    # before we give up and call it unmatched.
    #
    # CRITICAL FIX: status="matched" means Polymarket accepted and matched the
    # order — it IS filled even when size_matched=0.0 (a known API quirk where
    # the size_matched field is not populated in the initial response).
    # We MUST NOT cancel or declare unmatched in this case.
    # We also do NOT stop polling early because now_left <= MIN_FIRE_BUFFER —
    # the market closing does not prevent us detecting the balance delta.
    MAX_FILL_WAIT = 40          # seconds to poll — long enough to span market close + settle
    poll_interval = 2
    elapsed = 0
    while elapsed < MAX_FILL_WAIT:
        bal_after_attempt = get_balance(client)
        filled_amount = bal_before - bal_after_attempt
        if filled_amount > 0.01:
            break
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    else:
        bal_after_attempt = get_balance(client)
        filled_amount = bal_before - bal_after_attempt

    if filled_amount > 0.01:  # balance dropped = money spent = filled
        actual_price = price
        shares = round(filled_amount / actual_price, 6)
        log.info(f"✅ BUY CONFIRMED: spent=${filled_amount:.4f} shares={shares:.6f} @ {actual_price:.4f} after {attempt} attempt(s) ({elapsed}s poll)")
        state.position = Position(
            side=side, token_id=token_id, entry_price=actual_price,
            shares=shares, stake=filled_amount, cycle_id=_cycle_id(),
            market=state.market,
        )
        state.last_balance = bal_after_attempt
        save_trade(TradeRecord(
            cycle_id=state.position.cycle_id, side=side,
            entry_price=actual_price, exit_price=0,
            shares_held=shares, stake=filled_amount, outcome="open",
            payout=0, gross_profit=0, fee_usdc=0, net_profit=0,
            balance_before=bal_before, balance_after=bal_after_attempt,
            market_slug=state.market.slug, timestamp=_ts(),
        ))
        return

    # Order never filled — cancel it so it doesn't fill after we advance.
    # IMPORTANT: Do NOT cancel if the order was accepted (matched/live/filled) —
    # it has already filled or is in the book and the balance poll above just
    # didn't catch it in time.  Cancelling it would waste the fill.
    if last_order_status not in ("matched", "live", "filled"):
        try:
            client.cancel_all()
            log.info("  Cancelled unfilled GTC order.")
        except Exception:
            pass
    else:
        log.info(f"  Order was accepted (status={last_order_status}) — skipping cancel, "
                 f"balance poll did not confirm fill in time.")
        # Even though balance poll timed out, the order DID match — treat as filled
        # using the expected spend amount so the position is tracked and redeemed.
        shares = round(stake / price, 6)
        bal_after_attempt = get_balance(client)
        log.warning(f"  ⚠️ FORCE-SETTING position from accepted order: "
                    f"shares={shares:.6f} @ {price:.4f} | bal_before=${bal_before:.4f} bal_now=${bal_after_attempt:.4f}")
        state.position = Position(
            side=side, token_id=token_id, entry_price=price,
            shares=shares, stake=stake, cycle_id=_cycle_id(),
            market=state.market,
        )
        state.last_balance = bal_after_attempt
        save_trade(TradeRecord(
            cycle_id=state.position.cycle_id, side=side,
            entry_price=price, exit_price=0,
            shares_held=shares, stake=stake, outcome="open",
            payout=0, gross_profit=0, fee_usdc=0, net_profit=0,
            balance_before=bal_before, balance_after=bal_after_attempt,
            market_slug=state.market.slug, timestamp=_ts(),
        ))
        return

    log.info(f"Buy unmatched after {attempt} attempt(s) — no fill.")
    save_trade(TradeRecord(
        cycle_id=_cycle_id(), side=side, entry_price=price, exit_price=0,
        shares_held=0, stake=stake, outcome="unmatched",
        payout=0, gross_profit=0, fee_usdc=0, net_profit=0,
        balance_before=bal_before, balance_after=bal_before,
        market_slug=state.market.slug, timestamp=_ts(),
    ))
    return

def _builder_creds():
    key = (os.environ.get("BUILDER_KEY")
           or os.environ.get("BUILDER_API_KEY")
           or os.environ.get("POLY_BUILDER_API_KEY"))
    secret = os.environ.get("BUILDER_SECRET") or os.environ.get("POLY_BUILDER_SECRET")
    passphrase = (os.environ.get("BUILDER_PASSPHRASE")
                  or os.environ.get("BUILDER_PASS_PHRASE")
                  or os.environ.get("POLY_BUILDER_PASSPHRASE"))
    if not (key and secret and passphrase):
        return None
    return key, secret, passphrase

# ══════════════════════════════════════════════════════════════════════════════
#  REDEEM ENGINE v6 — Three-layer cascade for Magic/email Proxy Wallet accounts
#
#  Why v5's direct web3 approach failed:
#    Your funds live inside a Polymarket Proxy Contract (EIP-1167 minimal proxy),
#    NOT at your raw EOA address. Calling redeemPositions() directly from your
#    EOA always reverts because the CTF contract sees no tokens at your EOA —
#    they're at the proxy address. You must route calls THROUGH the proxy.
#
#  The three layers below all route correctly through the proxy:
#    Layer 1: Official relayer SDK with RelayerTxType.PROXY  (gasless)
#    Layer 2: poly-web3 library (community Python port, also gasless)
#    Layer 3: Direct proxy.forward() via web3.py             (needs ~$0.001 MATIC)
# ══════════════════════════════════════════════════════════════════════════════

# Polymarket Proxy Factory for MagicLink/email accounts (Polygon mainnet)
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

def _to_bytes32(hex_str: str) -> bytes:
    h = (hex_str or "").lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) > 64:
        raise ValueError("hex too long for bytes32")
    return bytes.fromhex(h.zfill(64))

def _encode_redeem_positions(condition_id: str) -> Optional[str]:
    """ABI-encode the redeemPositions(address,bytes32,bytes32,uint256[]) calldata."""
    try:
        from eth_utils import keccak, to_checksum_address
        from eth_abi import encode
    except Exception as e:
        log.warning(f"[REDEEM] ABI encoder unavailable: {e}")
        return None
    try:
        selector = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        args = encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [
                to_checksum_address(USDC_E),
                _to_bytes32(PARENT_COLLECTION_ID),
                _to_bytes32(condition_id),
                [1, 2],
            ],
        )
        return "0x" + (selector + args).hex()
    except Exception as e:
        log.warning(f"[REDEEM] Encode redeemPositions failed: {e}")
        return None

# ─── LAYER 1: Official py_builder_relayer_client with RelayerTxType.PROXY ─────
def _redeem_via_relayer(condition_id: str) -> bool:
    """
    Route redeemPositions through the Polymarket relayer using the PROXY tx type.
    Uses inspect.signature to detect the exact RelayClient constructor at runtime,
    so this works correctly regardless of which SDK version is installed.
    Requires: BUILDER_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE in .env
    """
    if not condition_id:
        log.warning("[REDEEM L1] condition_id missing.")
        return False

    creds = _builder_creds()
    if not creds:
        log.warning("[REDEEM L1] Builder creds missing — skipping relayer layer.")
        return False

    try:
        from py_builder_relayer_client.client import RelayClient
        import py_builder_relayer_client.models as br_models
        from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
        import inspect
    except ImportError as e:
        log.warning(f"[REDEEM L1] SDK not installed: {e}")
        return False

    # ── Resolve RelayerTxType (name varies by SDK version) ────────────────────
    RelayerTxType = (
        getattr(br_models, "RelayerTxType", None)
        or getattr(br_models, "RelayTxType", None)
    )
    if RelayerTxType is None:
        class _ShimTxType:
            PROXY = "PROXY"
            SAFE  = "SAFE"
        RelayerTxType = _ShimTxType

    # ── Resolve Transaction class (name also varies) ───────────────────────────
    TransactionCls = getattr(br_models, "Transaction", None)
    if TransactionCls is None:
        class TransactionCls:  # type: ignore
            def __init__(self, to: str, data: str, value: str):
                self.to, self.data, self.value = to, data, value

    calldata = _encode_redeem_positions(condition_id)
    if not calldata:
        return False

    key, secret, passphrase = creds
    rpc_url = os.environ.get("POLYGON_RPC_URL") or os.environ.get("RPC_URL")

    try:
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=key, secret=secret, passphrase=passphrase
            )
        )
        proxy_tx_type = RelayerTxType.PROXY

        # ── Introspect RelayClient.__init__ to build the right call ───────────
        # The error "Invalid chainID: 0xecc10d3f..." means the SDK received
        # PRIVATE_KEY where it expected chain_id — wrong positional arg order.
        # We inspect the real parameter names to pass args correctly.
        try:
            rc_sig    = inspect.signature(RelayClient.__init__)
            rc_params = [p for p in rc_sig.parameters if p != "self"]
        except Exception:
            rc_params = []

        relay_client = None

        if rc_params:
            # Build kwargs from actual param names — no positional guessing
            rc_kwargs = {}
            param_map = {
                "url":            RELAYER_URL,
                "host":           RELAYER_URL,
                "relayer_url":    RELAYER_URL,
                "chain_id":       CHAIN_ID,
                "chainId":        CHAIN_ID,
                "private_key":    PRIVATE_KEY,
                "key":            PRIVATE_KEY,
                "builder_config": builder_config,
                "config":         builder_config,
                "relay_tx_type":  proxy_tx_type,
                "tx_type":        proxy_tx_type,
                "transaction_type": proxy_tx_type,
                "rpc_url":        rpc_url,
            }
            for p in rc_params:
                if p in param_map and param_map[p] is not None:
                    rc_kwargs[p] = param_map[p]
            try:
                relay_client = RelayClient(**rc_kwargs)
            except Exception as e:
                log.warning(f"[REDEEM L1] Introspected constructor failed: {e}")

        # Fallback: brute-force every known signature if introspection failed
        if relay_client is None:
            _attempts = [
                ((RELAYER_URL, CHAIN_ID, PRIVATE_KEY, builder_config),
                 {"relay_tx_type": proxy_tx_type, "rpc_url": rpc_url}),
                ((RELAYER_URL, CHAIN_ID, PRIVATE_KEY, builder_config),
                 {"relay_tx_type": proxy_tx_type}),
                ((RELAYER_URL, CHAIN_ID, PRIVATE_KEY, builder_config, proxy_tx_type), {}),
                ((RELAYER_URL, PRIVATE_KEY, builder_config, proxy_tx_type), {}),
                ((RELAYER_URL, PRIVATE_KEY, builder_config), {}),
            ]
            _errs = []
            for _a, _k in _attempts:
                try:
                    relay_client = RelayClient(*_a, **_k)
                    break
                except (TypeError, Exception) as _e:
                    _errs.append(str(_e))
            if relay_client is None:
                log.warning("[REDEEM L1] All RelayClient constructors failed:")
                for _e in _errs:
                    log.warning(f"[REDEEM L1]   → {_e}")
                return False

        # ── Set tx type post-construction if setter exists ────────────────────
        # Some SDK versions don't accept tx_type in constructor but have a setter
        for _setter in ("set_relay_tx_type", "set_tx_type", "set_transaction_type"):
            _fn = getattr(relay_client, _setter, None)
            if _fn:
                try:
                    _fn(proxy_tx_type)
                except Exception:
                    pass
                break

        # ── Submit the redeem transaction ─────────────────────────────────────
        tx = TransactionCls(to=CTF_CONTRACT, data=calldata, value="0")
        resp = relay_client.execute([tx], "Redeem CTF positions")
        try:
            result = resp.wait()
            log.info(f"[REDEEM L1] ✅ Submitted: {result}")
        except Exception:
            pass
        return True

    except Exception as e:
        log.debug(f"[REDEEM L1] failed (expected for proxy wallets): {e}")
        return False

# ─── LAYER 2: poly-web3 library (community Python relayer port) ───────────────
def _redeem_via_poly_web3(condition_id: str) -> bool:
    """
    Use the poly-web3 library — a Python rewrite of Polymarket's official
    TypeScript builder-relayer-client. Supports both Proxy and Safe wallets.

    Install: pip install poly-web3
    Requires: BUILDER_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE in .env
    """
    if not condition_id:
        return False

    creds = _builder_creds()
    if not creds:
        log.warning("[REDEEM L2] Builder creds missing — skipping poly-web3 layer.")
        return False

    try:
        from poly_web3 import RELAYER_URL as PW3_RELAYER_URL, PolyWeb3Service
        from py_builder_relayer_client.client import RelayClient as PW3RelayClient
        from py_builder_signing_sdk.config import BuilderConfig as PW3BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds as PW3Creds
    except ImportError as e:
        log.warning(f"[REDEEM L2] poly-web3 not installed: {e}")
        log.warning("[REDEEM L2] Install with: pip install poly-web3")
        return False

    key, secret, passphrase = creds
    try:
        import inspect

        relay_url = os.environ.get("POLYMARKET_RELAYER_URL", PW3_RELAYER_URL)

        builder_config = PW3BuilderConfig(
            local_builder_creds=PW3Creds(key=key, secret=secret, passphrase=passphrase)
        )

        # Build relayer client — try all known arg signatures
        relayer_client = None
        for _rc_args, _rc_kwargs in [
            ((relay_url, CHAIN_ID, PRIVATE_KEY, builder_config), {}),
            ((relay_url, PRIVATE_KEY, builder_config), {}),
            ((relay_url, CHAIN_ID, PRIVATE_KEY), {}),
        ]:
            try:
                relayer_client = PW3RelayClient(*_rc_args, **_rc_kwargs)
                break
            except TypeError:
                pass
        if relayer_client is None:
            log.warning("[REDEEM L2] Could not create poly-web3 RelayClient")
            return False

        # Introspect PolyWeb3Service constructor to discover accepted params
        try:
            sig = inspect.signature(PolyWeb3Service.__init__)
            params = set(sig.parameters.keys()) - {"self"}
        except Exception:
            params = set()

        # Build kwargs based on what the service actually accepts
        clob = ClobClient(
            CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            signature_type=1, funder=FUNDER_ADDRESS,
        )
        clob.set_api_creds(clob.create_or_derive_api_creds())

        svc_kwargs = {}
        if "clob_client" in params:      svc_kwargs["clob_client"]      = clob
        if "relayer_client" in params:   svc_kwargs["relayer_client"]   = relayer_client
        if "private_key" in params:      svc_kwargs["private_key"]      = PRIVATE_KEY
        if "chain_id" in params:         svc_kwargs["chain_id"]         = CHAIN_ID
        if "key" in params:              svc_kwargs["key"]              = PRIVATE_KEY
        if "funder" in params:           svc_kwargs["funder"]           = FUNDER_ADDRESS

        # Fallback: try positional construction
        service = None
        for _svc_try in [
            lambda: PolyWeb3Service(**svc_kwargs),
            lambda: PolyWeb3Service(clob, relayer_client),
            lambda: PolyWeb3Service(PRIVATE_KEY, relayer_client),
            lambda: PolyWeb3Service(relayer_client),
        ]:
            try:
                service = _svc_try()
                break
            except TypeError:
                pass
        if service is None:
            log.warning("[REDEEM L2] Could not instantiate PolyWeb3Service — unknown constructor signature")
            return False

        cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id

        # Try known method names for redemption
        for method_name in ("redeem_positions", "redeem", "claim", "settle"):
            method = getattr(service, method_name, None)
            if method:
                result = method(cid)
                # Only log if we got a real tx receipt (non-empty list/dict with tx data)
                if result and isinstance(result, (list, dict)) and result != []:
                    log.info(f"[REDEEM L2] ✅ Redeemed {cid[:20]}... tx={result}")
                return True

        log.warning("[REDEEM L2] No redeem method found on PolyWeb3Service")
        return False
    except Exception as e:
        log.warning(f"[REDEEM L2] failed: {e}")
        return False

# ─── LAYER 3: Direct proxy.forward() via web3.py ──────────────────────────────
def _redeem_via_proxy_forward(condition_id: str) -> bool:
    """
    Call the Polymarket Proxy contract's forward() function directly using web3.py.

    This is the CORRECT direct approach for Magic/email proxy wallets:
      - Your EOA is the owner of the proxy contract
      - You call proxy.forward(CTF_CONTRACT, redeemPositions_calldata, 0) from your EOA
      - The proxy contract executes it internally, so CTF sees the proxy address
        as msg.sender — which is where the tokens actually are

    This is different from v5's broken approach which called CTF directly from EOA.

    Costs: ~0.0001–0.001 MATIC in gas (less than $0.001)
    Requires: web3 + eth_account installed, small MATIC balance at your EOA
    """
    if not WEB3_AVAILABLE:
        log.warning("[REDEEM L3] web3/eth_account not installed.")
        log.warning("[REDEEM L3] Install with: pip install web3 eth-account")
        return False
    if not condition_id:
        log.warning("[REDEEM L3] condition_id missing.")
        return False

    # Connect to Polygon RPC
    rpc_urls = [
        os.environ.get("POLYGON_RPC_URL"),
        os.environ.get("RPC_URL"),
        "https://rpc.ankr.com/polygon",
        "https://polygon.llamarpc.com",
        "https://1rpc.io/matic",
        "https://polygon-rpc.com",
        "https://polygon.meowrpc.com",
    ]
    w3 = None
    for url in rpc_urls:
        if not url:
            continue
        try:
            tmp = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if tmp.is_connected():
                w3 = tmp
                log.info(f"[REDEEM L3] Connected to RPC: {url}")
                break
        except Exception:
            pass

    if not w3:
        log.warning("[REDEEM L3] Cannot connect to any Polygon RPC node.")
        return False

    calldata = _encode_redeem_positions(condition_id)
    if not calldata:
        return False

    try:
        from eth_utils import to_checksum_address
    except ImportError:
        log.warning("[REDEEM L3] eth_utils not available.")
        return False

    try:
        eoa_account = Account.from_key(PRIVATE_KEY)
        eoa_address = eoa_account.address

        # Check EOA has MATIC for gas
        matic_balance = w3.eth.get_balance(eoa_address)
        matic_eth = w3.from_wei(matic_balance, "ether")
        log.info(f"[REDEEM L3] EOA {eoa_address[:16]}... MATIC balance: {matic_eth:.6f}")
        if matic_balance < w3.to_wei(0.0005, "ether"):
            log.warning(
                f"[REDEEM L3] EOA has only {matic_eth:.6f} MATIC — may not be enough for gas.\n"
                f"             Send at least 0.001 MATIC to {eoa_address} and retry."
            )
            # Still attempt — might just barely work

        # Use FUNDER_ADDRESS directly as the proxy wallet address.
        # FUNDER_ADDRESS IS the proxy contract address — it was set in .env
        # from the address shown on polymarket.com. No derivation needed.
        if not FUNDER_ADDRESS:
            log.warning("[REDEEM L3] FUNDER_ADDRESS not set in .env — cannot locate proxy contract.")
            return False
        proxy_address = to_checksum_address(FUNDER_ADDRESS)
        log.info(f"[REDEEM L3] Using proxy wallet: {proxy_address}")

        # Polymarket Proxy ABI — forward(address to, bytes data, uint256 value)
        # This is the function that executes arbitrary calls through the proxy
        PROXY_FORWARD_ABI = [
            {
                "name": "forward",
                "type": "function",
                "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "to",    "type": "address"},
                    {"name": "data",  "type": "bytes"},
                    {"name": "value", "type": "uint256"},
                ],
                "outputs": [{"name": "", "type": "bytes"}],
            }
        ]

        proxy_contract = w3.eth.contract(
            address=proxy_address,
            abi=PROXY_FORWARD_ABI,
        )

        # Verify the proxy exists (has code)
        code = w3.eth.get_code(proxy_address)
        if code == b"" or code == "0x":
            log.warning(
                f"[REDEEM L3] No contract code at {proxy_address}.\n"
                f"             Proxy may not be deployed. Log in to polymarket.com\n"
                f"             and make one trade to deploy your proxy wallet."
            )
            return False

        ctf_address = to_checksum_address(CTF_CONTRACT)
        calldata_bytes = bytes.fromhex(calldata[2:])  # strip 0x

        nonce    = w3.eth.get_transaction_count(eoa_address)
        gas_price = int(w3.eth.gas_price * 1.3)

        txn = proxy_contract.functions.forward(
            ctf_address, calldata_bytes, 0
        ).build_transaction({
            "from":     eoa_address,
            "nonce":    nonce,
            "gas":      350000,
            "gasPrice": gas_price,
            "chainId":  CHAIN_ID,
        })

        signed = eoa_account.sign_transaction(txn)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info(f"[REDEEM L3] Proxy forward tx sent → https://polygonscan.com/tx/{tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        success = receipt["status"] == 1
        if success:
            log.info(f"[REDEEM L3] ✅ SUCCESS via proxy.forward() (block {receipt['blockNumber']}, gas {receipt['gasUsed']})")
        else:
            log.warning(f"[REDEEM L3] Tx reverted. Check: https://polygonscan.com/tx/{tx_hash.hex()}")
        return success

    except Exception as e:
        log.warning(f"[REDEEM L3] proxy.forward() failed: {e}", exc_info=True)
        return False

async def _check_redeemable(session: aiohttp.ClientSession, condition_id: str) -> bool:
    """
    Poll Polymarket Data API to see if the winning position is redeemable.
    Returns True when redeemable=true is seen for this condition.
    """
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    if not funder:
        log.warning("[REDEEM] POLYMARKET_FUNDER_ADDRESS not set — cannot check redeemable status")
        return False

    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    try:
        async with session.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder.lower(), "sizeThreshold": "0.01"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            positions = await r.json()

        if not isinstance(positions, list):
            log.warning(f"[REDEEM] Unexpected positions response: {str(positions)[:100]}")
            return False

        redeemable = [
            p for p in positions
            if p.get("redeemable") is True
            and p.get("conditionId", "").lower() == cid.lower()
        ]
        if redeemable:
            size = redeemable[0].get("size", "?")
            log.info(f"[REDEEM] ✅ Position redeemable! size={size} tokens")
            return True
        else:
            log.info("[REDEEM] Position not yet redeemable (oracle still confirming)...")
            return False

    except Exception as e:
        log.warning(f"[REDEEM] Data API check failed: {e}")
        return False

# Keep old name as alias so _background_redeem can call it unchanged
async def _auto_redeem(session: aiohttp.ClientSession, condition_id: str) -> bool:
    return await _check_redeemable(session, condition_id)

async def _resolve_position(client: ClobClient, session: aiohttp.ClientSession, state: BotState):
    """
    Called when WSS market_resolved fires.
    Determines win/loss, saves the trade record, then launches a background
    redemption task. Compounding is updated ONLY after redeem confirms to prevent
    the next trade from firing on stake we don't yet have in the account.
    """
    pos  = state.position
    won  = (state.resolved == pos.token_id)
    outcome = "win" if won else "loss"
    fee_bps = float(state.market.fee_rate or 0)
    trade_stake = pos.stake if pos.stake and pos.stake > 0 else state.current_stake
    payout, gross, fee_usdc, net = calc_profit(trade_stake, pos.entry_price, 1.0, fee_bps, outcome)

    bal_before = state.last_balance
    if won:
        state.consecutive_wins += 1
        if state.consecutive_wins >= 5:
            state.consecutive_losses = 0
            state.consecutive_wins   = 0
            log.info("[STREAK] 5 consecutive wins — adaptive threshold reset to base.")
        # ── Compounding FIX: set pending_redeem=True and store expected net profit.
        # compound_base is NOT updated yet — we wait until _background_redeem
        # confirms USDC arrived. This prevents trading on phantom balance.
        state.last_net_profit = net
        state.pending_redeem  = True
        log.info(f"[COMPOUND] WIN detected — pending redeem. Will compound +${net:.4f} after USDC confirmed.")
    else:
        state.consecutive_losses += 1
        state.consecutive_wins   = 0
        state.last_net_profit    = 0.0
        state.compound_base      = STAKE
        state.current_stake      = STAKE
        state.pending_redeem     = False
        log.info(f"[COMPOUND] LOSS — stake reset to base ${STAKE:.2f}")

    log.info(f"{'WIN ✅' if won else 'LOSS ❌'}: gross={gross:+.4f} fee={fee_usdc:.5f} net={net:+.4f}")

    if won:
        cid = pos.market.condition_id
        if cid:
            log.info(f"[REDEEM] WIN detected — launching background redemption task for {cid[:20]}...")
            asyncio.create_task(
                _background_redeem(session, client, state, cid, pos, gross, fee_usdc, net, bal_before)
            )
        else:
            log.warning("[REDEEM] WIN but condition_id is empty — cannot auto-redeem.")
            # No redeem possible — apply compounding now with current balance
            bal_after = get_balance(client)
            state.last_balance   = bal_after
            state.compound_base  = round(state.compound_base + net, 4)
            state.current_stake  = state.compound_base
            state.pending_redeem = False
            log.info(f"[COMPOUND] Applied immediately (no CID): compound_base=${state.compound_base:.4f}")
            _save_resolved_trade(pos, outcome, payout, gross, fee_usdc, net, bal_before, bal_after)
    else:
        bal_after = get_balance(client)
        state.last_balance = bal_after
        log.info(f"Balance after loss: ${bal_after:.4f}")
        _save_resolved_trade(pos, outcome, payout, gross, fee_usdc, net, bal_before, bal_after)


async def _background_redeem(
    session: aiohttp.ClientSession,
    client: ClobClient,
    state: BotState,
    condition_id: str,
    pos,
    gross: float,
    fee_usdc: float,
    net: float,
    bal_before: float,
):
    """
    Background task: waits for on-chain settlement then redeems via 3-layer cascade.

    IMPORTANT: This runs fire-and-forget while the trading loop advances to the next
    market. It must NOT use state.last_balance as a baseline — the trading loop will
    overwrite it with the next trade's balance. Instead we use an isolated snapshot
    taken at task start, and only push back to state.last_balance on confirmed claim.
    """
    POLL_INTERVAL       = 8     # seconds between redeemable checks
    HARD_TIMEOUT        = 600   # 10-minute hard deadline
    CASCADE_RETRY_AFTER = 60    # retry cascade if balance hasn't changed after this many seconds

    log.info(f"[REDEEM] Background task started for {condition_id[:20]}... "
             f"— polling every {POLL_INTERVAL}s, hard timeout {HARD_TIMEOUT}s")

    # Register immediately so redeem_loop never fires a second cascade for this CID
    state.redeemed_condition_ids.add(condition_id)

    deadline           = time.time() + HARD_TIMEOUT
    # Take our OWN balance snapshot — do not touch state.last_balance until confirmed
    bal_snapshot       = get_balance(client)
    last_cascade_ts    = 0.0
    attempt            = 0

    while time.time() < deadline:
        attempt += 1
        elapsed = int(time.time() - deadline + HARD_TIMEOUT)

        await asyncio.sleep(POLL_INTERVAL)

        # ── Check if USDC arrived ─────────────────────────────────────────────
        bal_now = get_balance(client)
        gained  = bal_now - bal_snapshot
        log.info(f"[REDEEM] Poll {attempt} (T+{elapsed}s) | "
                 f"bal=${bal_now:.4f} gained=${gained:+.4f}")

        if gained > 0.01:
            log.info(f"[REDEEM] ✅ CLAIMED! ${bal_snapshot:.4f} → ${bal_now:.4f} (+${gained:.4f})")
            # ── COMPOUNDING: apply NOW that USDC is confirmed in account ─────
            # This is the only safe place — we know the money is actually there.
            state.compound_base  = round(state.compound_base + net, 4)
            state.current_stake  = state.compound_base
            state.pending_redeem = False
            log.info(f"[COMPOUND] ✅ CONFIRMED — compound_base=${state.compound_base:.4f} "
                     f"(+${net:.4f} net profit after redeem)")
            # NOW update state — this is the only safe place to do it
            state.last_balance = bal_now
            _save_resolved_trade(pos, "win", gained, gross, fee_usdc, net, bal_before, bal_now)
            return

        # ── Check redeemable status ───────────────────────────────────────────
        is_redeemable = await _auto_redeem(session, condition_id)

        if not is_redeemable:
            log.info(f"[REDEEM] Not yet redeemable — next check in {POLL_INTERVAL}s...")
            continue

        # ── Run cascade if not recently attempted ─────────────────────────────
        time_since_last_cascade = time.time() - last_cascade_ts
        if last_cascade_ts == 0.0 or time_since_last_cascade >= CASCADE_RETRY_AFTER:
            log.info(f"[REDEEM] Redeemable confirmed — launching 3-layer cascade "
                     f"(last attempt {int(time_since_last_cascade)}s ago)...")
            last_cascade_ts = time.time()

            l1_ok = await asyncio.to_thread(_redeem_via_relayer, condition_id)
            if l1_ok:
                log.info("[REDEEM] Layer 1 submitted ✅")
            else:
                log.info("[REDEEM] Layer 1 failed — trying Layer 2...")
                l2_ok = await asyncio.to_thread(_redeem_via_poly_web3, condition_id)
                if l2_ok:
                    log.info("[REDEEM] Layer 2 submitted ✅")
                else:
                    log.info("[REDEEM] Layer 2 failed — trying Layer 3...")
                    l3_ok = await asyncio.to_thread(_redeem_via_proxy_forward, condition_id)
                    if l3_ok:
                        log.info("[REDEEM] Layer 3 submitted ✅")
                    else:
                        log.warning(
                            "[REDEEM] ⚠️ All 3 layers failed — will retry in 60s.\n"
                            "         Causes: no Builder creds (L1+L2), no MATIC (L3)."
                        )
                        last_cascade_ts = time.time() - CASCADE_RETRY_AFTER + 15
        else:
            log.info(f"[REDEEM] Cascade attempted {int(time_since_last_cascade)}s ago — "
                     f"waiting for USDC (retry cascade in "
                     f"{int(CASCADE_RETRY_AFTER - time_since_last_cascade)}s)...")

    # Hard timeout
    bal_final    = get_balance(client)
    gained_final = bal_final - bal_snapshot
    state.last_balance   = bal_final
    state.pending_redeem = False  # always clear — whether we got money or not
    if gained_final <= 0.01:
        # Cascade never succeeded — remove from seen set so redeem_loop can retry
        state.redeemed_condition_ids.discard(condition_id)
        # Compounding was deferred — apply anyway so next trade uses correct stake
        # (The money WILL arrive eventually via redeem_loop, just not confirmed yet)
        state.compound_base = round(state.compound_base + net, 4)
        state.current_stake = state.compound_base
        log.warning(
            f"[REDEEM] ⚠️ 10-min timeout, no USDC received. Handing off to redeem_loop for retry.\n"
            f"         compound_base tentatively updated to ${state.compound_base:.4f}.\n"
            f"         Condition ID: {condition_id}"
        )
    else:
        # USDC arrived during timeout window — apply compounding now
        state.compound_base = round(state.compound_base + net, 4)
        state.current_stake = state.compound_base
        log.warning(
            f"[REDEEM] ⚠️ 10-min timeout but USDC arrived: gained=${gained_final:.4f}.\n"
            f"         compound_base=${state.compound_base:.4f}\n"
            f"         Condition ID: {condition_id}"
        )
    _save_resolved_trade(pos, "win", gained_final, gross, fee_usdc, net, bal_before, bal_final)


def _save_resolved_trade(pos, outcome, payout, gross, fee_usdc, net, bal_before, bal_after):
    """Save the final trade record after resolution."""
    save_trade(TradeRecord(
        cycle_id=pos.cycle_id, side=pos.side,
        entry_price=pos.entry_price, exit_price=1.0 if outcome == "win" else 0.0,
        shares_held=pos.shares, stake=pos.stake if pos.stake and pos.stake > 0 else STAKE, outcome=outcome,
        payout=payout, gross_profit=gross, fee_usdc=fee_usdc, net_profit=net,
        balance_before=bal_before, balance_after=bal_after,
        market_slug=pos.market.slug, timestamp=_ts(),
    ))
async def _do_stop_loss(client: ClobClient, session: aiohttp.ClientSession,
                        state: BotState, exit_bid: float, brutal: bool = False):
    """
    Execute a stop-loss SELL. Only clears position if sell actually filled.

    brutal=True  → use execute_brutal_sell (price floor=0.01, maximum aggression,
                   fires even with <5s remain). Triggered at <= 93% bid.
    brutal=False → use execute_sell (gentler floor-dropping approach).
    """
    pos = state.position
    mode_label = "⚡ BRUTAL" if brutal else "STOP-LOSS"

    # Always fetch a fresh balance right before selling — state.last_balance
    # may be stale from the original buy and cause false "fill failed" detection.
    bal_before = get_balance(client)
    state.last_balance = bal_before

    if brutal:
        resp = await execute_brutal_sell(client, state.market, pos,
                                         clock_offset=state.clock_offset)
    else:
        resp = await execute_sell(client, state.market, pos, exit_bid,
                                  clock_offset=state.clock_offset)

    # Determine if fill succeeded: check size_matched first, then balance delta
    size_matched = float(resp.get("size_matched", 0)) if resp else 0
    bal_after    = get_balance(client)
    filled       = size_matched > 0 or bal_after > bal_before + 0.01

    state.last_balance = bal_after

    if not filled:
        log.error(
            f"  ❌ {mode_label} SELL FAILED after all retries. "
            f"bal_before=${bal_before:.4f} bal_after=${bal_after:.4f}. "
            f"Position kept open — will resolve at market close."
        )
        return  # do NOT clear position — redeem at close will handle it

    actual_exit = bal_after - bal_before if bal_after > bal_before else exit_bid
    # Use the actual per-share exit price for record keeping
    actual_exit_price = min(actual_exit / pos.shares, 1.0) if pos.shares > 0 else exit_bid
    fee_bps = float(state.market.fee_rate or 0)
    trade_stake = pos.stake if pos.stake and pos.stake > 0 else state.current_stake
    payout, gross, fee_usdc, net = calc_profit(trade_stake, pos.entry_price, actual_exit_price, fee_bps, "stop_loss")
    state.consecutive_losses += 1
    state.consecutive_wins   = 0
    # ── Compounding: reset both to BASE_STAKE on any stop-loss ───────────────
    state.last_net_profit = 0.0
    state.compound_base   = STAKE
    state.current_stake   = STAKE
    state.pending_redeem  = False  # clear any stale flag
    log.info(f"[COMPOUND] {mode_label} — stake reset to base ${STAKE:.2f}")

    log.info(f"✅ {mode_label} executed: entry={pos.entry_price:.4f} exit≈{actual_exit_price:.4f} | "
             f"gross={gross:+.4f} net={net:+.4f} | balance ${bal_before:.4f} → ${bal_after:.4f}")
    save_trade(TradeRecord(
        cycle_id=pos.cycle_id, side=pos.side, entry_price=pos.entry_price,
        exit_price=actual_exit_price, shares_held=pos.shares, stake=trade_stake,
        outcome="brutal_sell" if brutal else "stop_loss",
        payout=payout, gross_profit=gross,
        fee_usdc=fee_usdc, net_profit=net,
        balance_before=bal_before, balance_after=bal_after,
        market_slug=pos.market.slug, timestamp=_ts(),
    ))
    state.position = None

def _log_skip(market, side, price, reason, balance):
    if not market: return
    save_trade(TradeRecord(
        cycle_id=_cycle_id(), side=side, entry_price=price, exit_price=0,
        shares_held=0, stake=0, outcome="skip",
        payout=0, gross_profit=0, fee_usdc=0, net_profit=0,
        balance_before=balance, balance_after=balance,
        market_slug=market.slug, timestamp=_ts(), skip_reason=reason,
    ))

async def _advance_market(client: ClobClient, session: aiohttp.ClientSession, state: BotState):
    """Reset state and move to next 5-minute window."""
    # Cancel any open GTC orders so they don't linger into next cycle
    try:
        client.cancel_all()
        log.info("[ADVANCE] Cancelled open orders.")
    except Exception as e:
        log.warning(f"[ADVANCE] Cancel orders: {e}")

    try:
        server_ts = int(await asyncio.to_thread(client.get_server_time))
    except Exception:
        server_ts = int(time.time() + state.clock_offset)
    state.trade_fired       = False
    state.resolved          = None   # cleared here — never carry over to next market
    state.presigned_order   = None
    state.presigned_for     = None
    state.position          = None  # always clear position on advance
    state.price_history     = deque(maxlen=200)
    state.last_status_ts    = 0.0   # reset so status logs immediately on new cycle
    state.btc_opening_price = 0.0   # reset so next window captures fresh opening price
    state.btc_price_history = []    # reset volatility history for new window

    if state.next_market:
        m = await _gamma_slug(session, state.next_market.slug)
        # Only use the queued market if it ends within the next two candle windows (~10 min).
        # If end_ts is far in the future (e.g. a wrong slug queued hours ahead), discard it
        # and fall through to fetch_btc_market to find the correct next 5-min window.
        MAX_ADVANCE_SECONDS = 700  # ~2 candle windows (10 min + buffer)
        if m and server_ts < m.end_ts <= server_ts + MAX_ADVANCE_SECONDS:
            state.market      = enrich_market(client, m)
            state.next_market = None
            log.info(f"[ADVANCE] Loaded queued market: {m.slug} | ends in {m.end_ts - server_ts}s")
            return
        else:
            if m:
                log.warning(f"[ADVANCE] Queued market {state.next_market.slug} end_ts too far "
                            f"({m.end_ts - server_ts}s away) — discarding and scanning fresh...")
            else:
                log.warning(f"[ADVANCE] Queued market invalid or expired, scanning fresh...")
            state.next_market = None

    m = await fetch_btc_market(session, server_ts)
    if m:
        state.market = enrich_market(client, m)
    else:
        next_b = ((server_ts // 300) + 1) * 300
        wait   = max(next_b - server_ts - 10, 1)
        log.info(f"No market — sleeping {wait}s...")
        await asyncio.sleep(wait)

def _ts(): return datetime.utcnow().isoformat() + "Z"
def _cycle_id(): return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00Z")

async def redeem_loop(session: aiohttp.ClientSession, client: ClobClient,
                     state: BotState, stop: asyncio.Event):
    """
    Persistent background loop that catches any redeemable positions the trading
    loop may have missed (e.g. status=matched + balance-poll-timeout bug, or
    positions that weren't redeemable yet when the cycle advanced).

    Design:
      - Polls Data API once every 30s (well under any rate limit)
      - Skips condition IDs already submitted this session (state.redeemed_condition_ids)
      - Also skips the current active position's condition ID — _background_redeem
        already owns that one
      - Runs the L1→L2→L3 cascade once per new redeemable CID, then marks it done
      - No concurrent tasks, no inner polling loop — one pass, move on
    """
    SCAN_INTERVAL = 30   # seconds between scans
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    if not funder:
        log.warning("[REDEEM-LOOP] POLYMARKET_FUNDER_ADDRESS not set — orphan redeem loop disabled")
        return

    log.info(f"[REDEEM-LOOP] Started — scanning for orphan redeemable positions every {SCAN_INTERVAL}s")

    # CIDs where cascade was submitted but we haven't confirmed clearance yet.
    # Maps cid -> {"submitted_at": float, "retries": int}
    # A CID stays here until it disappears from the redeemable list (confirmed cleared)
    # OR it exceeds MAX_RETRIES — at which point we give up to avoid infinite spam.
    MAX_RETRIES = 10   # ~5 minutes at 30s interval before giving up
    pending_verification: dict = {}

    while not stop.is_set():
        await asyncio.sleep(SCAN_INTERVAL)
        if stop.is_set():
            break

        try:
            async with session.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder.lower(), "sizeThreshold": "0.01"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                positions = await r.json() if r.status == 200 else []

            if not isinstance(positions, list):
                continue

            current_redeemable_cids = {
                p.get("conditionId", "")
                for p in positions
                if p.get("redeemable") is True and p.get("conditionId", "")
            }

            # ── Verify previously submitted cascades ──────────────────────────
            # Only delete from pending_verification once a CID is confirmed cleared
            # (no longer in the redeemable set) OR it has hit MAX_RETRIES.
            for cid, info in list(pending_verification.items()):
                if cid not in current_redeemable_cids:
                    # Cleared on-chain — done
                    log.info(f"[REDEEM-LOOP] ✅ Confirmed cleared: {cid[:20]}...")
                    del pending_verification[cid]
                else:
                    retries = info["retries"]
                    age = int(time.time() - info["submitted_at"])
                    if retries >= MAX_RETRIES:
                        log.warning(f"[REDEEM-LOOP] ⚠️ Giving up on {cid[:20]}... after "
                                    f"{retries} retries ({age}s) — all layers consistently failing. "
                                    f"Manual redemption may be needed on polymarket.com.")
                        del pending_verification[cid]
                        # Keep in redeemed_condition_ids to permanently stop retrying
                    else:
                        log.debug(f"[REDEEM-LOOP] {cid[:20]}... pending ({age}s, retry {retries+1}/{MAX_RETRIES})")
                        # Allow retry this scan by removing from seen set
                        state.redeemed_condition_ids.discard(cid)
                        del pending_verification[cid]

            redeemable = [
                p for p in positions
                if p.get("redeemable") is True
                and p.get("conditionId", "") not in state.redeemed_condition_ids
            ]

            if not redeemable:
                continue  # nothing to do — no log spam

            for p in redeemable:
                cid  = p.get("conditionId", "")
                size = p.get("size", "?")
                if not cid:
                    continue

                prior_retries = pending_verification.get(cid, {}).get("retries", 0)

                log.debug(f"[REDEEM-LOOP] processing orphan {cid[:20]}... size={size}")

                # Mark as seen BEFORE cascade so we don't retry on next scan
                state.redeemed_condition_ids.add(cid)

                submitted = False
                l1_ok = await asyncio.to_thread(_redeem_via_relayer, cid)
                if l1_ok:
                    submitted = True
                else:
                    l2_ok = await asyncio.to_thread(_redeem_via_poly_web3, cid)
                    if l2_ok:
                        submitted = True
                    else:
                        l3_ok = await asyncio.to_thread(_redeem_via_proxy_forward, cid)
                        if l3_ok:
                            submitted = True

                if submitted:
                    pending_verification[cid] = {
                        "submitted_at": time.time(),
                        "retries": prior_retries + 1,
                    }
                else:
                    log.warning(f"[REDEEM-LOOP] ⚠️ All layers failed for {cid[:20]}... — will retry next scan")
                    # Remove from seen set so next scan retries
                    state.redeemed_condition_ids.discard(cid)

        except Exception as e:
            log.warning(f"[REDEEM-LOOP] scan error: {e}")

# ─── Entry point ──────────────────────────────────────────────────────────────
async def run_bot():
    assert PRIVATE_KEY and "YOUR" not in PRIVATE_KEY,    "Set POLYMARKET_PRIVATE_KEY in .env"
    assert FUNDER_ADDRESS and "YOUR" not in FUNDER_ADDRESS, "Set POLYMARKET_FUNDER_ADDRESS in .env"

    client = build_client()
    state  = BotState()
    stop   = asyncio.Event()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("=" * 65)
    log.info(" Polymarket BTC 5m Bot — v9 | Compounding | 99% | Chainlink")
    log.info(f" Stake: base=${STAKE:.2f} | compounds AFTER redeem confirms")
    log.info(f" Buy: >= {BASE_THRESHOLD*100:.0f}% | entry window T-{ENTRY_WINDOW_SEC}s | presign T-{PRESIGN_BEFORE}s")
    log.info(f" Stop-loss: sell if bid < {STOP_LOSS_BID*100:.0f}% AND > {STOP_LOSS_MIN_SEC}s remain")
    log.info(f" BRUTAL SELL: bid <= {BRUTAL_SELL_THRESHOLD*100:.0f}% at any time → sell at ANY price immediately")
    log.info(f" BTC filter: distance >= ${MIN_BTC_DISTANCE:.0f} | volatility <= ${BTC_MAX_VOLATILITY:.0f}/{BTC_VOLATILITY_SEC}s")
    log.info(f" IST block: no trades 01:00–08:00 IST (19:30–02:30 UTC)")
    log.info(" REDEEM: 3-layer cascade (relayer PROXY → poly-web3 → proxy.forward)")
    log.info("=" * 65)

    async with aiohttp.ClientSession() as session:
        # Clock sync check
        server_ts = int(await asyncio.to_thread(client.get_server_time))
        drift = abs(time.time() - server_ts)
        status = "OK" if drift < 3 else f"WARNING — {drift:.1f}s drift may affect timing"
        log.info(f"Clock drift: {drift:.2f}s [{status}]")

        # Balance check — warn but continue (SDK may misread; real check happens pre-trade)
        state.last_balance = get_balance(client)
        log.info(f"USDC balance: ${state.last_balance:.4f}")
        if state.last_balance < STAKE:
            log.warning(f"Balance shows ${state.last_balance:.4f} — if funded, SDK may be misreading. Continuing...")

        # Scan for any redeemable positions missed by previous runs (e.g. status=matched bug)
        # Handled by redeem_loop which starts immediately in asyncio.gather below.

        # Bootstrap first market
        m = await fetch_btc_market(session, server_ts)
        if m:
            state.market = enrich_market(client, m)
            log.info(f"Starting market: {state.market.slug} | ends in {state.market.end_ts - server_ts}s")
        else:
            log.info("No market yet — will auto-detect via WSS new_market event.")

        try:
            await asyncio.gather(
                run_market_wss(state, client, session, stop),
                run_chainlink_wss(state, stop),
                trading_loop(client, session, state, stop),
                heartbeat_loop(client, state, stop),
                cred_refresh_loop(client, stop),
                redeem_loop(session, client, state, stop),
                return_exceptions=False,
            )
        except Exception as e:
            if not stop.is_set():
                log.error(f"Fatal coroutine error: {e}", exc_info=True)
                # Outer restart loop in __main__ will handle recovery

    log.info("Bot stopped cleanly.")

if __name__ == "__main__":
    # Outer restart loop — if run_bot() crashes due to a fatal unhandled error,
    # wait 10 seconds and restart rather than dying permanently on Railway.
    import sys
    RESTART_DELAY = 10
    while True:
        try:
            asyncio.run(run_bot())
            break  # clean stop (SIGINT/SIGTERM) — do not restart
        except KeyboardInterrupt:
            log.info("Keyboard interrupt — bot stopped.")
            break
        except Exception as e:
            log.error(f"[CRASH] Bot crashed: {e}", exc_info=True)
            log.error(f"[CRASH] Restarting in {RESTART_DELAY}s...")
            time.sleep(RESTART_DELAY)
