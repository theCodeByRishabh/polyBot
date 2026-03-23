"""
Polymarket BTC 5-Minute Bot — v10

Core philosophy change vs v9:
  STOP-LOSS REMOVED ENTIRELY.
  The v9 stop-loss was the primary source of losses. Here's why:
    - You enter at 99¢. Your win is 1¢/share. Your stop-loss at 93¢ loses 6¢/share.
    - A stop-loss at T-15s on a market that recovers = a locked-in loss on a trade
      that would have won.
    - At T-12s with BTC $120+ away, there is no time for the stop-loss to "save" you.
      It only hurts you.

  The ONLY way to reduce losses is to enter BETTER, not exit faster.

v10 changes vs v9:
  1. ENTRY WINDOW tightened: T-15s to T-4s (was T-40s to T-3s)
     - At T-15s with a strong BTC gap, the market is near-certain.
     - Less time = less room for reversal after entry.

  2. PRESIGN window: T-20s to T-16s (was T-50s to T-41s)
     - Tighter pre-sign aligned with new entry window.

  3. MIN_BTC_DISTANCE raised: $120 (was $60)
     - Only trade when BTC has moved significantly away from opening price.
     - $120 gap at T-15s is near-impossible to reverse before close.

  4. BASE_THRESHOLD lowered to 0.93 (was 0.98)
     - At T-12s with $120+ BTC gap, the Polymarket ask is realistically 0.93–0.97,
       NOT 0.99. Requiring 0.99 at late entry = zero trades. 0.93 is the right
       threshold for this timeframe.

  5. STABILITY check tightened: MAX_STD_DEV 0.008 (was 0.015)
     - At late entry we only want dead-calm prices. Any wobble = skip.

  6. BTC_MAX_VOLATILITY tightened: $80 (was $150)
     - Choppier BTC = more risk even at T-15s. Be strict.

  7. STOP_LOSS_BID = 0.0  →  stop-loss DISABLED
     BRUTAL_SELL_THRESHOLD = 0.0  →  brutal sell DISABLED
     - All positions held to resolution. No more self-inflicted losses.

  8. MOMENTUM check upgraded:
     - Require last 5 ticks to be NON-DECLINING (was just 3 of 8).
     - If price is ticking down even at 0.94, skip — it may not hold.

  9. No daily trade cap — if conditions satisfy, trade freely.
     Bot is conservative enough by design that over-trading is unlikely.

  10. LATE_ENTRY_SURCHARGE removed (was adding +1¢ in last 3s).
      Already using a tighter window; surcharge was redundant.

All other infrastructure (redeem cascade, Chainlink WSS, compounding, heartbeat,
cred refresh, redeem_loop) retained from v9 without changes.
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
from py_clob_client.order_builder.constants import BUY, SELL

# ─── Logging ──────────────────────────────────────────────────────────────────
import logging.handlers as _lh

def _make_logger():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh  = logging.StreamHandler()
    sh.setFormatter(fmt)
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
WSS_RTDS        = "wss://ws-live-data.polymarket.com"
CHAIN_ID        = 137
CTF_CONTRACT    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E          = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID = "0x" + ("00" * 32)
RELAYER_URL     = os.environ.get("POLYMARKET_RELAYER_URL") or os.environ.get("RELAYER_URL") or "https://relayer-v2.polymarket.com/"

# ── Strategy ──────────────────────────────────────────────────────────────────
STAKE             = 1.00    # Base stake per trade

# v10: tightened entry window — last 15s only
ENTRY_WINDOW_SEC  = 15      # Enter only in last 15 seconds (was 40)
PRESIGN_BEFORE    = 20      # Build signed order at T-20s, ready for T-15s window (was 50)
MIN_FIRE_BUFFER   = 4       # Never fire if < 4s remain

# v10: threshold lowered to match realistic late-entry pricing
# At T-12s with a large BTC gap, ask is realistically 0.93-0.97, NOT 0.99.
# Requiring 0.99 here would produce zero trades.
BASE_THRESHOLD    = 0.93    # Minimum ask to enter (was 0.98)
ADAPTIVE_THRESH   = 0.94    # After 2 consecutive losses (was 0.99)

# v10: STOP-LOSS COMPLETELY DISABLED
# All positions held to resolution. Stop-loss was the primary loss source.
STOP_LOSS_BID          = 0.0   # 0.0 = disabled (was 0.85)
STOP_LOSS_MIN_SEC      = 0     # irrelevant since stop-loss disabled
BRUTAL_SELL_THRESHOLD  = 0.0   # 0.0 = disabled (was 0.93)

# v10: tighter stability — dead-calm prices only at late entry
STABILITY_N       = 5       # Price ticks needed for stability check
MAX_STD_DEV       = 0.008   # Max std-dev (was 0.015) — tighter at late entry

MIN_LIQUIDITY     = 2.0     # Min USDC ask depth (slightly relaxed — late market is thin)
MAX_SPREAD        = 0.06    # Skip if bid-ask spread > 6¢ (slightly wider at late entry)

# ── BTC distance + volatility filter ──────────────────────────────────────────
MIN_BTC_DISTANCE    = 120.0  # live BTC must be >= $120 away from opening price (was $60)
BTC_VOLATILITY_SEC  = 60     # look-back window in seconds
BTC_MAX_VOLATILITY  = 80.0   # skip if BTC range > $80 in last 60s (was $150)

# ── Oscillation filter ─────────────────────────────────────────────────────────
OSCILLATION_N           = 6
OSCILLATION_MAX_REVERSE = 0.05   # tighter: 5¢ reverse tick = bad (was 8¢)
OSCILLATION_BAD_SWINGS  = 1      # skip on even 1 large reverse swing (was 2)

# v10: late-entry surcharge REMOVED — entry window already tight enough
LATE_ENTRY_MAX_T     = 0     # disabled
LATE_ENTRY_SURCHARGE = 0.00  # disabled

# v10: momentum — require ALL of last N ticks to be non-declining
MOMENTUM_N            = 5    # check last 5 ticks (was 8)
MIN_POSITIVE_TICKS    = 5    # ALL must be non-declining (was 3 of 8)
MAX_DROP_FROM_PEAK    = 0.01 # skip if dropped > 1¢ from recent high (was 2.5¢)

# ── IST trading hours block ────────────────────────────────────────────────────
IST_BLOCK_START_UTC = (19, 30)
IST_BLOCK_END_UTC   = ( 2, 30)

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
    exit_price: float
    shares_held: float
    stake: float
    outcome: str            # win / loss / unmatched / skip
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
    token_id: str
    side: str
    entry_price: float
    shares: float
    stake: float
    cycle_id: str
    market: "Market"

@dataclass
class BotState:
    market: Optional[Market]      = None
    next_market: Optional[Market] = None
    price_history: deque          = field(default_factory=lambda: deque(maxlen=200))
    trade_history: deque          = field(default_factory=lambda: deque(maxlen=500))
    position: Optional[Position]  = None
    trade_fired: bool             = False
    resolved: Optional[str]       = None
    heartbeat_id: str             = ""
    presigned_order: object       = None
    presigned_for: Optional[str]  = None
    consecutive_losses: int       = 0
    consecutive_wins: int         = 0
    exchange_disabled: bool       = False
    last_balance: float           = 0.0
    clock_offset: float           = 0.0
    last_sync: float              = 0.0
    last_market_scan: float       = 0.0
    last_status_ts: float         = 0.0
    redeemed_condition_ids: set   = field(default_factory=set)
    current_stake: float          = STAKE
    compound_base: float          = STAKE
    last_net_profit: float        = 0.0
    btc_opening_price: float      = 0.0
    btc_live_price: float         = 0.0
    btc_price_history: list       = field(default_factory=list)
    pending_redeem: bool          = False

# ─── Persistence ──────────────────────────────────────────────────────────────
def load_trades() -> list:
    if not TRADES_FILE.exists():
        return []
    try:
        text = TRADES_FILE.read_text().strip()
        if not text:
            return []
        if text.startswith("{"):
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return json.loads(text)
    except Exception:
        return []

def save_trade(r: TradeRecord):
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
    for attempt in range(1, retries + 1):
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
            resp = client.get_balance_allowance(params=params)
            raw = float(resp.get("balance", 0))
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
    state.heartbeat_id = ""
    while not stop.is_set():
        try:
            resp = client.post_heartbeat(state.heartbeat_id)
            new_id = resp.get("heartbeat_id", "")
            if new_id:
                state.heartbeat_id = new_id
        except Exception as e:
            if "Invalid Heartbeat ID" in str(e):
                state.heartbeat_id = ""
            else:
                log.warning(f"Heartbeat error: {e}")
        await asyncio.sleep(5)

async def cred_refresh_loop(client: ClobClient, stop: asyncio.Event):
    REFRESH_INTERVAL = 6 * 3600
    await asyncio.sleep(REFRESH_INTERVAL)
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
    log.info(f"[MARKET SCAN] server_ts={server_ts} | searching for next BTC 5m market...")
    current_window_start = (server_ts // 300) * 300
    for start_ts in [current_window_start, current_window_start + 300, current_window_start + 600]:
        end_ts = start_ts + 300
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

            m = markets[0]
            log.info(f"[GAMMA] market keys: {sorted(m.keys())}")

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
            outcomes     = [str(o).strip().strip('"').strip("'") for o in outcomes]

            log.info(f"[GAMMA] clobTokenIds={clob_ids}")
            log.info(f"[GAMMA] tokens={tokens}")
            log.info(f"[GAMMA] outcomes={outcomes}")

            try:
                end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                end_ts = int(end_dt.timestamp())
            except Exception:
                end_ts = end_ts_override

            if len(clob_ids) >= 2 and len(outcomes) >= 2:
                up_i   = next((i for i,o in enumerate(outcomes) if o.lower() == "up"),   0)
                down_i = next((i for i,o in enumerate(outcomes) if o.lower() == "down"), 1)
                up_id, down_id = clob_ids[up_i], clob_ids[down_i]
                log.info(f"[GAMMA] ✅ Strategy 1 — clobTokenIds: up={str(up_id)[:16]}... down={str(down_id)[:16]}... end={end_ts}")
                return Market(slug=slug, end_ts=end_ts, up_token_id=str(up_id),
                              down_token_id=str(down_id), condition_id=m.get("conditionId",""),
                              neg_risk=m.get("negRisk", False))

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

            log.info(f"[GAMMA] trying /markets endpoint as fallback...")
            async with session.get(f"{GAMMA_API}/markets", params={"slug": slug},
                                   headers=GAMMA_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r2:
                if r2.status == 200:
                    mdata = await r2.json()
                    if mdata:
                        m2 = mdata[0]
                        log.info(f"[GAMMA] /markets keys: {sorted(m2.keys())}")
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
        market.fee_rate = "0"
    log.info(f"Market ready: {market.slug} | tick={market.tick_size} fee_bps={market.fee_rate}")
    return market

# ─── IST trading-hours guard ──────────────────────────────────────────────────
def is_ist_blocked() -> bool:
    now_utc = datetime.utcnow()
    h, m    = now_utc.hour, now_utc.minute
    cur_min   = h * 60 + m
    start_min = IST_BLOCK_START_UTC[0] * 60 + IST_BLOCK_START_UTC[1]
    end_min   = IST_BLOCK_END_UTC[0]   * 60 + IST_BLOCK_END_UTC[1]
    return cur_min >= start_min or cur_min < end_min

# ─── Signal detection ─────────────────────────────────────────────────────────
def get_signal(market: Market, history: deque, consecutive_losses: int,
               time_left: int = 999) -> Optional[tuple]:
    """
    v10 signal logic — stricter than v9, tuned for T-15s late entry.

    Returns (side, token_id, best_ask) if ALL pass:

    1. best_ask >= threshold (0.93 base, 0.94 after 2 consecutive losses)
    2. Last STABILITY_N (5) ticks have std-dev <= MAX_STD_DEV (0.008)
       Dead-calm price required — any wobble skipped.
    3. Momentum: ALL of last MOMENTUM_N (5) ticks are non-declining AND
       price has not dropped > MAX_DROP_FROM_PEAK (1¢) from recent high.
    4. Oscillation: fewer than OSCILLATION_BAD_SWINGS (1) ticks with
       a reverse move > OSCILLATION_MAX_REVERSE (5¢).
       Even 1 large reverse = skip.
    """
    threshold = ADAPTIVE_THRESH if consecutive_losses >= 2 else BASE_THRESHOLD
    if consecutive_losses >= 2:
        log.info(f"  Adaptive threshold: {threshold*100:.0f}% (streak of {consecutive_losses} losses)")

    for token_id, side_label in [
        (market.up_token_id,   "up"),
        (market.down_token_id, "down"),
    ]:
        asks = [t.best_ask for t in history if t.token_id == token_id]

        # ── 1. Threshold + stability ──────────────────────────────────────
        ticks = asks[-STABILITY_N:]
        if len(ticks) < STABILITY_N:
            continue
        if ticks[-1] < threshold:
            continue
        std = statistics.stdev(ticks) if len(ticks) > 1 else 0.0
        if std > MAX_STD_DEV:
            log.info(f"  {side_label.upper()} unstable: ask={ticks[-1]:.4f} std={std:.4f} > {MAX_STD_DEV}")
            continue

        # ── 2. Momentum: ALL last N ticks must be non-declining ───────────
        momentum_ticks = asks[-MOMENTUM_N:]
        if len(momentum_ticks) >= MOMENTUM_N:
            # Count non-declining ticks (flat or rising)
            non_declining = sum(
                1 for i in range(1, len(momentum_ticks))
                if momentum_ticks[i] >= momentum_ticks[i - 1]
            )
            if non_declining < MIN_POSITIVE_TICKS - 1:
                log.info(
                    f"  {side_label.upper()} SKIP — weak momentum: "
                    f"{non_declining}/{len(momentum_ticks)-1} non-declining ticks "
                    f"(need {MIN_POSITIVE_TICKS - 1})"
                )
                continue

            # Check drop from recent peak
            recent_peak = max(momentum_ticks)
            drop_from_peak = recent_peak - momentum_ticks[-1]
            if drop_from_peak > MAX_DROP_FROM_PEAK:
                log.info(
                    f"  {side_label.upper()} SKIP — dropped {drop_from_peak:.4f} from peak "
                    f"{recent_peak:.4f} (max {MAX_DROP_FROM_PEAK})"
                )
                continue

        # ── 3. Oscillation check ──────────────────────────────────────────
        osc_ticks = asks[-OSCILLATION_N:]
        if len(osc_ticks) >= 3:
            bad_swings = sum(
                1 for i in range(1, len(osc_ticks))
                if osc_ticks[i - 1] - osc_ticks[i] > OSCILLATION_MAX_REVERSE
            )
            if bad_swings >= OSCILLATION_BAD_SWINGS:
                log.info(
                    f"  {side_label.upper()} SKIP — oscillation: "
                    f"{bad_swings} reverse swing(s) > {OSCILLATION_MAX_REVERSE:.2f}"
                )
                continue

        log.info(f"  ✅ Signal: {side_label.upper()} ask={ticks[-1]:.4f} std={std:.4f} momentum=OK")
        return side_label, token_id, ticks[-1]
    return None

# ─── Entry filters ────────────────────────────────────────────────────────────
def spread_ok(history: deque, token_id: str) -> bool:
    recent = [t for t in history if t.token_id == token_id]
    if not recent: return False
    t = recent[-1]
    spread = t.best_ask - t.best_bid
    # At late entry >= 0.93, the spread can be wider — allow MAX_SPREAD (6¢)
    max_spread = MAX_SPREAD
    ok = spread <= max_spread
    log.info(f"  Spread: bid={t.best_bid:.4f} ask={t.best_ask:.4f} spread={spread:.4f} max={max_spread:.2f} ({'ok' if ok else 'WIDE'})")
    return ok

def volume_surge(trade_history: deque, token_id: str) -> bool:
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
    v10: slightly relaxed liquidity check for late-entry pricing (0.93-0.97).
    At T-12s the book is thinner — market makers have left. We scale down requirement.
    """
    try:
        async with session.get(f"{CLOB_HOST}/book", params={"token_id": token_id},
                               timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status == 200:
                book = await r.json()
                window = 0.03 if price >= 0.93 else 0.02
                depth  = sum(float(a["size"]) * float(a["price"])
                             for a in book.get("asks", [])
                             if float(a["price"]) <= price + window)

                if price >= 0.93:
                    # linear: 0.93→MIN_LIQUIDITY, 1.00→0.10
                    t         = (price - 0.93) / 0.07
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
    if stake is None:
        stake = STAKE
    try:
        raw_size = stake / price
        size = math.ceil(raw_size * 100) / 100
        size = max(size, 5.0)
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

# ─── Profit calculation ───────────────────────────────────────────────────────
def calc_profit(stake, entry_price, exit_price, fee_rate_bps, outcome):
    shares = stake / entry_price
    fee_rate = fee_rate_bps / 10000
    if outcome == "win":
        payout = shares * 1.0
    elif outcome == "stop_loss":
        payout = shares * exit_price
    else:
        payout = 0.0
    gross    = payout - stake
    fee_usdc = shares * fee_rate * (entry_price * (1 - entry_price)) ** 2
    net      = gross - fee_usdc
    return round(payout,6), round(gross,6), round(fee_usdc,6), round(net,6)

# ─── WebSocket: Chainlink BTC/USD ─────────────────────────────────────────────
async def run_chainlink_wss(state: BotState, stop: asyncio.Event):
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

                    state.btc_price_history.append((now_ts, btc_price))
                    cutoff = now_ts - BTC_VOLATILITY_SEC
                    state.btc_price_history = [
                        (t, p) for t, p in state.btc_price_history if t >= cutoff
                    ]

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

# ─── WebSocket: Market prices ─────────────────────────────────────────────────
async def run_market_wss(state: BotState, client: ClobClient,
                         session: aiohttp.ClientSession, stop: asyncio.Event):
    log.info("[WSS] WebSocket task started — waiting for market...")
    current_slug = None

    while not stop.is_set():
        if not state.market:
            await asyncio.sleep(1)
            continue

        if state.market.slug == current_slug:
            await asyncio.sleep(0.1)
            continue

        current_slug = state.market.slug

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
                subscribed_slug = current_slug

                async for raw in ws:
                    if stop.is_set():
                        break

                    if state.market and state.market.slug != subscribed_slug:
                        log.info(f"[WSS] Market changed → {state.market.slug} — closing old socket")
                        break

                    if raw in ("PING", "PONG"):
                        continue
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        continue

                    messages = parsed if isinstance(parsed, list) else [parsed]

                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue

                        etype = msg.get("event_type", "")

                        if etype == "price_change":
                            for ch in msg.get("price_changes", []):
                                tid = ch.get("asset_id", "")
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
        _now = time.time()
        _tl  = (state.market.end_ts - int(_now + state.clock_offset)) if state.market else 999
        await asyncio.sleep(0.1 if MIN_FIRE_BUFFER < _tl <= ENTRY_WINDOW_SEC else 1)

        if state.exchange_disabled:
            log.warning("Exchange disabled — retrying in 60s...")
            await asyncio.sleep(60)
            state.exchange_disabled = False
            continue

        if is_ist_blocked():
            now_utc = datetime.utcnow()
            log.info(f"[IST BLOCK] No trading 01:00–08:00 IST (UTC now={now_utc.strftime('%H:%M')}) — sleeping 60s")
            await asyncio.sleep(60)
            continue

        if not state.market:
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

        now_local   = time.time()
        in_window   = state.market and MIN_FIRE_BUFFER < (state.market.end_ts - int(now_local + state.clock_offset)) <= ENTRY_WINDOW_SEC
        sync_interval = 5 if in_window else 30   # sync more often in entry window
        if now_local - state.last_sync > sync_interval:
            try:
                server_ts_raw = int(await asyncio.to_thread(client.get_server_time))
                state.clock_offset = server_ts_raw - now_local
                state.last_sync    = now_local
            except Exception:
                pass
        server_ts = int(now_local + state.clock_offset)
        time_left = state.market.end_ts - server_ts

        now_wall = time.time()
        if now_wall - state.last_status_ts >= 5.0:
            state.last_status_ts = now_wall
            ups = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
            dns = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
            tick_count = len(state.price_history)
            btc_gap = (state.btc_live_price - state.btc_opening_price) if state.btc_opening_price > 0 else 0
            if ups and dns:
                log.info(f"[STATUS] T-{time_left}s | UP={ups[-1]:.3f} DOWN={dns[-1]:.3f} | "
                         f"BTC_gap={btc_gap:+.1f} | ticks={tick_count} | bal=${state.last_balance:.2f}")
            else:
                log.info(f"[STATUS] T-{time_left}s | waiting for price ticks... (received {tick_count} so far)")

        # ── Window expired ─────────────────────────────────────────────────
        if time_left <= 0:
            if not state.trade_fired:
                log.info("Window closed — no trade this cycle.")
                _log_skip(state.market, "", 0, "no_signal", state.last_balance)

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

        # ── OPEN POSITION: v10 — NO STOP-LOSS, just monitor for resolution ─
        if state.position is not None:
            # v10: stop-loss is DISABLED. We only act on WSS market_resolved.
            # Holding to resolution is the entire strategy.
            if state.resolved is not None:
                await _resolve_position(client, session, state)
                await _advance_market(client, session, state)
            # If not resolved yet, just log status and keep waiting
            continue

        # ── PRE-SIGN at T-(PRESIGN_BEFORE)s to T-(ENTRY_WINDOW_SEC+1)s ───
        # v10: presign window is T-20s to T-16s
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

        # ── ENTRY WINDOW: T-15s to T-4s ───────────────────────────────────
        if MIN_FIRE_BUFFER < time_left <= ENTRY_WINDOW_SEC and not state.trade_fired:
            signal = get_signal(state.market, state.price_history, state.consecutive_losses, time_left)

            if not signal:
                ups  = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
                dns  = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
                up_p = ups[-1] if ups else 0
                dn_p = dns[-1] if dns else 0
                if now_wall - state.last_status_ts >= 1.0:
                    state.last_status_ts = now_wall
                    log.info(f"  T-{time_left}s | UP={up_p:.3f} DOWN={dn_p:.3f} | no signal")
                continue

            side, token_id, price = signal
            log.info(f"SIGNAL @ T-{time_left}s: BTC {side.upper()} ask={price:.4f} | stake=${state.current_stake:.4f}")

            # Gate 1: spread
            if not spread_ok(state.price_history, token_id):
                log.info(f"  T-{time_left}s: wide spread — will retry next tick...")
                continue

            # Gate 2: volume surge
            if volume_surge(state.trade_history, token_id):
                log.info(f"  T-{time_left}s: volume surge — will retry next tick...")
                continue

            # Gate 3: liquidity
            if not await liquidity_ok(session, token_id, price):
                log.info(f"  T-{time_left}s: liquidity thin — will retry next tick...")
                continue

            # Gate 4: balance + pending redeem
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

            # Gate 5: BTC distance from opening price
            # v10: MIN_BTC_DISTANCE = $120
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
                    log.info(f"  ✅ BTC distance OK: live=${state.btc_live_price:.2f} "
                             f"open=${state.btc_opening_price:.2f} gap=${btc_gap:+.2f}")
            else:
                log.info(f"  BTC distance: Chainlink not ready — SKIPPING trade for safety "
                         f"(live={state.btc_live_price:.2f} open={state.btc_opening_price:.2f})")
                # v10: if Chainlink not ready at late entry, SKIP (was: allow as failsafe)
                # At T-15s we need the BTC data — without it we're flying blind.
                continue

            # Gate 6: BTC volatility
            # v10: BTC_MAX_VOLATILITY = $80
            if len(state.btc_price_history) >= 3:
                recent_prices = [p for _, p in state.btc_price_history]
                btc_range = max(recent_prices) - min(recent_prices)
                if btc_range > BTC_MAX_VOLATILITY:
                    log.info(f"  T-{time_left}s: BTC too volatile — "
                             f"range=${btc_range:.2f} over last {BTC_VOLATILITY_SEC}s "
                             f"(max=${BTC_MAX_VOLATILITY:.0f}) — retrying...")
                    continue
                else:
                    log.info(f"  ✅ BTC volatility OK: range=${btc_range:.2f} over last {BTC_VOLATILITY_SEC}s")
            else:
                log.info(f"  BTC volatility: insufficient history — SKIPPING for safety")
                # v10: skip if not enough BTC history — same reasoning as Chainlink gate
                continue

            # All gates passed — fire
            state.trade_fired = True
            await _do_buy(client, session, state, side, token_id, price, bal, time_left)

        elif time_left > ENTRY_WINDOW_SEC:
            if now_wall - state.last_status_ts >= 10.0:
                ups = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
                dns = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
                log.info(f"  T-{time_left}s | UP={ups[-1]:.3f} DOWN={dns[-1]:.3f}" if ups and dns else f"  T-{time_left}s | waiting for price ticks...")

# ─── Trade actions ────────────────────────────────────────────────────────────
async def _do_buy(client, session, state: BotState, side, token_id, price, bal_before, time_left=15):
    """
    v10: retry buy until filled or time runs out.
    No stop-loss means we MUST get in correctly or not at all.
    """
    stake = state.current_stake
    attempt = 0
    deadline = time.time() + max(time_left - MIN_FIRE_BUFFER, 2)
    last_order_status = ""

    while time.time() < deadline:
        attempt += 1
        now_left = state.market.end_ts - int(time.time() + state.clock_offset)

        if now_left <= MIN_FIRE_BUFFER:
            log.info(f"  Buy abort: only {now_left}s left, too close to close.")
            break

        if attempt == 1 and state.presigned_order and state.presigned_for == token_id:
            asks = [t.best_ask for t in state.price_history if t.token_id == token_id]
            current_ask = asks[-1] if asks else 0
            if current_ask >= BASE_THRESHOLD:
                order = state.presigned_order
                log.info(f"  Using presigned order (attempt 1, current_ask={current_ask:.4f})")
            else:
                log.info(f"  Presigned order stale (ask dropped to {current_ask:.4f}) — re-signing fresh")
                order = presign_order(client, state.market, token_id, price, stake=stake)
            state.presigned_order = None
            state.presigned_for   = None
        else:
            order = presign_order(client, state.market, token_id, price, stake=stake)

        if not order:
            log.warning(f"  Order build failed (attempt {attempt}) — retrying...")
            await asyncio.sleep(0.5)
            continue

        try:
            resp = client.post_order(order, OrderType.GTC)
            log.info(f"  Order resp (attempt {attempt}): {resp}")

            size_matched  = float(resp.get("size_matched",  0) or 0) if resp else 0
            taking_amount = float(resp.get("takingAmount",  0) or 0) if resp else 0
            making_amount = float(resp.get("makingAmount",  0) or 0) if resp else 0
            status        = (resp.get("status", "") or "").lower()   if resp else ""
            last_order_status = status

            if size_matched > 0 or taking_amount > 0 or making_amount > 0 or status == "matched":
                log.info(f"  ✅ Immediate fill on attempt {attempt}: status={status}")
                break

            if status in ("live",):
                log.info(f"  Order resting as GTC (attempt {attempt}) — balance poll will confirm")
                break

            kind = classify_error(Exception(str(resp)))
            if kind == "rate_limit":
                await asyncio.sleep(1)
            elif kind == "no_fill":
                log.info(f"  FOK no fill — retrying with GTC next attempt...")
                await asyncio.sleep(0.3)
            else:
                await asyncio.sleep(0.5)
        except Exception as e:
            kind = classify_error(e)
            if kind == "rate_limit":
                log.warning(f"  Rate limit (attempt {attempt}) — backing off 1s")
                await asyncio.sleep(1)
            elif kind == "disabled":
                log.error("  Exchange disabled mid-buy — aborting")
                state.exchange_disabled = True
                break
            elif kind == "funds":
                log.error(f"  Insufficient funds mid-buy — aborting")
                break
            else:
                log.error(f"  Attempt {attempt} error: {e}")
                await asyncio.sleep(1)
        continue

    # ── Post-loop fill detection ──────────────────────────────────────────────
    MAX_FILL_WAIT = 40
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

    if filled_amount > 0.01:
        actual_price = price
        shares = round(filled_amount / actual_price, 6)
        log.info(f"✅ BUY CONFIRMED: spent=${filled_amount:.4f} shares={shares:.6f} @ {actual_price:.4f} "
                 f"after {attempt} attempt(s) ({elapsed}s poll)")
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

    if last_order_status not in ("matched", "live", "filled"):
        try:
            client.cancel_all()
            log.info("  Cancelled unfilled GTC order.")
        except Exception:
            pass
    else:
        log.info(f"  Order was accepted (status={last_order_status}) — skipping cancel.")
        shares = round(stake / price, 6)
        bal_after_attempt = get_balance(client)
        log.warning(f"  ⚠️ FORCE-SETTING position from accepted order: "
                    f"shares={shares:.6f} @ {price:.4f}")
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
#  REDEEM ENGINE v6 — Three-layer cascade (unchanged from v9)
# ══════════════════════════════════════════════════════════════════════════════
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

def _to_bytes32(hex_str: str) -> bytes:
    h = (hex_str or "").lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) > 64:
        raise ValueError("hex too long for bytes32")
    return bytes.fromhex(h.zfill(64))

def _encode_redeem_positions(condition_id: str) -> Optional[str]:
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
        log.warning(f"[REDEEM] ABI encode failed: {e}")
        return None

def _redeem_via_relayer(condition_id: str) -> bool:
    try:
        from py_builder_relayer_client import BuilderRelayerClient, RelayerTxType
    except ImportError:
        log.warning("[REDEEM L1] py_builder_relayer_client not installed.")
        return False

    creds = _builder_creds()
    if not creds:
        log.warning("[REDEEM L1] Builder credentials not set.")
        return False

    key, secret, passphrase = creds
    try:
        relayer = BuilderRelayerClient(
            key=key, secret=secret, passphrase=passphrase,
            host=RELAYER_URL,
        )
        calldata = _encode_redeem_positions(condition_id)
        if not calldata:
            return False

        result = relayer.submit_transaction(
            tx_type=RelayerTxType.PROXY,
            to=CTF_CONTRACT,
            data=calldata,
            value=0,
        )
        log.info(f"[REDEEM L1] ✅ Submitted via relayer PROXY: {result}")
        return True
    except Exception as e:
        log.warning(f"[REDEEM L1] failed: {e}")
        return False

def _redeem_via_poly_web3(condition_id: str) -> bool:
    try:
        import poly_web3
    except ImportError:
        log.warning("[REDEEM L2] poly_web3 not installed. pip install poly-web3")
        return False

    creds = _builder_creds()
    if not creds:
        log.warning("[REDEEM L2] Builder credentials not set.")
        return False

    key, secret, passphrase = creds
    try:
        svc_cls = getattr(poly_web3, "PolyWeb3Service", None)
        if not svc_cls:
            for attr in dir(poly_web3):
                obj = getattr(poly_web3, attr)
                if isinstance(obj, type) and "redeem" in " ".join(dir(obj)).lower():
                    svc_cls = obj
                    break
        if not svc_cls:
            log.warning("[REDEEM L2] Cannot find PolyWeb3Service class in poly_web3 module.")
            return False

        svc = svc_cls(api_key=key, api_secret=secret, api_passphrase=passphrase)
        for method_name in ("redeem_positions", "redeemPositions", "redeem"):
            method = getattr(svc, method_name, None)
            if method:
                log.info(f"[REDEEM L2] Using method: {method_name}")
                result = method(condition_id)
                if result and isinstance(result, (list, dict)) and result != []:
                    log.info(f"[REDEEM L2] ✅ Redeemed {condition_id[:20]}... tx={result}")
                return True

        log.warning("[REDEEM L2] No redeem method found on PolyWeb3Service")
        return False
    except Exception as e:
        log.warning(f"[REDEEM L2] failed: {e}")
        return False

def _redeem_via_proxy_forward(condition_id: str) -> bool:
    if not WEB3_AVAILABLE:
        log.warning("[REDEEM L3] web3/eth_account not installed.")
        return False
    if not condition_id:
        log.warning("[REDEEM L3] condition_id missing.")
        return False

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

        matic_balance = w3.eth.get_balance(eoa_address)
        matic_eth = w3.from_wei(matic_balance, "ether")
        log.info(f"[REDEEM L3] EOA {eoa_address[:16]}... MATIC balance: {matic_eth:.6f}")
        if matic_balance < w3.to_wei(0.0005, "ether"):
            log.warning(f"[REDEEM L3] EOA has only {matic_eth:.6f} MATIC — may not be enough for gas.")

        if not FUNDER_ADDRESS:
            log.warning("[REDEEM L3] FUNDER_ADDRESS not set in .env — cannot locate proxy contract.")
            return False
        proxy_address = to_checksum_address(FUNDER_ADDRESS)
        log.info(f"[REDEEM L3] Using proxy wallet: {proxy_address}")

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

        proxy_contract = w3.eth.contract(address=proxy_address, abi=PROXY_FORWARD_ABI)

        code = w3.eth.get_code(proxy_address)
        if code == b"" or code == "0x":
            log.warning(f"[REDEEM L3] No contract code at {proxy_address}.")
            return False

        ctf_address   = to_checksum_address(CTF_CONTRACT)
        calldata_bytes = bytes.fromhex(calldata[2:])
        nonce     = w3.eth.get_transaction_count(eoa_address)
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

        signed  = eoa_account.sign_transaction(txn)
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

async def _auto_redeem(session: aiohttp.ClientSession, condition_id: str) -> bool:
    return await _check_redeemable(session, condition_id)

async def _resolve_position(client: ClobClient, session: aiohttp.ClientSession, state: BotState):
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
    POLL_INTERVAL       = 8
    HARD_TIMEOUT        = 600
    CASCADE_RETRY_AFTER = 60

    log.info(f"[REDEEM] Background task started for {condition_id[:20]}... "
             f"— polling every {POLL_INTERVAL}s, hard timeout {HARD_TIMEOUT}s")

    state.redeemed_condition_ids.add(condition_id)

    deadline        = time.time() + HARD_TIMEOUT
    bal_snapshot    = get_balance(client)
    last_cascade_ts = 0.0
    attempt         = 0

    while time.time() < deadline:
        attempt += 1
        elapsed = int(time.time() - deadline + HARD_TIMEOUT)

        await asyncio.sleep(POLL_INTERVAL)

        bal_now = get_balance(client)
        gained  = bal_now - bal_snapshot
        log.info(f"[REDEEM] Poll {attempt} (T+{elapsed}s) | bal=${bal_now:.4f} gained=${gained:+.4f}")

        if gained > 0.01:
            log.info(f"[REDEEM] ✅ CLAIMED! ${bal_snapshot:.4f} → ${bal_now:.4f} (+${gained:.4f})")
            state.compound_base  = round(state.compound_base + net, 4)
            state.current_stake  = state.compound_base
            state.pending_redeem = False
            log.info(f"[COMPOUND] ✅ CONFIRMED — compound_base=${state.compound_base:.4f} "
                     f"(+${net:.4f} net profit after redeem)")
            state.last_balance = bal_now
            _save_resolved_trade(pos, "win", gained, gross, fee_usdc, net, bal_before, bal_now)
            return

        is_redeemable = await _auto_redeem(session, condition_id)

        if not is_redeemable:
            log.info(f"[REDEEM] Not yet redeemable — next check in {POLL_INTERVAL}s...")
            continue

        time_since_last_cascade = time.time() - last_cascade_ts
        if last_cascade_ts == 0.0 or time_since_last_cascade >= CASCADE_RETRY_AFTER:
            log.info(f"[REDEEM] Redeemable confirmed — launching 3-layer cascade...")
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
                        log.warning("[REDEEM] ⚠️ All 3 layers failed — will retry in 60s.")
                        last_cascade_ts = time.time() - CASCADE_RETRY_AFTER + 15
        else:
            log.info(f"[REDEEM] Cascade attempted {int(time_since_last_cascade)}s ago — "
                     f"waiting for USDC...")

    bal_final    = get_balance(client)
    gained_final = bal_final - bal_snapshot
    state.last_balance   = bal_final
    state.pending_redeem = False
    if gained_final <= 0.01:
        state.redeemed_condition_ids.discard(condition_id)
        state.compound_base = round(state.compound_base + net, 4)
        state.current_stake = state.compound_base
        log.warning(
            f"[REDEEM] ⚠️ 10-min timeout, no USDC received. Handing off to redeem_loop.\n"
            f"         compound_base tentatively updated to ${state.compound_base:.4f}.\n"
            f"         Condition ID: {condition_id}"
        )
    else:
        state.compound_base = round(state.compound_base + net, 4)
        state.current_stake = state.compound_base
        log.warning(
            f"[REDEEM] ⚠️ 10-min timeout but USDC arrived: gained=${gained_final:.4f}.\n"
            f"         compound_base=${state.compound_base:.4f}"
        )
    _save_resolved_trade(pos, "win", gained_final, gross, fee_usdc, net, bal_before, bal_final)


def _save_resolved_trade(pos, outcome, payout, gross, fee_usdc, net, bal_before, bal_after):
    save_trade(TradeRecord(
        cycle_id=pos.cycle_id, side=pos.side,
        entry_price=pos.entry_price, exit_price=1.0 if outcome == "win" else 0.0,
        shares_held=pos.shares, stake=pos.stake if pos.stake and pos.stake > 0 else STAKE,
        outcome=outcome,
        payout=payout, gross_profit=gross, fee_usdc=fee_usdc, net_profit=net,
        balance_before=bal_before, balance_after=bal_after,
        market_slug=pos.market.slug, timestamp=_ts(),
    ))

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
    state.resolved          = None
    state.presigned_order   = None
    state.presigned_for     = None
    state.position          = None
    state.price_history     = deque(maxlen=200)
    state.last_status_ts    = 0.0
    state.btc_opening_price = 0.0
    state.btc_price_history = []

    if state.next_market:
        m = await _gamma_slug(session, state.next_market.slug)
        MAX_ADVANCE_SECONDS = 700
        if m and server_ts < m.end_ts <= server_ts + MAX_ADVANCE_SECONDS:
            state.market      = enrich_market(client, m)
            state.next_market = None
            log.info(f"[ADVANCE] Loaded queued market: {m.slug} | ends in {m.end_ts - server_ts}s")
            return
        else:
            if m:
                log.warning(f"[ADVANCE] Queued market end_ts too far — discarding and scanning fresh...")
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

# ─── Orphan redeem loop ────────────────────────────────────────────────────────
async def redeem_loop(session: aiohttp.ClientSession, client: ClobClient,
                     state: BotState, stop: asyncio.Event):
    SCAN_INTERVAL = 30
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    if not funder:
        log.warning("[REDEEM-LOOP] POLYMARKET_FUNDER_ADDRESS not set — orphan redeem loop disabled")
        return

    log.info(f"[REDEEM-LOOP] Started — scanning for orphan redeemable positions every {SCAN_INTERVAL}s")

    MAX_RETRIES = 10
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

            for cid, info in list(pending_verification.items()):
                if cid not in current_redeemable_cids:
                    log.info(f"[REDEEM-LOOP] ✅ Confirmed cleared: {cid[:20]}...")
                    del pending_verification[cid]
                else:
                    retries = info["retries"]
                    age = int(time.time() - info["submitted_at"])
                    if retries >= MAX_RETRIES:
                        log.warning(f"[REDEEM-LOOP] ⚠️ Giving up on {cid[:20]}... after "
                                    f"{retries} retries ({age}s).")
                        del pending_verification[cid]
                    else:
                        log.debug(f"[REDEEM-LOOP] {cid[:20]}... pending ({age}s, retry {retries+1}/{MAX_RETRIES})")
                        state.redeemed_condition_ids.discard(cid)
                        del pending_verification[cid]

            redeemable = [
                p for p in positions
                if p.get("redeemable") is True
                and p.get("conditionId", "") not in state.redeemed_condition_ids
            ]

            if not redeemable:
                continue

            for p in redeemable:
                cid  = p.get("conditionId", "")
                size = p.get("size", "?")
                if not cid:
                    continue

                prior_retries = pending_verification.get(cid, {}).get("retries", 0)
                log.debug(f"[REDEEM-LOOP] processing orphan {cid[:20]}... size={size}")
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

    log.info("=" * 70)
    log.info(" Polymarket BTC 5m Bot — v10 | No Stop-Loss | Late Entry | Compounding")
    log.info(f" Stake: base=${STAKE:.2f} | compounds AFTER redeem confirms")
    log.info(f" Entry window: T-{ENTRY_WINDOW_SEC}s to T-{MIN_FIRE_BUFFER}s (was T-40s in v9)")
    log.info(f" Buy threshold: >= {BASE_THRESHOLD*100:.0f}% (realistic for late-entry market pricing)")
    log.info(f" BTC distance: >= ${MIN_BTC_DISTANCE:.0f} from opening price (was $60)")
    log.info(f" BTC volatility: <= ${BTC_MAX_VOLATILITY:.0f}/60s (was $150)")
    log.info(f" Stability: std-dev <= {MAX_STD_DEV} (was 0.015)")
    log.info(f" Stop-loss: DISABLED — all positions held to resolution")
    log.info(f" Brutal sell: DISABLED")
    log.info(f" IST block: no trades 01:00–08:00 IST (19:30–02:30 UTC)")
    log.info(f" Chainlink: required for entry (no fallback skip — blind trading prevented)")
    log.info(" REDEEM: 3-layer cascade (relayer PROXY → poly-web3 → proxy.forward)")
    log.info("=" * 70)

    async with aiohttp.ClientSession() as session:
        server_ts = int(await asyncio.to_thread(client.get_server_time))
        drift = abs(time.time() - server_ts)
        status = "OK" if drift < 3 else f"WARNING — {drift:.1f}s drift may affect timing"
        log.info(f"Clock drift: {drift:.2f}s [{status}]")

        state.last_balance = get_balance(client)
        log.info(f"USDC balance: ${state.last_balance:.4f}")
        if state.last_balance < STAKE:
            log.warning(f"Balance shows ${state.last_balance:.4f} — if funded, SDK may be misreading. Continuing...")

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

    log.info("Bot stopped cleanly.")

if __name__ == "__main__":
    import sys
    RESTART_DELAY = 10
    while True:
        try:
            asyncio.run(run_bot())
            break
        except KeyboardInterrupt:
            log.info("Keyboard interrupt — bot stopped.")
            break
        except Exception as e:
            log.error(f"[CRASH] Bot crashed: {e}", exc_info=True)
            log.error(f"[CRASH] Restarting in {RESTART_DELAY}s...")
            time.sleep(RESTART_DELAY)
