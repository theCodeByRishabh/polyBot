"""
Polymarket Bot Dashboard v6
Fixes:
  1. FALSE UPDATES FIXED — balance card only reads from settled outcomes
     (win / loss / stop_loss). 'open' and 'unmatched' records no longer
     cause the balance to flicker or show wrong numbers.
  2. Full UI redesign — terminal-trading aesthetic, equity curve chart,
     proper KPI hierarchy, countdown refresh, no meta-refresh flicker.

v6 fixes:
  3. load_trades() now handles NDJSON format (bot v7+) and legacy JSON array
  4. isinstance(t, dict) guard on all trade list processing — prevents AttributeError crash
  5. STAKE default corrected to $6.00 to match bot
"""

import os, json, secrets, time
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

TRADES_FILE = Path("trades.json")
DASH_USER   = os.environ.get("DASH_USER", "admin")
DASH_PASS   = os.environ.get("DASH_PASS", "changeme")
PORT        = int(os.environ.get("DASH_PORT", "8080"))
SESSION_TTL = 3600
STAKE       = float(os.environ.get("BOT_STAKE", "6.00"))

# Only these outcomes represent a settled position with a real balance change
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
    """Read trades.json — supports both NDJSON (new bot v7+) and legacy JSON array."""
    if not TRADES_FILE.exists():
        return []
    try:
        text = TRADES_FILE.read_text().strip()
        if not text:
            return []
        # NDJSON format: each line is a separate JSON object (bot v7+)
        if text.startswith("{"):
            records = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass  # skip malformed lines
            return records
        # Legacy format: single JSON array (bot v5/v6)
        data = json.loads(text)
        # Guard: must be a list of dicts
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return []
    except Exception:
        return []

# ── Stats ──────────────────────────────────────────────────────────────────────
def get_latest_balance(trades):
    """
    Walk trades in reverse. ONLY consider settled outcomes so the balance
    card never lies after an 'open' or 'unmatched' write.
    """
    trades = [t for t in trades if isinstance(t, dict)]
    for t in reversed(trades):
        if t.get("outcome") not in SETTLED:
            continue
        b = t.get("balance_after")
        if b is not None and b >= 0:
            return float(b)
    return None   # no settled trade yet

def compute_stats(trades):
    # Guard: skip any non-dict entries (e.g. from partially-written NDJSON lines)
    trades = [t for t in trades if isinstance(t, dict)]
    done  = [t for t in trades if t.get("outcome") in SETTLED]
    wins  = [t for t in done if t["outcome"] == "win"]
    loses = [t for t in done if t["outcome"] == "loss"]
    stops = [t for t in done if t["outcome"] == "stop_loss"]

    total_net   = sum(t.get("net_profit",   0) for t in done)
    total_fees  = sum(t.get("fee_usdc",     0) for t in done)
    total_gross = sum(t.get("gross_profit", 0) for t in done)
    total_stake = sum(t.get("stake", STAKE)    for t in done)
    win_rate    = len(wins) / len(done) * 100 if done else 0.0

    streak, stype = 0, ""
    for t in reversed(done):
        if streak == 0: stype, streak = t["outcome"], 1
        elif t["outcome"] == stype: streak += 1
        else: break

    best  = max(done, key=lambda t: t.get("net_profit", 0), default=None)
    worst = min(done, key=lambda t: t.get("net_profit", 0), default=None)

    equity, running = [], 0.0
    for t in done:
        running += t.get("net_profit", 0)
        equity.append({"ts": t.get("timestamp","")[:16].replace("T"," "),
                        "val": round(running, 4)})

    return {
        "total": len(done), "wins": len(wins), "losses": len(loses), "stops": len(stops),
        "win_rate":    round(win_rate, 1),
        "total_net":   round(total_net, 4),
        "total_gross": round(total_gross, 4),
        "total_fees":  round(total_fees, 6),
        "total_stake": round(total_stake, 2),
        "roi": round(total_net / total_stake * 100, 2) if total_stake else 0.0,
        "streak": streak, "streak_type": stype,
        "best":  round(best["net_profit"]  if best  else 0, 4),
        "worst": round(worst["net_profit"] if worst else 0, 4),
        "skipped": len([t for t in trades if t.get("outcome") == "skip"]),
        "open":    len([t for t in trades if t.get("outcome") == "open"]),
        "equity":  equity,
    }

# ── Pages ──────────────────────────────────────────────────────────────────────
_LOGIN = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@500;600;700&family=JetBrains+Mono:wght@400;600&family=Sora:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0f;--bg2:#12121a;--card:rgba(20,20,30,0.6);--bdr:rgba(0,195,255,0.2);
  --acc:#00e5ff;--acc2:#f000ff;
  --grn:#00ffaa;--red:#ff2a5f;
  --txt:#e2e8f0;--dim:#8ba3b8;
  --mono:'JetBrains Mono',monospace;
  --sans:'Sora',sans-serif;
  --display:'Sora',sans-serif;
}
body{
  font-family:var(--sans);background:var(--bg);color:var(--txt);
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background-image:
    radial-gradient(100% 100% at 50% -20%,rgba(0,229,255,.05) 0%,transparent 60%),
    radial-gradient(80% 80% at 85% 100%,rgba(240,0,255,.05) 0%,transparent 60%),
    linear-gradient(180deg,var(--bg2),var(--bg));
  backdrop-filter: blur(10px);
}
body::before{content:'';position:fixed;inset:0;pointer-events:none;
  background:radial-gradient(120% 120% at 50% 0%,rgba(0,229,255,.05),transparent 55%);
  mix-blend-mode:screen;opacity:.7}
.shell{
  width:100%;max-width:440px;padding:32px 32px 36px;
  background:var(--card);border:1px solid var(--bdr);border-radius:16px;
  backdrop-filter: blur(20px);
  box-shadow:0 0 40px rgba(0,229,255,.1), inset 0 0 20px rgba(0,229,255,.05);
  position:relative;z-index:1;
  animation:floatIn .8s cubic-bezier(0.16, 1, 0.3, 1) both;
}
@keyframes floatIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
.eye{
  font-family:var(--mono);font-size:11px;letter-spacing:.3em;
  color:var(--acc);text-transform:uppercase;
  display:flex;align-items:center;gap:12px;margin-bottom:24px;
  text-shadow: 0 0 10px rgba(0,229,255,0.5);
}
.eye::before,.eye::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--acc),transparent)}
h1{
  font-family:var(--display);font-size:36px;font-weight:700;letter-spacing:-.02em;
  line-height:1.1;margin-bottom:10px;color:#fff;
  text-shadow: 0 0 20px rgba(255,255,255,0.1);
}
h1 em{font-style:normal;color:var(--acc);text-shadow: 0 0 15px rgba(0,229,255,0.4)}
.sub{font-family:var(--mono);font-size:12px;color:var(--dim);margin-bottom:32px;text-transform:uppercase;letter-spacing:.05em}
.fld{margin-bottom:20px}
label{display:block;font-family:var(--mono);font-size:11px;letter-spacing:.2em;
      color:span;text-transform:uppercase;margin-bottom:8px;color:var(--dim)}
input{width:100%;background:rgba(10,10,15,0.8);border:1px solid rgba(0,195,255,0.3);border-radius:8px;
      padding:14px 16px;color:var(--txt);font-family:var(--mono);font-size:14px;
      outline:none;transition:all .3s ease;box-shadow:inset 0 2px 10px rgba(0,0,0,0.5)}
input:focus{border-color:var(--acc);box-shadow:0 0 15px rgba(0,229,255,.2), inset 0 2px 10px rgba(0,0,0,0.5);transform:translateY(-1px);background:rgba(15,15,25,0.9)}
button{
  width:100%;background:linear-gradient(90deg,var(--acc),#00aaff);
  color:#000;border:none;border-radius:8px;
  padding:16px;font-family:var(--mono);font-size:14px;font-weight:700;text-transform:uppercase;cursor:pointer;
  letter-spacing:.1em;transition:all .3s ease;margin-top:16px;
  box-shadow:0 0 20px rgba(0,229,255,.3);
}
button:hover{transform:translateY(-2px);box-shadow:0 0 30px rgba(0,229,255,.5);background:linear-gradient(90deg,#00aaff,var(--acc))}
.err{background:rgba(255,42,95,.1);border:1px solid rgba(255,42,95,.4);
     border-radius:8px;padding:12px 16px;font-size:13px;color:var(--red);
     margin-bottom:20px;font-family:var(--mono);text-shadow: 0 0 10px rgba(255,42,95,0.5)}
</style></head>
<body><div class="shell">
  <div class="eye">Polybot v5</div>
  <h1>Trading<br><em>Dashboard</em></h1>
  <p class="sub">BTC 5-minute prediction markets</p>
  {error}
  <form method="POST" action="/login">
    <div class="fld"><label>Username</label>
      <input type="text" name="username" autocomplete="username" required autofocus></div>
    <div class="fld"><label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required></div>
    <button type="submit">Access Dashboard &rarr;</button>
  </form>
</div></body></html>"""


_DASH = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@500;600;700&family=JetBrains+Mono:wght@400;600&family=Sora:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07070a;--bg2:#0d0d14;--sur:rgba(18,18,25,0.7);--sur2:rgba(25,25,35,0.5);
  --bdr:rgba(0,229,255,0.15);--bdr2:rgba(0,229,255,0.05);
  --acc:#00e5ff;--acc2:#f000ff;
  --grn:#00ffaa;--red:#ff2a5f;--amb:#ffaa00;
  --txt:#f8fafc;--txt2:#cbd5e1;--dim:#64748b;
  --r:16px;
  --mono:'JetBrains Mono',monospace;
  --sans:'Sora',sans-serif;
  --display:'Sora',sans-serif;
}
body{font-family:var(--sans);background:var(--bg);color:var(--txt);min-height:100vh;
     background-image:
       radial-gradient(ellipse at top right, rgba(0,229,255,0.1), transparent 50%),
       radial-gradient(ellipse at bottom left, rgba(240,0,255,0.08), transparent 50%),
       linear-gradient(180deg,var(--bg2),var(--bg));
       background-attachment: fixed;
     }
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px) 0 0 / 40px 40px,
    linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px) 0 0 / 40px 40px;
  mix-blend-mode:screen;opacity:.5}

/* ── Nav ── */
.nav{
  position:sticky;top:0;z-index:50;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 32px;height:70px;
  background:rgba(10,10,15,.75);border-bottom:1px solid var(--bdr);
  backdrop-filter:blur(20px);
  box-shadow:0 10px 40px rgba(0,0,0,0.5);
}
.brand{font-family:var(--mono);font-size:14px;font-weight:700;
       letter-spacing:.25em;color:var(--acc);display:flex;align-items:center;gap:12px;
       text-shadow: 0 0 15px rgba(0,229,255,0.4)}
.brand-box{width:32px;height:32px;border:1px solid var(--acc);border-radius:6px;
           display:flex;align-items:center;justify-content:center;font-size:14px;background:rgba(0,229,255,0.1);
           box-shadow: inset 0 0 10px rgba(0,229,255,0.2)}
.nav-r{display:flex;align-items:center;gap:16px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:6px;
      font-family:var(--mono);font-size:10px;letter-spacing:.15em;border:1px solid var(--bdr);color:var(--txt2);
      background:rgba(20,20,30,0.6);text-transform:uppercase}
.pill.live{border-color:rgba(0,255,170,.4);color:var(--grn);background:rgba(0,255,170,.1);text-shadow: 0 0 10px rgba(0,255,170,0.5)}
.pill.warn{border-color:rgba(255,170,0,.4);color:var(--amb);background:rgba(255,170,0,.1);text-shadow: 0 0 10px rgba(255,170,0,0.5)}
.pill.bad {border-color:rgba(255,42,95,.4);color:var(--red);background:rgba(255,42,95,.1);text-shadow: 0 0 10px rgba(255,42,95,0.5)}
.blink{width:6px;height:6px;border-radius:50%;background:currentColor;animation:blink 2s infinite;box-shadow:0 0 8px currentColor}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.signout{font-family:var(--mono);font-size:10px;letter-spacing:.15em;text-transform:uppercase;
         color:var(--dim);text-decoration:none;padding:8px 16px;
         border:1px solid var(--bdr);border-radius:6px;transition:all .3s ease;background:transparent}
.signout:hover{color:var(--acc);border-color:var(--acc);box-shadow:0 0 15px rgba(0,229,255,.2);background:rgba(0,229,255,0.05)}

/* ── Page ── */
.page{max-width:1240px;margin:0 auto;padding:40px 32px 100px;position:relative;z-index:1;
      animation:pageIn .8s cubic-bezier(0.16, 1, 0.3, 1) both}
@keyframes pageIn{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.sec-label{
  font-family:var(--mono);font-size:11px;letter-spacing:.3em;
  text-transform:uppercase;color:var(--acc);
  display:flex;align-items:center;gap:16px;margin-bottom:24px;margin-top:48px;
  text-shadow: 0 0 10px rgba(0,229,255,0.3);
}
.sec-label::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--acc) 0%,transparent 100%);opacity:0.3}
.sec-label:first-child{margin-top:0}

/* ── Alert ── */
.alert{display:flex;align-items:flex-start;gap:16px;padding:16px 24px;
       border-radius:12px;margin-bottom:24px;font-size:14px;line-height:1.6;
       backdrop-filter:blur(10px)}
.alert.warn{background:rgba(255,170,0,.05);border:1px solid rgba(255,170,0,.3);box-shadow:0 0 20px rgba(255,170,0,.1)}
.alert.crit{background:rgba(255,42,95,.05);border:1px solid rgba(255,42,95,.3);box-shadow:0 0 20px rgba(255,42,95,.1)}
.alert-ico{font-size:18px;margin-top:2px;flex-shrink:0}
.alert strong{font-weight:700;letter-spacing:.05em}
.alert code{font-family:var(--mono);font-size:12px;background:rgba(0,0,0,.4);color:var(--acc);
            padding:2px 8px;border-radius:4px;border:1px solid rgba(0,229,255,0.2)}

/* ── Hero row ── */
.hero{display:grid;grid-template-columns:300px 1fr;gap:24px;align-items:stretch}
@media(max-width:800px){.hero{grid-template-columns:1fr}}

/* Balance card */
.balcard{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;
         padding:30px 24px;min-width:260px;position:relative;overflow:hidden;
         box-shadow:0 15px 35px rgba(0,0,0,0.4), inset 0 0 20px rgba(0,229,255,0.05);
         backdrop-filter:blur(10px)}
.balcard::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--acc),var(--acc2));opacity:.8;
  box-shadow: 0 0 10px var(--acc)}
.balcard::after{content:'';position:absolute;top:2px;left:0;right:0;height:40px;
  background:linear-gradient(180deg,rgba(0,229,255,0.1),transparent);pointer-events:none}
.bal-lbl{font-family:var(--mono);font-size:11px;letter-spacing:.25em;
         text-transform:uppercase;color:var(--dim);margin-bottom:16px}
.bal-num{font-family:var(--mono);font-size:48px;font-weight:600;
         letter-spacing:-.02em;line-height:1;margin-bottom:6px;
         text-shadow: 0 0 30px currentcolor}
.bal-num.ok {color:var(--grn)}
.bal-num.low{color:var(--amb)}
.bal-num.bad{color:var(--red)}
.bal-num.unk{color:var(--dim);font-size:32px}
.bal-sub{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:12px;line-height:1.6}
.bal-badge{display:inline-flex;align-items:center;gap:8px;margin-top:20px;
           padding:6px 14px;border-radius:6px;
           font-family:var(--mono);font-size:10px;letter-spacing:.15em;text-transform:uppercase;font-weight:700}
.bal-badge.ok {background:rgba(0,255,170,.1);color:var(--grn);border:1px solid rgba(0,255,170,.3);box-shadow:0 0 15px rgba(0,255,170,0.2)}
.bal-badge.low{background:rgba(255,170,0,.1);color:var(--amb);border:1px solid rgba(255,170,0,.3);box-shadow:0 0 15px rgba(255,170,0,0.2)}
.bal-badge.bad{background:rgba(255,42,95,.1);color:var(--red);border:1px solid rgba(255,42,95,.3);box-shadow:0 0 15px rgba(255,42,95,0.2)}
.bal-badge.unk{background:rgba(100,116,139,.1);color:var(--dim);border:1px solid rgba(100,116,139,0.3)}

/* Chart card */
.chartcard{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;
           padding:24px 30px;display:flex;flex-direction:column;
           box-shadow:0 15px 35px rgba(0,0,0,0.4), inset 0 0 20px rgba(0,229,255,0.05);
           backdrop-filter:blur(10px)}
.ch-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.ch-title{font-family:var(--mono);font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--dim)}
.ch-pnl{font-family:var(--mono);font-size:28px;font-weight:600;letter-spacing:-.02em;text-shadow: 0 0 20px currentcolor}
.ch-pnl.pos{color:var(--grn)}.ch-pnl.neg{color:var(--red)}
.ch-wrap{flex:1;min-height:120px;position:relative}
#eqchart{width:100%;height:100%;display:block;filter:drop-shadow(0 0 8px rgba(0,229,255,0.3))}
@keyframes rise{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.balcard,.chartcard,.kpi,.tcard{animation:rise .8s cubic-bezier(0.16, 1, 0.3, 1) both}
.balcard{animation-delay:.05s}
.chartcard{animation-delay:.15s}
.kpi:nth-child(1){animation-delay:.1s}
.kpi:nth-child(2){animation-delay:.15s}
.kpi:nth-child(3){animation-delay:.2s}
.kpi:nth-child(4){animation-delay:.25s}
.kpi:nth-child(5){animation-delay:.3s}
.kpi:nth-child(6){animation-delay:.35s}
.tcard{animation-delay:.2s}

/* ── KPI grid ── */
.kgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}
.kpi{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;
     padding:22px 24px;position:relative;overflow:hidden;
     box-shadow:0 10px 30px rgba(0,0,0,0.3);
     backdrop-filter:blur(10px);
     transition:transform .3s cubic-bezier(0.16, 1, 0.3, 1), box-shadow .3s ease, border-color .3s ease}
.kpi:hover{transform:translateY(-4px);box-shadow:0 15px 40px rgba(0,229,255,.1);border-color:rgba(0,229,255,0.4)}
.kpi-bar{position:absolute;top:0;left:0;width:4px;height:100%;
         border-radius:6px 0 0 6px; box-shadow: 0 0 15px currentcolor}
.kpi-bar.g{background:var(--grn)}.kpi-bar.r{background:var(--red)}
.kpi-bar.a{background:var(--amb)}.kpi-bar.b{background:var(--acc2)}
.kpi-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.2em;
         text-transform:uppercase;color:var(--dim);margin-bottom:12px}
.kpi-val{font-family:var(--mono);font-size:26px;font-weight:600;
         letter-spacing:-.02em;line-height:1;margin-bottom:8px;text-shadow: 0 0 15px currentcolor}
.kpi-val.pos{color:var(--grn)}.kpi-val.neg{color:var(--red)}.kpi-val.neu{color:var(--txt)}
.kpi-sub{font-family:var(--mono);font-size:11px;color:span;opacity:.8}

/* ── Table ── */
.tcard{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;overflow:hidden;
       box-shadow:0 15px 40px rgba(0,0,0,0.4);backdrop-filter:blur(10px)}
.thead{padding:18px 24px;border-bottom:1px solid var(--bdr);background:rgba(20,20,30,0.3);
       display:flex;align-items:center;justify-content:space-between}
.thead-l{font-family:var(--mono);font-size:12px;letter-spacing:.15em;text-transform:uppercase;color:var(--acc);text-shadow:0 0 10px rgba(0,229,255,0.3)}
.thead-r{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--dim)}
.tscroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;white-space:nowrap}
thead th{padding:14px 20px;text-align:left;
         font-family:var(--mono);font-size:10px;letter-spacing:.2em;text-transform:uppercase;
         color:span;opacity:0.6;background:rgba(10,10,15,0.4);border-bottom:1px solid var(--bdr2)}
tbody td{padding:16px 20px;font-family:var(--mono);font-size:12px;
         border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle;color:var(--txt2);
         transition:background .2s}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:rgba(0,229,255,.05);color:var(--txt)}
.b{display:inline-block;padding:4px 10px;border-radius:4px;
   font-size:10px;font-weight:700;letter-spacing:.15em;text-transform:uppercase}
.bw{background:rgba(0,255,170,.1);color:var(--grn);border:1px solid rgba(0,255,170,.3);box-shadow:0 0 10px rgba(0,255,170,0.1)}
.bl{background:rgba(255,42,95,.1);color:var(--red);border:1px solid rgba(255,42,95,.3);box-shadow:0 0 10px rgba(255,42,95,0.1)}
.bs{background:rgba(255,170,0,.1);color:var(--amb);border:1px solid rgba(255,170,0,.3);box-shadow:0 0 10px rgba(255,170,0,0.1)}
.bo{background:rgba(100,116,139,.1);color:span;opacity:0.8;border:1px solid rgba(100,116,139,.3)}
.bc{background:rgba(0,229,255,.1);color:var(--acc);border:1px solid rgba(0,229,255,.3);box-shadow:0 0 10px rgba(0,229,255,0.1)}
.pp{color:var(--grn);text-shadow:0 0 10px rgba(0,255,170,0.3)}.pn{color:var(--red);text-shadow:0 0 10px rgba(255,42,95,0.3)}
.tfoot{padding:14px 24px;border-top:1px solid var(--bdr);background:rgba(20,20,30,0.3);
       font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:.1em;
       display:flex;align-items:center;justify-content:space-between}
.empty{text-align:center;padding:80px 20px;
       font-family:var(--mono);font-size:14px;color:var(--dim);line-height:2;letter-spacing:.1em;text-transform:uppercase}
@media (prefers-reduced-motion: reduce){
  .page,.kpi,.balcard,.chartcard,.tcard,.shell{animation:none}
  .blink{animation:none}
}
</style></head>
<body>
<nav class="nav">
  <div class="brand"><div class="brand-box">&#x2B21;</div>POLYBOT</div>
  <div class="nav-r">
    <div class="pill {status_pill}"><span class="blink"></span>{status_txt}</div>
    <a href="/logout" class="signout">sign out</a>
  </div>
</nav>
<main class="page">

{alert_html}

<div class="sec-label">Account</div>
<div class="hero" style="margin-bottom:26px">
  <div class="balcard">
    <div class="bal-lbl">USDC Balance</div>
    <div class="bal-num {bal_cls}">{bal_num}</div>
    <div class="bal-sub">{bal_sub}</div>
    <div class="bal-badge {bal_cls}">&#9679; {bal_badge}</div>
  </div>
  <div class="chartcard">
    <div class="ch-head">
      <span class="ch-title">Equity Curve &mdash; Cumulative Net P&amp;L</span>
      <span class="ch-pnl {pnl_cls}">{pnl_disp}</span>
    </div>
    <div class="ch-wrap"><canvas id="eqchart"></canvas></div>
  </div>
</div>

<div class="sec-label">Performance</div>
<div class="kgrid" style="margin-bottom:26px">{kpi_html}</div>

<div class="sec-label">Trade History</div>
<div class="tcard">
  <div class="thead">
    <span class="thead-l">Recent trades</span>
    <span class="thead-r">BTC 5m &middot; ${stake} stake</span>
  </div>
  <div class="tscroll">{table_html}</div>
  <div class="tfoot">
    <span>Last updated: {last_upd}</span>
    <span id="ctr">auto-refresh in 20s</span>
  </div>
</div>

</main>
<script>
/* equity chart */
(function(){
  var raw={equity_json};
  var cv=document.getElementById('eqchart');
  if(!cv||raw.length<2)return;
  var dpr=window.devicePixelRatio||1;
  var rc=cv.parentElement.getBoundingClientRect();
  var W=rc.width||600,H=rc.height||120;
  cv.width=W*dpr;cv.height=H*dpr;
  cv.style.width=W+'px';cv.style.height=H+'px';
  var c=cv.getContext('2d');c.scale(dpr,dpr);
  var vals=raw.map(function(d){return d.val});
  var mn=Math.min.apply(null,vals.concat([0]));
  var mx=Math.max.apply(null,vals.concat([0]));
  var rng=mx-mn||1;
  var pt={t:12,b:12,l:8,r:8};
  var w=W-pt.l-pt.r,h=H-pt.t-pt.b;
  var xf=function(i){return pt.l+(i/(vals.length-1))*w};
  var yf=function(v){return pt.t+(1-(v-mn)/rng)*h};
  var zy=yf(0);
  var pos=vals[vals.length-1]>=0;
  var rgb=pos?'0,255,170':'255,42,95';
  var g=c.createLinearGradient(0,pt.t,0,H-pt.b);
  g.addColorStop(0,'rgba('+rgb+',.3)');g.addColorStop(1,'rgba('+rgb+',.0)');
  c.beginPath();c.moveTo(xf(0),zy);
  vals.forEach(function(v,i){c.lineTo(xf(i),yf(v))});
  c.lineTo(xf(vals.length-1),zy);c.closePath();
  c.fillStyle=g;c.fill();
  c.beginPath();c.strokeStyle='rgba(0,229,255,.2)';c.lineWidth=1;
  c.setLineDash([4,6]);c.moveTo(pt.l,zy);c.lineTo(W-pt.r,zy);c.stroke();c.setLineDash([]);
  c.beginPath();
  vals.forEach(function(v,i){i===0?c.moveTo(xf(i),yf(v)):c.lineTo(xf(i),yf(v))});
  c.strokeStyle=pos?'#00ffaa':'#ff2a5f';c.lineWidth=2.5;c.lineJoin='round';c.stroke();
  var lx=xf(vals.length-1),ly=yf(vals[vals.length-1]);
  c.beginPath();c.arc(lx,ly,5,0,Math.PI*2);
  c.fillStyle=pos?'#00ffaa':'#ff2a5f';
  c.shadowColor=pos?'#00ffaa':'#ff2a5f';c.shadowBlur=10;
  c.fill();c.shadowBlur=0;
})();

/* countdown refresh */
var n=20,el=document.getElementById('ctr');
(function tick(){
  if(el)el.textContent='auto-refresh in '+n+'s';
  if(--n<0){location.reload();return}
  setTimeout(tick,1000);
})();
</script>
</body></html>"""


# ── Build helpers ──────────────────────────────────────────────────────────────
def _kpi(label, val, sub, bar="", vc="neu"):
    b = f'<div class="kpi-bar {bar}"></div>' if bar else ""
    return (f'<div class="kpi">{b}'
            f'<div class="kpi-lbl">{label}</div>'
            f'<div class="kpi-val {vc}">{val}</div>'
            f'<div class="kpi-sub">{sub}</div>'
            f'</div>')


def build_page(trades):
    s   = compute_stats(trades)
    bal = get_latest_balance(trades)

    # ── Balance card state ──────────────────────────────────────────────────
    if bal is None:
        bal_cls, bal_num, bal_sub = "unk", "—", "No settled trades yet"
        bal_badge = "Awaiting data"
        status_pill, status_txt = "", "Live · 20s"
        alert_html = ""
    elif bal <= 0:
        bal_cls, bal_num = "bad", f"${bal:.4f}"
        bal_sub = "Cannot place trades"
        bal_badge = "Out of funds"
        status_pill, status_txt = "bad", "ALERT · No funds"
        alert_html = ('<div class="alert crit"><span class="alert-ico">&#9888;</span>'
                      '<div><strong>Balance is zero.</strong> Top up your Polymarket account with USDC to resume.</div></div>')
    elif bal < STAKE:
        bal_cls, bal_num = "low", f"${bal:.4f}"
        bal_sub = f"Below ${STAKE:.2f} stake threshold"
        bal_badge = "Low balance"
        status_pill, status_txt = "warn", f"LOW BAL"
        alert_html = (f'<div class="alert warn"><span class="alert-ico">&#9888;</span>'
                      f'<div><strong>Balance below <code>${STAKE:.2f}</code> stake.</strong> '
                      f'Current: <strong>${bal:.4f}</strong>. Top up to resume.</div></div>')
    else:
        bal_cls, bal_num = "ok", f"${bal:.4f}"
        bal_sub = f"Sufficient · next stake ${STAKE:.2f}"
        bal_badge = "Funded"
        status_pill, status_txt = "live", "Live · 20s"
        alert_html = ""

    # ── Chart ───────────────────────────────────────────────────────────────
    pnl_cls  = "pos" if s["total_net"] >= 0 else "neg"
    pnl_disp = f"${s['total_net']:+.4f}"

    # ── KPIs ────────────────────────────────────────────────────────────────
    wr_cls  = "pos" if s["win_rate"] >= 60 else ("neg" if s["win_rate"] < 40 else "neu")
    roi_cls = "pos" if s["roi"] >= 0 else "neg"
    net_cls = "pos" if s["total_net"] >= 0 else "neg"

    sk = s["streak"]
    st = s["streak_type"]
    sk_disp = (("🔥 " if st=="win" else "🧊 ") + f"{sk} {st}") if sk else "—"

    kpi_html = (
        _kpi("Net P&amp;L",   f"${s['total_net']:+.4f}", f"gross ${s['total_gross']:+.4f}", "g" if s["total_net"]>=0 else "r", net_cls) +
        _kpi("Win Rate",      f"{s['win_rate']}%",       f"{s['wins']}W / {s['losses']}L / {s['stops']}SL", "g" if s["win_rate"]>=60 else "r", wr_cls) +
        _kpi("ROI",           f"{s['roi']:+.2f}%",       f"on ${s['total_stake']:.2f} deployed", "g" if s["roi"]>=0 else "r", roi_cls) +
        _kpi("Total Trades",  str(s["total"]),           f"{s['skipped']} skipped · {s['open']} open", "b") +
        _kpi("Best Trade",    f"${s['best']:+.4f}",      "net profit", "g", "pos") +
        _kpi("Worst Trade",   f"${s['worst']:+.4f}",     "net profit", "r", "neg") +
        _kpi("Streak",        sk_disp,                   "&nbsp;", "a") +
        _kpi("Fees Paid",     f"${s['total_fees']:.5f}", "cumulative", "")
    )

    # ── Table ───────────────────────────────────────────────────────────────
    trades = [t for t in trades if isinstance(t, dict)]
    recent = list(reversed(trades[-100:]))
    if recent:
        rows = ""
        for t in recent:
            out = t.get("outcome", "")
            if   out == "win":       badge = '<span class="b bw">WIN</span>'
            elif out == "loss":      badge = '<span class="b bl">LOSS</span>'
            elif out == "stop_loss": badge = '<span class="b bs">STOP</span>'
            elif out == "open":      badge = '<span class="b bc">OPEN</span>'
            elif out == "skip":
                r = (t.get("skip_reason") or "skip").replace("_"," ")
                badge = f'<span class="b bo">{r}</span>'
            else:
                badge = f'<span class="b bo">{out or "—"}</span>'

            net  = t.get("net_profit", 0)
            nc   = "pp" if net > 0 else ("pn" if net < 0 else "")
            ns   = f"${net:+.4f}" if net else "—"
            fee  = t.get("fee_usdc", 0)
            fs   = f"${fee:.5f}" if fee else "—"
            baft = t.get("balance_after")
            # Show balance_after only for settled trades — avoids false "balance changed" noise
            bs   = (f"${baft:.4f}" if baft is not None and out in SETTLED else "—")
            ts   = t.get("timestamp", "")[:19].replace("T", " ")
            side = (t.get("side") or "—").upper()
            ep   = f"{t.get('entry_price',0):.4f}" if t.get("entry_price") else "—"
            ex   = t.get("exit_price", 0)
            exs  = f"{ex:.4f}" if ex else "—"
            stk  = f"${t.get('stake',0):.2f}" if t.get("stake",0)>0 else "—"
            slug = (t.get("market_slug") or "")[-12:]

            rows += (f"<tr><td>{ts}</td><td>{side}</td>"
                     f"<td>{ep}</td><td>{exs}</td><td>{stk}</td>"
                     f"<td>{badge}</td>"
                     f"<td class='{nc}'>{ns}</td><td>{fs}</td>"
                     f"<td>{bs}</td><td style='color:var(--dim)'>{slug}</td></tr>")

        table_html = ("<table><thead><tr>"
                      "<th>Time UTC</th><th>Side</th>"
                      "<th>Entry</th><th>Exit</th><th>Stake</th>"
                      "<th>Outcome</th><th>Net P&amp;L</th><th>Fee</th>"
                      f"<th>Balance</th><th>Market</th>"
                      f"</tr></thead><tbody>{rows}</tbody></table>")
    else:
        table_html = '<div class="empty">&#128225;<br>No trades yet &mdash; bot is monitoring BTC 5m markets...</div>'

    last_upd = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    page = _DASH
    for k, v in {
        "{status_pill}":  status_pill,
        "{status_txt}":   status_txt,
        "{alert_html}":   alert_html,
        "{bal_cls}":      bal_cls,
        "{bal_num}":      bal_num,
        "{bal_sub}":      bal_sub,
        "{bal_badge}":    bal_badge,
        "{pnl_cls}":      pnl_cls,
        "{pnl_disp}":     pnl_disp,
        "{equity_json}":  json.dumps(s["equity"]),
        "{kpi_html}":     kpi_html,
        "{table_html}":   table_html,
        "{last_upd}":     last_upd,
        "{stake}":        f"{STAKE:.2f}",
    }.items():
        page = page.replace(k, v)
    return page


# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ct="text/html; charset=utf-8", hdrs=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type",          ct)
        self.send_header("Content-Length",        str(len(b)))
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options",       "DENY")
        self.send_header("Cache-Control",         "no-store")
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
                self._send(200, _LOGIN.replace("{error}", ""))
            return
        if path == "/dashboard":
            if not _valid_session(self._tok()):
                self._send(302, "", hdrs={"Location": "/"})
                return
            self._send(200, build_page(load_trades()))
            return
        self._send(404, "<h1>404</h1>")

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
            self._send(200, _LOGIN.replace("{error}",
                                           '<div class="err">Invalid credentials.</div>'))


if __name__ == "__main__":
    print(f"Dashboard → http://0.0.0.0:{PORT}  (user: {DASH_USER})")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()