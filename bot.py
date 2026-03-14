"""
Polymarket BTC 5-Minute Bot — v5 (final, production-ready)

Changes vs v4:
  LOGIC FIXES
  1.  Entry window: fires ANY time price >= 95% within last 30s (not just T-25s)
      Previous: only checked at exactly T-25s
      Now: polls every second from T-30s, fires the instant condition is met

  2.  Stop-loss: after buying, monitor best_bid continuously
      If best_bid drops below 0.60 AND >5s remain, sell all shares via FAK SELL
      Shares held = calculated from fill amount (STAKE / entry_price)
      Uses FAK not FOK for SELL — FAK fills what's available, FOK requires full fill

  3.  Position tracking: bot now records shares_held after buy, clears after sell/resolve

  4.  Stop-loss outcome: logged as "stop_loss" in trades.json with actual exit price

  AUDIT FIXES
  5.  FIRE_AT_SEC removed — entry now fires as soon as T <= ENTRY_WINDOW_SEC AND signal met
  6.  Presign window adjusted: T-40s to T-31s (1s before entry window opens)
  7.  Stop-loss skip when < 5s remain (too close to resolution, not worth the fee)
  8.  FAK SELL uses correct SDK call: create_market_order(side=SELL, amount=shares)
  9.  Shares computed as STAKE / entry_price (how many tokens you received)
  10. All Telegram code removed from bot — alerts removed per user request
"""

import os, json, time, logging, statistics, asyncio, signal
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import websockets
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType
from py_clob_client.order_builder.constants import BUY, SELL

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)
log = logging.getLogger("polybot")

# ─── Config ───────────────────────────────────────────────────────────────────
PRIVATE_KEY     = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER_ADDRESS  = os.environ["POLYMARKET_FUNDER_ADDRESS"]
CLOB_HOST       = "https://clob.polymarket.com"
GAMMA_API       = "https://gamma-api.polymarket.com"
WSS_MARKET      = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAIN_ID        = 137

# ── Strategy ──────────────────────────────────────────────────────────────────
STAKE             = 1.00    # Fixed $1.00 per trade
BASE_THRESHOLD    = 0.95    # Buy when dominant side ask >= 95%
ADAPTIVE_THRESH   = 0.97    # Raised after 2 consecutive losses
ENTRY_WINDOW_SEC  = 30      # Enter any time price >= threshold AND <=30s remain
PRESIGN_BEFORE    = 40      # Build signed order at T-40s (removes signing latency)
MIN_FIRE_BUFFER   = 3       # Never fire if < 3s remain (too risky)
STOP_LOSS_BID     = 0.60    # Exit position if best_bid falls below this
STOP_LOSS_MIN_SEC = 5       # Don't stop-loss if < 5s remain (just let it resolve)
STABILITY_N       = 5       # Price ticks needed for stability check
MAX_STD_DEV       = 0.015   # Max std-dev for stability
MIN_LIQUIDITY     = 3.0     # Min USDC ask depth
MAX_SPREAD        = 0.03    # Skip if bid-ask spread > 3¢

TRADES_FILE = Path("trades.json")

# ─── Data structures ──────────────────────────────────────────────────────────
@dataclass
class Market:
    slug: str
    end_ts: int
    up_token_id: str
    down_token_id: str
    condition_id: str
    neg_risk: bool = False
    tick_size: str = "0.01"
    fee_rate: str  = "0"

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
    exchange_disabled: bool       = False
    last_balance: float           = 0.0

# ─── Persistence ──────────────────────────────────────────────────────────────
def load_trades() -> list:
    if TRADES_FILE.exists():
        try:    return json.loads(TRADES_FILE.read_text())
        except: return []
    return []

def save_trade(r: TradeRecord):
    trades = load_trades()
    trades.append(asdict(r))
    TRADES_FILE.write_text(json.dumps(trades, indent=2))

# ─── CLOB client ──────────────────────────────────────────────────────────────
def build_client() -> ClobClient:
    client = ClobClient(
        CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
        signature_type=1, funder=FUNDER_ADDRESS,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    log.info("CLOB client ready.")
    return client

def refresh_creds(client: ClobClient):
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
        log.info("Credentials refreshed.")
    except Exception as e:
        log.error(f"Credential refresh failed: {e}")

# ─── Balance ──────────────────────────────────────────────────────────────────
def get_balance(client: ClobClient) -> float:
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
        resp = client.get_balance_allowance(params=params)
        raw = float(resp.get("balance", 0))
        # Polymarket returns balance in micro-USDC (6 decimals), convert to dollars
        return raw / 1_000_000 if raw > 1000 else raw
    except Exception as e:
        log.warning(f"Balance check failed: {e}")
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
    while not stop.is_set():
        try:
            resp = client.post_heartbeat(state.heartbeat_id)
            state.heartbeat_id = resp.get("heartbeat_id", "")
        except Exception as e:
            log.warning(f"Heartbeat error: {e}")
        await asyncio.sleep(5)

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
    try:
        # Fetch via events endpoint — returns full token data including clobTokenIds
        params = {"slug": slug}
        async with session.get(f"{GAMMA_API}/events", params=params,
                               headers=GAMMA_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            log.info(f"[GAMMA] GET /events?slug={slug} → HTTP {r.status}")
            if r.status != 200:
                log.warning(f"[GAMMA] {slug} → HTTP {r.status}")
                return None
            data = await r.json()
            if not data:
                log.info(f"[GAMMA] {slug} → empty response from /events")
                return None

            event = data[0]
            markets = event.get("markets", [])
            log.info(f"[GAMMA] {slug} → event found, {len(markets)} market(s)")

            if not markets:
                # Fallback: try /markets endpoint with clob token params
                log.info(f"[GAMMA] {slug} → no markets in event, trying /markets endpoint...")
                async with session.get(f"{GAMMA_API}/markets", params={"slug": slug},
                                       headers=GAMMA_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r2:
                    if r2.status != 200: return None
                    mdata = await r2.json()
                    if not mdata: return None
                    markets = mdata
                    log.info(f"[GAMMA] /markets fallback returned {len(markets)} record(s)")

            # Find the UP and DOWN markets
            up_market   = next((m for m in markets if m.get("groupItemTitle","").upper() == "UP"   or "up"   in m.get("question","").lower()), None)
            down_market = next((m for m in markets if m.get("groupItemTitle","").upper() == "DOWN" or "down" in m.get("question","").lower()), None)

            log.info(f"[GAMMA] market titles: {[m.get('groupItemTitle') or m.get('question','?')[:30] for m in markets]}")

            if not up_market or not down_market:
                # Last resort: use clobTokenIds from single market with outcomes
                for m in markets:
                    tokens = m.get("tokens", [])
                    clob_ids = m.get("clobTokenIds", [])
                    outcomes = m.get("outcomes", "").split(",") if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                    log.info(f"[GAMMA] market raw — tokens={len(tokens)} clobTokenIds={clob_ids} outcomes={outcomes}")
                    if len(clob_ids) >= 2:
                        # outcomes order: index 0 = first outcome
                        outcome_list = [o.strip() for o in outcomes]
                        up_i   = next((i for i,o in enumerate(outcome_list) if o.lower() == "up"),   0)
                        down_i = next((i for i,o in enumerate(outcome_list) if o.lower() == "down"), 1)
                        try:
                            end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                            end_ts = int(end_dt.timestamp())
                        except Exception:
                            end_ts = end_ts_override
                        log.info(f"[GAMMA] ✅ using clobTokenIds: up={clob_ids[up_i][:12]}... end_ts={end_ts}")
                        return Market(
                            slug=m.get("slug", slug), end_ts=end_ts,
                            up_token_id=clob_ids[up_i], down_token_id=clob_ids[down_i],
                            condition_id=m.get("conditionId",""), neg_risk=m.get("negRisk",False),
                        )
                log.warning(f"[GAMMA] {slug} → could not extract UP/DOWN tokens from any field")
                return None

            def _extract_id(market_obj, side):
                ids = market_obj.get("clobTokenIds") or []
                if ids and isinstance(ids[0], str) and len(ids[0]) > 10:
                    return ids[0]
                # fallback to tokens array
                for t in market_obj.get("tokens", []):
                    if t.get("outcome","").lower() == side:
                        return t.get("token_id","")
                return None
            up_id   = _extract_id(up_market,   "up")
            down_id = _extract_id(down_market, "down")

            if not up_id or not down_id:
                log.warning(f"[GAMMA] {slug} → token IDs missing after all attempts")
                return None

            try:
                end_dt = datetime.fromisoformat(up_market["endDate"].replace("Z", "+00:00"))
                end_ts = int(end_dt.timestamp())
            except Exception:
                end_ts = end_ts_override

            log.info(f"[GAMMA] ✅ {slug} → end_ts={end_ts} up={up_id[:12]}... down={down_id[:12]}...")
            return Market(
                slug=slug, end_ts=end_ts,
                up_token_id=up_id, down_token_id=down_id,
                condition_id=up_market.get("conditionId",""), neg_risk=up_market.get("negRisk",False),
            )
    except Exception as e:
        log.warning(f"[GAMMA] fetch error [{slug}]: {e}", exc_info=True)
    return None

def enrich_market(client: ClobClient, market: Market) -> Market:
    try:    market.tick_size = str(client.get_tick_size(market.up_token_id))
    except Exception as e: log.warning(f"tick_size: {e}")
    try:
        fr = client.get_fee_rate(market.up_token_id)
        market.fee_rate = str(fr) if fr is not None else "0"
    except Exception as e: log.warning(f"fee_rate: {e}")
    log.info(f"Market ready: {market.slug} | tick={market.tick_size} fee_bps={market.fee_rate}")
    return market

# ─── Signal detection ─────────────────────────────────────────────────────────
def get_signal(market: Market, history: deque, consecutive_losses: int) -> Optional[tuple]:
    """
    Returns (side, token_id, best_ask) if:
      - best_ask >= threshold (adaptive after 2 losses)
      - last STABILITY_N ticks have low std-dev
    Checks UP first (arbitrary — both are checked every call).
    """
    threshold = ADAPTIVE_THRESH if consecutive_losses >= 2 else BASE_THRESHOLD
    if consecutive_losses >= 2:
        log.info(f"  Adaptive threshold: {threshold*100:.0f}% (streak of {consecutive_losses} losses)")

    for token_id, side_label in [
        (market.up_token_id,   "up"),
        (market.down_token_id, "down"),
    ]:
        ticks = [t.best_ask for t in history if t.token_id == token_id][-STABILITY_N:]
        if len(ticks) < STABILITY_N:
            continue
        if ticks[-1] < threshold:
            continue
        std = statistics.stdev(ticks) if len(ticks) > 1 else 0.0
        if std > MAX_STD_DEV:
            log.info(f"  {side_label.upper()} signal but unstable: ask={ticks[-1]:.4f} std={std:.4f}")
            continue
        log.info(f"  Signal: {side_label.upper()} ask={ticks[-1]:.4f} std={std:.4f}")
        return side_label, token_id, ticks[-1]
    return None

# ─── Entry filters ────────────────────────────────────────────────────────────
def spread_ok(history: deque, token_id: str) -> bool:
    recent = [t for t in history if t.token_id == token_id]
    if not recent: return False
    t = recent[-1]
    spread = t.best_ask - t.best_bid
    ok = spread <= MAX_SPREAD
    log.info(f"  Spread: bid={t.best_bid:.4f} ask={t.best_ask:.4f} spread={spread:.4f} ({'ok' if ok else 'WIDE'})")
    return ok

def volume_surge(trade_history: deque, token_id: str) -> bool:
    """Returns True (skip) if recent 30s volume is 3x the prior 30s."""
    now    = time.time()
    recent = sum(t.size for t in trade_history if t.token_id == token_id and now - t.ts <= 30)
    older  = sum(t.size for t in trade_history if t.token_id == token_id and 30 < now - t.ts <= 60)
    if older == 0: return False
    ratio = recent / older
    surge = ratio > 3.0
    log.info(f"  Volume momentum: recent={recent:.1f} older={older:.1f} ratio={ratio:.1f} {'SURGE' if surge else 'ok'}")
    return surge

async def liquidity_ok(session: aiohttp.ClientSession, token_id: str, price: float) -> bool:
    try:
        async with session.get(f"{CLOB_HOST}/book", params={"token_id": token_id},
                               timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status == 200:
                book  = await r.json()
                depth = sum(float(a["size"]) * float(a["price"])
                            for a in book.get("asks",[]) if float(a["price"]) <= price + 0.01)
                ok = depth >= MIN_LIQUIDITY
                log.info(f"  Liquidity: ${depth:.2f} ({'ok' if ok else 'THIN'})")
                return ok
    except Exception as e:
        log.warning(f"Liquidity check: {e}")
    return False

# ─── Order execution ──────────────────────────────────────────────────────────
def presign_order(client: ClobClient, market: Market, token_id: str, price: float):
    """Build and EIP-712 sign the BUY order at T-40s so it's ready to POST at fire time."""
    try:
        return client.create_market_order(
            token_id = token_id,
            side     = BUY,
            amount   = STAKE,
            price    = price,
            options  = {"tick_size": market.tick_size, "neg_risk": market.neg_risk},
        )
    except Exception as e:
        log.error(f"Presign error: {e}")
        return None

async def execute_buy(client: ClobClient, market: Market, token_id: str,
                      side: str, price: float, state: BotState) -> Optional[dict]:
    """Post the pre-signed (or freshly signed) BUY FOK order."""
    order = (state.presigned_order if state.presigned_for == token_id
             else presign_order(client, market, token_id, price))
    if not order:
        return None

    log.info(f"BUYING: BTC {side.upper()} ${STAKE:.2f} FOK @ floor={price:.4f}")
    try:
        resp = await with_retry(lambda: client.post_order(order, "FOK"), "buy_order")
        log.info(f"  Buy response: {resp}")
        return resp
    except ExchangeDisabledError:
        state.exchange_disabled = True
        return None
    except InsufficientFundsError:
        log.error(f"Insufficient funds. Balance: ${state.last_balance:.4f}")
        return None
    except Exception as e:
        log.error(f"Buy order error: {e}")
        return None

async def execute_sell(client: ClobClient, market: Market, position: Position,
                       exit_price: float) -> Optional[dict]:
    """
    Sell all shares held via FAK SELL (market order).
    Per docs: for SELL, amount = number of shares (not dollar amount).
    price = worst-price floor (minimum we accept per share).
    We set it to STOP_LOSS_BID - 0.02 to allow slight slippage.
    """
    floor = max(exit_price - 0.02, 0.01)
    log.info(f"STOP-LOSS SELL: {position.shares:.6f} shares @ floor={floor:.4f}")
    try:
        order = client.create_market_order(
            token_id = position.token_id,
            side     = SELL,
            amount   = position.shares,
            price    = floor,
            options  = {"tick_size": market.tick_size, "neg_risk": market.neg_risk},
        )
        resp = client.post_order(order, "FAK")  # FAK: fill what we can, cancel rest
        log.info(f"  Sell response: {resp}")
        return resp
    except Exception as e:
        log.error(f"Sell order error: {e}")
        return None

# ─── Stop-loss monitor ────────────────────────────────────────────────────────
def should_stop_loss(position: Position, history: deque, time_left: int) -> Optional[float]:
    """
    Returns the current best_bid if stop-loss should trigger, else None.
    Conditions:
      - best_bid < STOP_LOSS_BID (60%)
      - time_left > STOP_LOSS_MIN_SEC (don't bother selling with <5s left — let it resolve)
    """
    if time_left <= STOP_LOSS_MIN_SEC:
        return None
    bids = [t.best_bid for t in history if t.token_id == position.token_id]
    if not bids:
        return None
    current_bid = bids[-1]
    if current_bid < STOP_LOSS_BID:
        log.info(f"  Stop-loss triggered: bid={current_bid:.4f} < threshold={STOP_LOSS_BID}")
        return current_bid
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

# ─── Resolution via WSS ───────────────────────────────────────────────────────
async def wait_for_wss_resolution(state: BotState, timeout: int = 180) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline and state.resolved is None:
        await asyncio.sleep(1)
    return state.resolved

# ─── WebSocket ────────────────────────────────────────────────────────────────
async def run_market_wss(state: BotState, client: ClobClient,
                         session: aiohttp.ClientSession, stop: asyncio.Event):
    log.info("[WSS] WebSocket task started — waiting for market...")
    while not stop.is_set():
        if not state.market:
            await asyncio.sleep(1)
            continue

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

                async for raw in ws:
                    if stop.is_set(): break
                    if raw in ("PING","PONG"): continue
                    try:
                        parsed = json.loads(raw)
                    except:
                        continue

                    # Polymarket WSS sends either a single dict OR a list of dicts
                    messages = parsed if isinstance(parsed, list) else [parsed]

                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue

                        etype = msg.get("event_type","")

                        if etype == "price_change":
                            for ch in msg.get("price_changes",[]):
                                state.price_history.append(PriceTick(
                                    ts=time.time(), token_id=ch.get("asset_id",""),
                                    best_bid=float(ch.get("best_bid",0) or 0),
                                    best_ask=float(ch.get("best_ask",1) or 1),
                                ))
                            if len(state.price_history) <= 3:
                                log.info(f"[WSS] First price tick! total={len(state.price_history)}")

                        elif etype == "best_bid_ask":
                            state.price_history.append(PriceTick(
                                ts=time.time(), token_id=msg.get("asset_id",""),
                                best_bid=float(msg.get("best_bid",0) or 0),
                                best_ask=float(msg.get("best_ask",1) or 1),
                            ))
                            if len(state.price_history) <= 3:
                                log.info(f"[WSS] First best_bid_ask tick! total={len(state.price_history)}")

                        elif etype == "last_trade_price":
                            state.trade_history.append(TradeTick(
                                ts=time.time(), token_id=msg.get("asset_id",""),
                                size=float(msg.get("size",0) or 0),
                            ))

                        elif etype == "tick_size_change":
                            if state.market:
                                state.market.tick_size = msg.get("new_tick_size","0.01")
                                state.presigned_order  = None
                                log.warning(f"Tick size changed → {state.market.tick_size}")

                        elif etype == "new_market":
                            slug = msg.get("slug","")
                            if "btc-updown-5m" in slug.lower():
                                assets   = msg.get("assets_ids",[])
                                outcomes = msg.get("outcomes",[])
                                if len(assets)==2 and len(outcomes)==2:
                                    up_i = next((i for i,o in enumerate(outcomes) if o.lower()=="up"),0)
                                    state.next_market = Market(
                                        slug=slug, end_ts=0,
                                        up_token_id=assets[up_i], down_token_id=assets[1-up_i],
                                        condition_id=msg.get("market",""),
                                    )
                                    log.info(f"[WSS] Next market queued: {slug}")

                        elif etype == "market_resolved":
                            state.resolved = msg.get("winning_asset_id","")
                            log.info(f"[WSS] Market resolved: winner={state.resolved[:20]}...")

                        elif etype:
                            log.info(f"[WSS] unhandled event: {etype}")

                ping_t.cancel()

        except Exception as e:
            if not stop.is_set():
                log.warning(f"WSS dropped (reconnect in 2s): {e}")
                await asyncio.sleep(2)

async def _wss_ping(ws, stop):
    while not stop.is_set():
        try: await ws.send("PING")
        except: break
        await asyncio.sleep(10)

# ─── Main trading loop ────────────────────────────────────────────────────────
async def trading_loop(client: ClobClient, session: aiohttp.ClientSession,
                       state: BotState, stop: asyncio.Event):
    while not stop.is_set():
        await asyncio.sleep(1)

        if state.exchange_disabled:
            log.warning("Exchange disabled — retrying in 60s...")
            await asyncio.sleep(60)
            state.exchange_disabled = False
            continue

        if not state.market:
            # Poll for market every 15s — don't rely solely on WSS new_market event
            now = int(time.time())
            if not hasattr(state, '_last_market_scan') or now - state._last_market_scan > 15:
                state._last_market_scan = now
                log.info("[LOOP] No market loaded — polling Gamma API...")
                m = await fetch_btc_market(session, now)
                if m:
                    state.market = enrich_market(client, m)
                    log.info(f"[LOOP] ✅ Market loaded: {state.market.slug} | ends in {state.market.end_ts - now}s")
                else:
                    log.info("[LOOP] No market available yet — will retry in 15s")
            continue

        server_ts = int(client.get_server_time())
        time_left = state.market.end_ts - server_ts

        # Log status every 10s
        if int(time_left) % 10 == 0:
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
            await _advance_market(client, session, state)
            continue

        # ── OPEN POSITION: monitor for stop-loss or resolution ─────────────
        if state.position is not None:
            exit_bid = should_stop_loss(state.position, state.price_history, time_left)
            if exit_bid is not None:
                await _do_stop_loss(client, session, state, exit_bid)
                await _advance_market(client, session, state)
                continue

            # Resolution came in via WSS
            if state.resolved is not None:
                await _resolve_position(client, state)
                await _advance_market(client, session, state)
                continue

            continue  # holding, still monitoring

        # ── PRE-SIGN at T-40s to T-31s ────────────────────────────────────
        if ENTRY_WINDOW_SEC < time_left <= PRESIGN_BEFORE and not state.presigned_order:
            signal = get_signal(state.market, state.price_history, state.consecutive_losses)
            if signal:
                side, token_id, price = signal
                state.presigned_order = presign_order(client, state.market, token_id, price)
                state.presigned_for   = token_id
                log.info(f"Pre-signed at T-{time_left}s: {side.upper()} @ {price:.4f}")

        # ── ENTRY WINDOW: T-30s to T-3s ───────────────────────────────────
        if MIN_FIRE_BUFFER < time_left <= ENTRY_WINDOW_SEC and not state.trade_fired:
            signal = get_signal(state.market, state.price_history, state.consecutive_losses)

            if not signal:
                if int(time_left) % 5 == 0:
                    # Log current prices every 5s while in window
                    ups  = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
                    dns  = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
                    up_p = ups[-1] if ups else 0
                    dn_p = dns[-1] if dns else 0
                    log.info(f"  T-{time_left}s | UP={up_p:.3f} DOWN={dn_p:.3f} | no signal")
                continue

            side, token_id, price = signal
            log.info(f"SIGNAL @ T-{time_left}s: BTC {side.upper()} ask={price:.4f}")

            # Gate 1: spread
            if not spread_ok(state.price_history, token_id):
                log.info("Skip: wide spread")
                state.trade_fired = True
                _log_skip(state.market, side, price, "wide_spread", state.last_balance)
                continue

            # Gate 2: volume surge
            if volume_surge(state.trade_history, token_id):
                log.info("Skip: volume surge")
                state.trade_fired = True
                _log_skip(state.market, side, price, "volume_surge", state.last_balance)
                continue

            # Gate 3: liquidity
            if not await liquidity_ok(session, token_id, price):
                log.info("Skip: insufficient liquidity")
                state.trade_fired = True
                _log_skip(state.market, side, price, "no_liquidity", state.last_balance)
                continue

            # Gate 4: balance
            bal = get_balance(client)
            state.last_balance = bal
            if bal < STAKE:
                log.error(f"Insufficient funds: ${bal:.4f} < ${STAKE:.2f}")
                state.trade_fired = True
                _log_skip(state.market, side, price, "insufficient_funds", bal)
                continue

            # All gates passed — fire!
            state.trade_fired = True
            await _do_buy(client, session, state, side, token_id, price, bal)

        elif time_left > ENTRY_WINDOW_SEC and int(time_left) % 10 == 0:
            # Waiting for entry window — log every 10s
            ups = [t.best_ask for t in state.price_history if t.token_id == state.market.up_token_id]
            dns = [t.best_ask for t in state.price_history if t.token_id == state.market.down_token_id]
            log.info(f"  T-{time_left}s | UP={ups[-1]:.3f} DOWN={dns[-1]:.3f}" if ups and dns else f"  T-{time_left}s | waiting for price ticks...")

# ─── Trade actions ────────────────────────────────────────────────────────────
async def _do_buy(client, session, state: BotState, side, token_id, price, bal_before):
    resp = await execute_buy(client, state.market, token_id, side, price, state)

    if not resp or resp.get("status") in ("", "unmatched"):
        log.info("Buy unmatched — no fill.")
        save_trade(TradeRecord(
            cycle_id=_cycle_id(), side=side, entry_price=price, exit_price=0,
            shares_held=0, stake=STAKE, outcome="unmatched",
            payout=0, gross_profit=0, fee_usdc=0, net_profit=0,
            balance_before=bal_before, balance_after=bal_before,
            market_slug=state.market.slug, timestamp=_ts(),
        ))
        return

    shares = STAKE / price  # shares received = dollars spent / price per share
    log.info(f"Buy filled! Holding {shares:.6f} shares of BTC {side.upper()}")

    state.position = Position(
        token_id    = token_id,
        side        = side,
        entry_price = price,
        shares      = shares,
        cycle_id    = _cycle_id(),
        market      = state.market,
    )

async def _resolve_position(client: ClobClient, state: BotState):
    """Called when WSS market_resolved arrives. Determines win/loss."""
    pos  = state.position
    won  = (state.resolved == pos.token_id)
    outcome = "win" if won else "loss"
    fee_bps = float(state.market.fee_rate or 0)
    payout, gross, fee_usdc, net = calc_profit(STAKE, pos.entry_price, 1.0, fee_bps, outcome)

    bal_after = get_balance(client)
    state.last_balance = bal_after
    state.consecutive_losses = 0 if won else state.consecutive_losses + 1

    log.info(f"{'WIN' if won else 'LOSS'}: gross={gross:+.4f} fee={fee_usdc:.5f} net={net:+.4f} | balance=${bal_after:.4f}")
    save_trade(TradeRecord(
        cycle_id=pos.cycle_id, side=pos.side, entry_price=pos.entry_price,
        exit_price=0, shares_held=pos.shares, stake=STAKE,
        outcome=outcome, payout=payout, gross_profit=gross,
        fee_usdc=fee_usdc, net_profit=net,
        balance_before=state.last_balance, balance_after=bal_after,
        market_slug=pos.market.slug, timestamp=_ts(),
    ))

    if bal_after < STAKE:
        log.warning(f"Balance ${bal_after:.4f} is below stake. Top up to resume trading.")

    state.position = None

async def _do_stop_loss(client: ClobClient, session: aiohttp.ClientSession,
                        state: BotState, exit_bid: float):
    """Execute a stop-loss SELL when price crashes below 60%."""
    pos = state.position
    resp = await execute_sell(client, state.market, pos, exit_bid)

    # Estimate exit price from bid (FAK may fill at slightly different price)
    actual_exit = exit_bid
    fee_bps = float(state.market.fee_rate or 0)
    payout, gross, fee_usdc, net = calc_profit(STAKE, pos.entry_price, actual_exit, fee_bps, "stop_loss")

    bal_after = get_balance(client)
    state.last_balance = bal_after
    state.consecutive_losses += 1

    log.info(f"STOP-LOSS: exit_bid={exit_bid:.4f} | gross={gross:+.4f} net={net:+.4f} | balance=${bal_after:.4f}")
    save_trade(TradeRecord(
        cycle_id=pos.cycle_id, side=pos.side, entry_price=pos.entry_price,
        exit_price=actual_exit, shares_held=pos.shares, stake=STAKE,
        outcome="stop_loss", payout=payout, gross_profit=gross,
        fee_usdc=fee_usdc, net_profit=net,
        balance_before=state.last_balance, balance_after=bal_after,
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
    server_ts = int(client.get_server_time())
    state.trade_fired      = False
    state.resolved         = None
    state.presigned_order  = None
    state.presigned_for    = None
    state.price_history    = deque(maxlen=200)
    # Note: state.position cleared in _resolve_position/_do_stop_loss, not here
    # (in case advance_market is called while position is still pending — unlikely but safe)

    if state.next_market:
        m = await _gamma_slug(session, state.next_market.slug)
        if m:
            state.market      = enrich_market(client, m)
            state.next_market = None
            return

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
    log.info(" Polymarket BTC 5m Bot v5  —  $1 fixed | Entry window 30s")
    log.info(f" Buy: >= {BASE_THRESHOLD*100:.0f}% (adaptive {ADAPTIVE_THRESH*100:.0f}% after 2 losses)")
    log.info(f" Stop-loss: sell if bid < {STOP_LOSS_BID*100:.0f}% AND > {STOP_LOSS_MIN_SEC}s remain")
    log.info(f" Filters: spread<{MAX_SPREAD} | liq>${MIN_LIQUIDITY} | vol-momentum | presign@T-{PRESIGN_BEFORE}s")
    log.info("=" * 65)

    async with aiohttp.ClientSession() as session:
        # Clock sync check
        server_ts = int(client.get_server_time())
        drift = abs(time.time() - server_ts)
        status = "OK" if drift < 3 else f"WARNING — {drift:.1f}s drift may affect timing"
        log.info(f"Clock drift: {drift:.2f}s [{status}]")

        # Balance check — warn but continue (SDK may misread; real check happens pre-trade)
        state.last_balance = get_balance(client)
        log.info(f"USDC balance: ${state.last_balance:.4f}")
        if state.last_balance < STAKE:
            log.warning(f"Balance shows ${state.last_balance:.4f} — if funded, SDK may be misreading. Continuing...")

        # Bootstrap first market
        m = await fetch_btc_market(session, server_ts)
        if m:
            state.market = enrich_market(client, m)
            log.info(f"Starting market: {state.market.slug} | ends in {state.market.end_ts - server_ts}s")
        else:
            log.info("No market yet — will auto-detect via WSS new_market event.")

        await asyncio.gather(
            run_market_wss(state, client, session, stop),
            trading_loop(client, session, state, stop),
            heartbeat_loop(client, state, stop),
        )

    log.info("Bot stopped cleanly.")

if __name__ == "__main__":
    asyncio.run(run_bot())