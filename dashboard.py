"""
Polymarket Bot Dashboard v7
Complete redesign — clean, readable, data-first trading terminal.

Fixes vs v6:
  - load_trades() handles NDJSON (bot v7+) and legacy JSON array
  - All trade data displays correctly in the table
  - Equity curve uses Chart.js from CDN — no more blank canvas
  - Daily P&L bar chart
  - KPI cards always populated (even with zero data)
  - STAKE default matches bot ($6.00)
  - Full trade history with all relevant columns
  - Win/Loss/Stop breakdown bar
  - Try/except around build_page so errors show in browser, not just crash
"""

import os, json, secrets, time
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from collections import defaultdict

TRADES_FILE = Path("trades.json")
DASH_USER   = os.environ.get("DASH_USER", "admin")
DASH_PASS   = os.environ.get("DASH_PASS", "changeme")
PORT        = int(os.environ.get("DASH_PORT", "8080"))
SESSION_TTL = 3600
STAKE       = float(os.environ.get("BOT_STAKE", "6.00"))

SETTLED = {"win", "loss", "stop_loss"}

sessions: dict = {}

# ── Session helpers ────────────────────────────────────────────────────────────
def _new_session():
    tok = secrets.token_hex(32)
    sessions[tok] = time.time() + SESSION_TTL
    return tok

def _valid_session(tok):
    if not tok: return False
    exp = sessions.get(tok, 0)
    if time.time() > exp:
        sessions.pop(tok, None)
        return False
    sessions[tok] = time.time() + SESSION_TTL
    return True

def _get_cookie(headers, name):
    for part in headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == name: return v.strip()
    return None

def load_trades():
    """Read trades.json — supports NDJSON (bot v7+) and legacy JSON array."""
    if not TRADES_FILE.exists():
        return []
    try:
        text = TRADES_FILE.read_text().strip()
        if not text:
            return []
        if text.startswith("{"):
            # NDJSON: one JSON object per line
            records = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
            return records
        # Legacy: single JSON array
        data = json.loads(text)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return []
    except Exception:
        return []

# ── Stats ──────────────────────────────────────────────────────────────────────
def compute_stats(trades):
    trades = [t for t in trades if isinstance(t, dict)]
    done   = [t for t in trades if t.get("outcome") in SETTLED]
    wins   = [t for t in done if t["outcome"] == "win"]
    losses = [t for t in done if t["outcome"] == "loss"]
    stops  = [t for t in done if t["outcome"] == "stop_loss"]

    total_net   = sum(t.get("net_profit",   0) or 0 for t in done)
    total_fees  = sum(t.get("fee_usdc",     0) or 0 for t in done)
    total_gross = sum(t.get("gross_profit", 0) or 0 for t in done)
    total_stake = sum(t.get("stake", STAKE) or STAKE for t in done)
    win_rate    = len(wins) / len(done) * 100 if done else 0.0

    streak, stype = 0, ""
    for t in reversed(done):
        if streak == 0: stype, streak = t["outcome"], 1
        elif t["outcome"] == stype: streak += 1
        else: break

    best  = max(done, key=lambda t: t.get("net_profit", 0) or 0, default=None)
    worst = min(done, key=lambda t: t.get("net_profit", 0) or 0, default=None)

    # Equity curve
    equity, running = [], 0.0
    for t in done:
        running += t.get("net_profit", 0) or 0
        equity.append({
            "ts":  (t.get("timestamp","") or "")[:16].replace("T"," "),
            "val": round(running, 4)
        })

    # Daily P&L
    daily = defaultdict(float)
    for t in done:
        day = (t.get("timestamp","") or "")[:10]
        if day:
            daily[day] += t.get("net_profit", 0) or 0
    daily_list = sorted(
        [{"day": k, "pnl": round(v, 4)} for k, v in daily.items()],
        key=lambda x: x["day"]
    )

    # Latest balance
    latest_bal = None
    for t in reversed(trades):
        if isinstance(t, dict) and t.get("outcome") in SETTLED:
            b = t.get("balance_after")
            if b is not None and b >= 0:
                latest_bal = float(b)
                break

    return {
        "total":       len(done),
        "wins":        len(wins),
        "losses":      len(losses),
        "stops":       len(stops),
        "skipped":     len([t for t in trades if t.get("outcome") == "skip"]),
        "open":        len([t for t in trades if t.get("outcome") == "open"]),
        "unmatched":   len([t for t in trades if t.get("outcome") == "unmatched"]),
        "win_rate":    round(win_rate, 1),
        "total_net":   round(total_net, 4),
        "total_gross": round(total_gross, 4),
        "total_fees":  round(total_fees, 6),
        "total_stake": round(total_stake, 2),
        "roi":         round(total_net / total_stake * 100, 2) if total_stake else 0.0,
        "streak":      streak,
        "streak_type": stype,
        "best":        round((best["net_profit"]  or 0) if best  else 0, 4),
        "worst":       round((worst["net_profit"] or 0) if worst else 0, 4),
        "equity":      equity,
        "daily":       daily_list,
        "latest_bal":  latest_bal,
    }

# ── HTML helpers ───────────────────────────────────────────────────────────────
def _esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _fmt_pnl(v, dec=4):
    v   = float(v or 0)
    sgn = "+" if v >= 0 else ""
    cls = "green" if v > 0 else ("red" if v < 0 else "dim")
    return f'<span class="{cls}">{sgn}{v:.{dec}f}</span>'

def _badge(outcome, skip_reason=""):
    if outcome == "win":       return '<span class="badge win">WIN</span>'
    if outcome == "loss":      return '<span class="badge loss">LOSS</span>'
    if outcome == "stop_loss": return '<span class="badge stop">STOP</span>'
    if outcome == "open":      return '<span class="badge open">OPEN</span>'
    if outcome == "unmatched": return '<span class="badge nobadge">NO FILL</span>'
    if outcome == "skip":
        r = (skip_reason or "skip").replace("_"," ").upper()
        return f'<span class="badge skip">{_esc(r)}</span>'
    return f'<span class="badge nobadge">{_esc(outcome or "—")}</span>'

# ── Page builder ───────────────────────────────────────────────────────────────
def build_page(trades):
    trades = [t for t in trades if isinstance(t, dict)]
    s      = compute_stats(trades)

    # Balance display
    bal = s["latest_bal"]
    if bal is None:
        bal_disp, bal_cls, bal_note = "—", "dim", "No settled trades yet"
    elif bal == 0:
        bal_disp, bal_cls, bal_note = "$0.00", "danger", "Account empty — top up USDC"
    elif bal < STAKE:
        bal_disp, bal_cls, bal_note = f"${bal:.2f}", "warning", f"Below ${STAKE:.2f} stake — top up to resume"
    else:
        bal_disp, bal_cls, bal_note = f"${bal:.2f}", "ok", f"Ready · next stake ${STAKE:.2f}"

    # Nav pill
    if bal is None:
        pclass, ptxt = "pill-dim", "Monitoring"
    elif bal == 0:
        pclass, ptxt = "pill-danger", "⚠ No Funds"
    elif bal < STAKE:
        pclass, ptxt = "pill-warn", "⚠ Low Balance"
    else:
        pclass, ptxt = "pill-live", "● Live"

    # Alert banner
    alert = ""
    if bal is not None and bal == 0:
        alert = '<div class="alert alert-danger">⚠ Balance is zero — top up USDC to resume trading.</div>'
    elif bal is not None and bal < STAKE:
        alert = f'<div class="alert alert-warn">⚠ Balance <strong>${bal:.2f}</strong> is below the ${STAKE:.2f} stake threshold. Top up USDC to resume.</div>'

    # Breakdown bar
    total = s["total"]
    if total > 0:
        wp = s["wins"]   / total * 100
        lp = s["losses"] / total * 100
        sp = s["stops"]  / total * 100
        breakdown = (
            f'<div class="breakdown-bar">'
            f'<div class="bb-win"  style="width:{wp:.1f}%" title="{s["wins"]} wins ({wp:.1f}%)"></div>'
            f'<div class="bb-loss" style="width:{lp:.1f}%" title="{s["losses"]} losses ({lp:.1f}%)"></div>'
            f'<div class="bb-stop" style="width:{sp:.1f}%" title="{s["stops"]} stops ({sp:.1f}%)"></div>'
            f'</div>'
            f'<div class="breakdown-legend">'
            f'<span class="bl-win">■ {s["wins"]} Wins ({wp:.1f}%)</span>'
            f'<span class="bl-loss">■ {s["losses"]} Losses ({lp:.1f}%)</span>'
            f'<span class="bl-stop">■ {s["stops"]} Stops ({sp:.1f}%)</span>'
            f'<span class="bl-dim">{s["skipped"]} Skipped · {s["unmatched"]} No Fill · {s["open"]} Open</span>'
            f'</div>'
        )
    else:
        breakdown = '<p class="no-data-note">No settled trades yet — bot is running.</p>'

    # Streak
    if s["streak"]:
        icon = "🔥" if s["streak_type"] == "win" else "🧊"
        streak_disp = f'{icon} {s["streak"]} {s["streak_type"].upper()} streak'
    else:
        streak_disp = "—"

    # Colors
    net_cls = "green" if s["total_net"] >= 0 else "red"
    roi_cls = "green" if s["roi"]       >= 0 else "red"
    wr_cls  = "green" if s["win_rate"]  >= 55 else ("red" if s["win_rate"] < 40 else "amber")

    # P&L sign
    net_disp = ("+" if s["total_net"] >= 0 else "") + f'{s["total_net"]:.4f}'
    roi_disp = ("+" if s["roi"]       >= 0 else "") + f'{s["roi"]:.2f}%'

    # Table rows
    all_trades = list(reversed(trades))
    if all_trades:
        rows = ""
        for t in all_trades[:300]:
            outcome  = t.get("outcome", "")
            skip_r   = t.get("skip_reason", "")
            net      = t.get("net_profit", 0) or 0
            net_s    = _fmt_pnl(net) if outcome in SETTLED else '<span class="dim">—</span>'
            fee      = t.get("fee_usdc", 0) or 0
            fee_s    = f'<span class="dim">${fee:.5f}</span>' if fee else '<span class="dim">—</span>'
            bal_a    = t.get("balance_after")
            bal_s    = (f'<span class="mono">${float(bal_a):.2f}</span>'
                        if bal_a is not None and outcome in SETTLED
                        else '<span class="dim">—</span>')
            ts       = (t.get("timestamp","") or "")[:19].replace("T"," ")
            side     = (t.get("side") or "—").upper()
            ep       = float(t.get("entry_price", 0) or 0)
            ep_s     = f'{ep:.4f}' if ep else "—"
            ex       = float(t.get("exit_price", 0) or 0)
            ex_s     = f'{ex:.4f}' if ex else "—"
            stk      = float(t.get("stake", 0) or 0)
            stk_s    = f'${stk:.2f}' if stk else "—"
            sh       = float(t.get("shares_held", 0) or 0)
            sh_s     = f'{sh:.4f}' if sh else "—"
            slug     = (t.get("market_slug") or "")[-18:] or "—"
            side_cls = "up" if side == "UP" else ("down" if side == "DOWN" else "")
            rows += (
                f'<tr>'
                f'<td class="mono dim small">{_esc(ts)}</td>'
                f'<td><span class="side-badge {side_cls}">{_esc(side)}</span></td>'
                f'<td class="mono">{_esc(ep_s)}</td>'
                f'<td class="mono">{_esc(ex_s)}</td>'
                f'<td class="mono dim">{_esc(sh_s)}</td>'
                f'<td class="mono">{_esc(stk_s)}</td>'
                f'<td>{_badge(outcome, skip_r)}</td>'
                f'<td class="mono">{net_s}</td>'
                f'<td>{fee_s}</td>'
                f'<td>{bal_s}</td>'
                f'<td class="mono dim small">{_esc(slug)}</td>'
                f'</tr>'
            )
        table_count = f"{min(len(all_trades), 300)} of {len(all_trades)} trades"
    else:
        rows = '<tr><td colspan="11" class="empty-row">No trades yet — bot is monitoring BTC 5-minute markets.</td></tr>'
        table_count = "0 trades"

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    return _HTML.format(
        status_cls   = pclass,
        status_txt   = ptxt,
        alert        = alert,
        bal_disp     = bal_disp,
        bal_cls      = bal_cls,
        bal_note     = bal_note,
        net_disp     = net_disp,
        net_cls      = net_cls,
        roi_disp     = roi_disp,
        roi_cls      = roi_cls,
        total_stake  = f'{s["total_stake"]:.2f}',
        win_rate     = f'{s["win_rate"]:.1f}%',
        wr_cls       = wr_cls,
        wins         = s["wins"],
        losses       = s["losses"],
        stops        = s["stops"],
        total_trades = s["total"],
        skipped      = s["skipped"],
        open_c       = s["open"],
        best         = ("+" if s["best"] >= 0 else "") + f'{s["best"]:.4f}',
        worst        = f'{s["worst"]:.4f}',
        total_fees   = f'{s["total_fees"]:.5f}',
        streak_disp  = streak_disp,
        breakdown    = breakdown,
        equity_json  = json.dumps(s["equity"]),
        daily_json   = json.dumps(s["daily"]),
        table_rows   = rows,
        table_count  = table_count,
        last_upd     = now,
        stake        = f'{STAKE:.2f}',
    )


# ── Login page ─────────────────────────────────────────────────────────────────
_LOGIN = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot · Login</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'IBM Plex Sans',sans-serif;background:#0c0e14;color:#e2e8f0;
      min-height:100vh;display:flex;align-items:center;justify-content:center;
      background-image:radial-gradient(ellipse at 50% 0%,rgba(59,130,246,.1),transparent 60%)}}
.card{{width:100%;max-width:380px;padding:40px;background:#141720;
       border:1px solid #1e2535;border-radius:12px;box-shadow:0 30px 60px rgba(0,0,0,.6)}}
.logo{{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.3em;
       color:#3b82f6;text-transform:uppercase;margin-bottom:28px}}
h1{{font-size:26px;font-weight:600;color:#f8fafc;margin-bottom:6px}}
.sub{{font-size:13px;color:#64748b;margin-bottom:28px}}
label{{display:block;font-family:'IBM Plex Mono',monospace;font-size:10px;
       letter-spacing:.2em;color:#64748b;text-transform:uppercase;margin-bottom:6px}}
input{{width:100%;background:#0c0e14;border:1px solid #1e2535;border-radius:7px;
       padding:11px 14px;color:#e2e8f0;font-family:'IBM Plex Mono',monospace;
       font-size:13px;outline:none;transition:border-color .2s;margin-bottom:18px}}
input:focus{{border-color:#3b82f6}}
button{{width:100%;background:#3b82f6;color:#fff;border:none;border-radius:7px;
        padding:13px;font-family:'IBM Plex Mono',monospace;font-size:12px;
        font-weight:600;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;
        transition:background .2s}}
button:hover{{background:#2563eb}}
.err{{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);
      border-radius:6px;padding:11px 14px;font-size:12px;color:#f87171;
      margin-bottom:16px;font-family:'IBM Plex Mono',monospace}}
</style></head>
<body><div class="card">
  <div class="logo">Polymarket Bot</div>
  <h1>Dashboard</h1>
  <p class="sub">BTC 5-minute prediction markets</p>
  {error}
  <form method="POST" action="/login">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" required autofocus>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Sign In &rarr;</button>
  </form>
</div></body></html>"""


# ── Dashboard page ─────────────────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0c0e14; --s1:#141720; --s2:#1a1f2e; --s3:#1e2535;
  --b1:#1e2535; --b2:#252d40;
  --txt:#e2e8f0; --txt2:#94a3b8; --dim:#475569;
  --blue:#3b82f6;   --blue-bg:rgba(59,130,246,.12);  --blue-bdr:rgba(59,130,246,.3);
  --green:#10b981;  --green-bg:rgba(16,185,129,.1);   --green-bdr:rgba(16,185,129,.3);
  --red:#ef4444;    --red-bg:rgba(239,68,68,.1);       --red-bdr:rgba(239,68,68,.3);
  --amber:#f59e0b;  --amber-bg:rgba(245,158,11,.1);   --amber-bdr:rgba(245,158,11,.3);
  --mono:'IBM Plex Mono',monospace;
  --sans:'IBM Plex Sans',sans-serif;
  --r:10px;
}}
html{{scroll-behavior:smooth}}
body{{font-family:var(--sans);background:var(--bg);color:var(--txt);min-height:100vh;
      background-image:
        radial-gradient(ellipse at 80% 0%, rgba(59,130,246,.07), transparent 50%),
        radial-gradient(ellipse at 10% 90%, rgba(16,185,129,.04), transparent 50%)}}

/* ── Nav ── */
.nav{{position:sticky;top:0;z-index:100;height:52px;display:flex;align-items:center;
      justify-content:space-between;padding:0 24px;
      background:rgba(12,14,20,.92);border-bottom:1px solid var(--b1);
      backdrop-filter:blur(16px)}}
.brand{{font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:.25em;
        color:var(--blue);text-transform:uppercase;display:flex;align-items:center;gap:8px}}
.brand-dot{{width:7px;height:7px;border-radius:50%;background:var(--blue)}}
.nav-r{{display:flex;align-items:center;gap:10px}}
.pill-live  {{padding:3px 12px;border-radius:20px;font-family:var(--mono);font-size:11px;
              background:var(--green-bg);color:var(--green);border:1px solid var(--green-bdr)}}
.pill-warn  {{padding:3px 12px;border-radius:20px;font-family:var(--mono);font-size:11px;
              background:var(--amber-bg);color:var(--amber);border:1px solid var(--amber-bdr)}}
.pill-danger{{padding:3px 12px;border-radius:20px;font-family:var(--mono);font-size:11px;
              background:var(--red-bg);color:var(--red);border:1px solid var(--red-bdr)}}
.pill-dim   {{padding:3px 12px;border-radius:20px;font-family:var(--mono);font-size:11px;
              background:rgba(71,85,105,.15);color:var(--dim);border:1px solid var(--b1)}}
.nav-out{{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--dim);
          text-decoration:none;padding:5px 12px;border:1px solid var(--b1);border-radius:6px;
          text-transform:uppercase;transition:all .2s}}
.nav-out:hover{{color:var(--blue);border-color:var(--blue-bdr)}}

/* ── Page ── */
.page{{max-width:1360px;margin:0 auto;padding:24px 24px 80px}}
.sec{{font-family:var(--mono);font-size:10px;letter-spacing:.3em;color:var(--dim);
      text-transform:uppercase;margin:24px 0 12px;padding-bottom:8px;
      border-bottom:1px solid var(--b1)}}

/* ── Alerts ── */
.alert{{padding:12px 16px;border-radius:8px;font-size:13px;margin-bottom:16px;line-height:1.5}}
.alert-danger{{background:var(--red-bg);border:1px solid var(--red-bdr);color:#fca5a5}}
.alert-warn  {{background:var(--amber-bg);border:1px solid var(--amber-bdr);color:#fcd34d}}

/* ── Summary row ── */
.summary-row{{display:grid;grid-template-columns:repeat(4,1fr) 2fr;gap:12px;margin-bottom:12px}}
@media(max-width:1000px){{.summary-row{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:600px) {{.summary-row{{grid-template-columns:1fr}}}}
.scard{{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:18px 20px}}
.scard-lbl{{font-family:var(--mono);font-size:10px;letter-spacing:.2em;color:var(--dim);
            text-transform:uppercase;margin-bottom:8px}}
.scard-val{{font-family:var(--mono);font-size:26px;font-weight:600;line-height:1;margin-bottom:5px}}
.scard-note{{font-size:12px;color:var(--txt2)}}
.ok      {{color:var(--green)}}
.warning {{color:var(--amber)}}
.danger  {{color:var(--red)}}
.dim     {{color:var(--dim)}}
.green   {{color:var(--green)}}
.red     {{color:var(--red)}}
.amber   {{color:var(--amber)}}
.mono    {{font-family:var(--mono)}}
.small   {{font-size:11px}}

/* ── Chart card ── */
.chart-card{{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);
             padding:18px 20px;display:flex;flex-direction:column}}
.ch-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}}
.ch-lbl{{font-family:var(--mono);font-size:10px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase}}
.ch-val{{font-family:var(--mono);font-size:13px;font-weight:600}}
.ch-wrap{{flex:1;position:relative;min-height:120px}}

/* ── KPI grid ── */
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}}
.kpi{{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);
      padding:16px 18px;transition:border-color .2s}}
.kpi:hover{{border-color:var(--b2)}}
.kpi-lbl{{font-family:var(--mono);font-size:10px;letter-spacing:.2em;color:var(--dim);
          text-transform:uppercase;margin-bottom:7px}}
.kpi-val{{font-family:var(--mono);font-size:20px;font-weight:600;line-height:1;margin-bottom:4px}}
.kpi-sub{{font-size:11px;color:var(--txt2)}}

/* ── Charts row ── */
.charts-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
@media(max-width:800px){{.charts-row{{grid-template-columns:1fr}}}}

/* ── Breakdown ── */
.breakdown-card{{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);
                 padding:18px 20px;margin-bottom:12px}}
.breakdown-bar{{height:10px;border-radius:5px;background:var(--s2);
                display:flex;overflow:hidden;margin-bottom:10px}}
.bb-win {{background:var(--green);transition:width .6s ease}}
.bb-loss{{background:var(--red);  transition:width .6s ease}}
.bb-stop{{background:var(--amber);transition:width .6s ease}}
.breakdown-legend{{display:flex;gap:20px;flex-wrap:wrap}}
.breakdown-legend span{{font-family:var(--mono);font-size:11px}}
.bl-win {{color:var(--green)}}.bl-loss{{color:var(--red)}}.bl-stop{{color:var(--amber)}}.bl-dim{{color:var(--dim)}}
.no-data-note{{font-family:var(--mono);font-size:12px;color:var(--dim)}}

/* ── Table ── */
.tcard{{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);overflow:hidden}}
.thead{{display:flex;justify-content:space-between;align-items:center;
        padding:14px 18px;border-bottom:1px solid var(--b1);background:var(--s2)}}
.thead-l{{font-family:var(--mono);font-size:10px;letter-spacing:.2em;color:var(--txt2);text-transform:uppercase}}
.thead-r{{font-family:var(--mono);font-size:11px;color:var(--dim)}}
.tscroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;white-space:nowrap}}
thead th{{padding:9px 14px;text-align:left;font-family:var(--mono);font-size:10px;
          letter-spacing:.15em;text-transform:uppercase;color:var(--dim);
          background:rgba(0,0,0,.15);border-bottom:1px solid var(--b1)}}
tbody td{{padding:10px 14px;font-size:12px;border-bottom:1px solid rgba(255,255,255,.025);
          color:var(--txt2);vertical-align:middle}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:rgba(59,130,246,.04);color:var(--txt)}}
.tfoot{{display:flex;justify-content:space-between;align-items:center;
        padding:11px 18px;border-top:1px solid var(--b1);background:var(--s2);
        font-family:var(--mono);font-size:11px;color:var(--dim)}}
.empty-row{{text-align:center;padding:48px;color:var(--dim);font-family:var(--mono);font-size:12px}}

/* ── Badges ── */
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;
        font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.08em}}
.badge.win    {{background:var(--green-bg); color:var(--green);border:1px solid var(--green-bdr)}}
.badge.loss   {{background:var(--red-bg);   color:var(--red);  border:1px solid var(--red-bdr)}}
.badge.stop   {{background:var(--amber-bg); color:var(--amber);border:1px solid var(--amber-bdr)}}
.badge.open   {{background:var(--blue-bg);  color:var(--blue); border:1px solid var(--blue-bdr)}}
.badge.skip   {{background:rgba(71,85,105,.1);color:#94a3b8;border:1px solid var(--b1);font-size:9px}}
.badge.nobadge{{background:rgba(71,85,105,.1);color:var(--dim);border:1px solid var(--b1);font-size:9px}}
.side-badge{{display:inline-block;padding:2px 6px;border-radius:3px;
             font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.08em}}
.up  {{background:var(--green-bg);color:var(--green)}}
.down{{background:var(--red-bg);  color:var(--red)}}
</style></head>
<body>

<nav class="nav">
  <div class="brand"><div class="brand-dot"></div>POLYBOT</div>
  <div class="nav-r">
    <span class="{status_cls}">{status_txt}</span>
    <a href="/logout" class="nav-out">Sign Out</a>
  </div>
</nav>

<div class="page">

  {alert}

  <!-- ── Summary row ── -->
  <div class="sec">Account Overview</div>
  <div class="summary-row">
    <div class="scard">
      <div class="scard-lbl">USDC Balance</div>
      <div class="scard-val {bal_cls}">{bal_disp}</div>
      <div class="scard-note">{bal_note}</div>
    </div>
    <div class="scard">
      <div class="scard-lbl">Net P&amp;L</div>
      <div class="scard-val {net_cls}">{net_disp}</div>
      <div class="scard-note">all settled trades</div>
    </div>
    <div class="scard">
      <div class="scard-lbl">ROI</div>
      <div class="scard-val {roi_cls}">{roi_disp}</div>
      <div class="scard-note">on ${total_stake} deployed</div>
    </div>
    <div class="scard">
      <div class="scard-lbl">Win Rate</div>
      <div class="scard-val {wr_cls}">{win_rate}</div>
      <div class="scard-note">{wins}W / {losses}L / {stops} Stop</div>
    </div>
    <div class="chart-card">
      <div class="ch-head">
        <span class="ch-lbl">Equity Curve</span>
        <span class="ch-val {net_cls}">{net_disp}</span>
      </div>
      <div class="ch-wrap"><canvas id="eqChart"></canvas></div>
    </div>
  </div>

  <!-- ── KPI grid ── -->
  <div class="sec">Performance Details</div>
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-lbl">Total Settled</div>
      <div class="kpi-val">{total_trades}</div>
      <div class="kpi-sub">{wins} wins · {losses} losses · {stops} stops</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Skipped / No Fill</div>
      <div class="kpi-val dim">{skipped}</div>
      <div class="kpi-sub">{open_c} currently open</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Best Trade</div>
      <div class="kpi-val green">{best}</div>
      <div class="kpi-sub">net USDC profit</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Worst Trade</div>
      <div class="kpi-val red">{worst}</div>
      <div class="kpi-sub">net USDC loss</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Streak</div>
      <div class="kpi-val" style="font-size:15px;line-height:1.4">{streak_disp}</div>
      <div class="kpi-sub">&nbsp;</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Total Fees</div>
      <div class="kpi-val" style="font-size:16px">${total_fees}</div>
      <div class="kpi-sub">cumulative USDC</div>
    </div>
  </div>

  <!-- ── Breakdown bar ── -->
  <div class="sec">Trade Breakdown</div>
  <div class="breakdown-card">{breakdown}</div>

  <!-- ── Two charts ── -->
  <div class="charts-row">
    <div class="chart-card">
      <div class="ch-head">
        <span class="ch-lbl">Equity Curve (All Trades)</span>
      </div>
      <div class="ch-wrap" style="min-height:150px"><canvas id="eqChart2"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="ch-head">
        <span class="ch-lbl">Daily P&amp;L</span>
      </div>
      <div class="ch-wrap" style="min-height:150px"><canvas id="dayChart"></canvas></div>
    </div>
  </div>

  <!-- ── Trade table ── -->
  <div class="sec">Trade History</div>
  <div class="tcard">
    <div class="thead">
      <span class="thead-l">All Trades &mdash; Newest First</span>
      <span class="thead-r">${stake} stake &middot; {table_count}</span>
    </div>
    <div class="tscroll">
      <table>
        <thead><tr>
          <th>Time (UTC)</th><th>Side</th><th>Entry</th><th>Exit</th>
          <th>Shares</th><th>Stake</th><th>Outcome</th>
          <th>Net P&amp;L</th><th>Fee</th><th>Balance</th><th>Market</th>
        </tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
    <div class="tfoot">
      <span>Updated: {last_upd}</span>
      <span id="ctr">Refreshing in 20s</span>
    </div>
  </div>

</div><!-- /page -->

<script>
// ── Chart.js global defaults ───────────────────────────────────────────────────
Chart.defaults.color          = '#475569';
Chart.defaults.borderColor    = '#1e2535';
Chart.defaults.font.family    = "'IBM Plex Mono', monospace";
Chart.defaults.font.size      = 10;
Chart.defaults.plugins.legend = {{display: false}};

var G = '#10b981', R = '#ef4444', B = '#3b82f6', A = '#f59e0b';

// ── Data injected from Python ─────────────────────────────────────────────────
var EQ   = {equity_json};
var DAILY= {daily_json};

// ── Equity chart factory ──────────────────────────────────────────────────────
function makeEqChart(id, data) {{
  var cv = document.getElementById(id);
  if (!cv || !data || data.length === 0) {{
    if (cv) {{
      var ctx = cv.getContext('2d');
      ctx.fillStyle = '#475569';
      ctx.font = "11px 'IBM Plex Mono'";
      ctx.textAlign = 'center';
      ctx.fillText('No settled trades yet', cv.offsetWidth/2 || 150, 60);
    }}
    return;
  }}
  var labels = data.map(function(d){{return d.ts;}});
  var vals   = data.map(function(d){{return d.val;}});
  var last   = vals[vals.length-1] || 0;
  var color  = last >= 0 ? G : R;
  var pts    = vals.length > 50 ? 0 : (vals.length > 20 ? 2 : 4);

  new Chart(cv, {{
    type: 'line',
    data: {{
      labels: labels,
      datasets: [{{
        label: 'Net P&L',
        data: vals,
        borderColor: color,
        borderWidth: 2,
        pointRadius: pts,
        pointBackgroundColor: color,
        pointBorderColor: 'transparent',
        fill: true,
        backgroundColor: (function(ctx2){{
          var g = ctx2.chart.ctx.createLinearGradient(0,0,0,ctx2.chart.height);
          g.addColorStop(0, last>=0 ? 'rgba(16,185,129,.25)' : 'rgba(239,68,68,.25)');
          g.addColorStop(1, 'rgba(0,0,0,0)');
          return g;
        }}),
        tension: 0.35,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{mode:'index', intersect:false}},
      plugins: {{
        tooltip: {{
          backgroundColor: '#1a1f2e',
          borderColor: '#1e2535',
          borderWidth: 1,
          titleColor: '#94a3b8',
          bodyColor: '#e2e8f0',
          padding: 10,
          callbacks: {{
            label: function(ctx3) {{
              var v = ctx3.parsed.y;
              return (v>=0?'+':'') + v.toFixed(4) + ' USDC';
            }}
          }}
        }}
      }},
      scales: {{
        x: {{
          grid: {{color:'#1e2535', drawBorder:false}},
          ticks: {{maxTicksLimit:6, maxRotation:0}}
        }},
        y: {{
          grid: {{color:'#1e2535', drawBorder:false}},
          ticks: {{
            callback: function(v) {{ return (v>=0?'+':'') + v.toFixed(2); }}
          }}
        }}
      }}
    }}
  }});
}}

makeEqChart('eqChart',  EQ);
makeEqChart('eqChart2', EQ);

// ── Daily P&L bar chart ────────────────────────────────────────────────────────
(function() {{
  var cv = document.getElementById('dayChart');
  if (!cv) return;
  if (!DAILY || DAILY.length === 0) {{
    var ctx = cv.getContext('2d');
    ctx.fillStyle = '#475569';
    ctx.font = "11px 'IBM Plex Mono'";
    ctx.textAlign = 'center';
    ctx.fillText('No daily data yet', cv.offsetWidth/2 || 150, 60);
    return;
  }}
  var labels = DAILY.map(function(d){{return d.day.slice(5);}});  // MM-DD
  var vals   = DAILY.map(function(d){{return d.pnl;}});
  var bgs    = vals.map(function(v){{return v>=0 ? 'rgba(16,185,129,.6)':'rgba(239,68,68,.6)';}});
  var bds    = vals.map(function(v){{return v>=0 ? G : R;}});

  new Chart(cv, {{
    type: 'bar',
    data: {{
      labels: labels,
      datasets: [{{
        label: 'Daily P&L',
        data: vals,
        backgroundColor: bgs,
        borderColor: bds,
        borderWidth: 1,
        borderRadius: 3,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{mode:'index', intersect:false}},
      plugins: {{
        tooltip: {{
          backgroundColor: '#1a1f2e',
          borderColor: '#1e2535',
          borderWidth: 1,
          callbacks: {{
            label: function(ctx4) {{
              var v = ctx4.parsed.y;
              return (v>=0?'+':'') + v.toFixed(4) + ' USDC';
            }}
          }}
        }}
      }},
      scales: {{
        x: {{grid:{{display:false}}, ticks:{{maxRotation:45}}}},
        y: {{
          grid: {{color:'#1e2535'}},
          ticks: {{callback: function(v){{return (v>=0?'+':'')+v.toFixed(2);}}}}
        }}
      }}
    }}
  }});
}})();

// ── Auto-refresh countdown ─────────────────────────────────────────────────────
var n=20, el=document.getElementById('ctr');
(function tick(){{
  if(el) el.textContent = 'Refreshing in '+n+'s';
  if(--n < 0){{ location.reload(); return; }}
  setTimeout(tick, 1000);
}})();
</script>
</body></html>"""


# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ct="text/html; charset=utf-8", hdrs=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type",           ct)
        self.send_header("Content-Length",         str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options",        "DENY")
        self.send_header("Cache-Control",          "no-store")
        if hdrs:
            for k, v in hdrs.items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _tok(self): return _get_cookie(self.headers, "session")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/logout":
            sessions.pop(self._tok(), None)
            self._send(302, "", hdrs={"Location": "/",
                                      "Set-Cookie": "session=; Max-Age=0; Path=/"})
            return
        if path in ("/", "/login"):
            if _valid_session(self._tok()):
                self._send(302, "", hdrs={"Location": "/dashboard"})
            else:
                self._send(200, _LOGIN.format(error=""))
            return
        if path == "/dashboard":
            if not _valid_session(self._tok()):
                self._send(302, "", hdrs={"Location": "/"})
                return
            try:
                self._send(200, build_page(load_trades()))
            except Exception as e:
                import traceback
                self._send(500, f"<pre style='color:#ef4444;background:#0c0e14;padding:24px;font-family:monospace'>Dashboard error:\n{traceback.format_exc()}</pre>")
            return
        self._send(404, "<h1 style='font-family:monospace;padding:24px'>404 Not Found</h1>")

    def do_POST(self):
        if urlparse(self.path).path != "/login":
            self._send(405, "Method Not Allowed")
            return
        length = int(self.headers.get("Content-Length", 0))
        p      = parse_qs(self.rfile.read(length).decode())
        user   = p.get("username", [""])[0]
        pw     = p.get("password",  [""])[0]
        ok_u   = secrets.compare_digest(user.encode(), DASH_USER.encode())
        ok_p   = secrets.compare_digest(pw.encode(),   DASH_PASS.encode())
        if ok_u and ok_p:
            tok = _new_session()
            self._send(302, "", hdrs={
                "Location":   "/dashboard",
                "Set-Cookie": f"session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}",
            })
        else:
            self._send(200, _LOGIN.format(error='<div class="err">Invalid credentials.</div>'))


if __name__ == "__main__":
    print(f"Dashboard → http://0.0.0.0:{PORT}  (user: {DASH_USER})")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()