"""
Polymarket Bot Dashboard v5
Fixes:
  1. FALSE UPDATES FIXED — balance card only reads from settled outcomes
     (win / loss / stop_loss). 'open' and 'unmatched' records no longer
     cause the balance to flicker or show wrong numbers.
  2. Full UI redesign — terminal-trading aesthetic, equity curve chart,
     proper KPI hierarchy, countdown refresh, no meta-refresh flicker.
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
STAKE       = float(os.environ.get("BOT_STAKE", "5.00"))

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
    if TRADES_FILE.exists():
        try: return json.loads(TRADES_FILE.read_text())
        except Exception: return []
    return []

# ── Stats ──────────────────────────────────────────────────────────────────────
def get_latest_balance(trades):
    """
    Walk trades in reverse. ONLY consider settled outcomes so the balance
    card never lies after an 'open' or 'unmatched' write.
    """
    for t in reversed(trades):
        if t.get("outcome") not in SETTLED:
            continue
        b = t.get("balance_after")
        if b is not None and b >= 0:
            return float(b)
    return None   # no settled trade yet

def compute_stats(trades):
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
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07080f;--sur:#0e0f1c;--bdr:#1b1d32;
  --acc:#00e5ff;--acc2:#7b61ff;
  --grn:#00ff88;--red:#ff4d6d;
  --txt:#dfe1f2;--dim:#555776;
  --mono:'Space Mono',monospace;
  --sans:'DM Sans',sans-serif;
}
body{
  font-family:var(--sans);background:var(--bg);color:var(--txt);
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background-image:
    radial-gradient(ellipse 60% 70% at 15% 50%,rgba(123,97,255,.09) 0%,transparent 55%),
    radial-gradient(ellipse 45% 55% at 85% 50%,rgba(0,229,255,.06) 0%,transparent 55%);
}
.shell{width:100%;max-width:400px;padding:0 24px}
.eye{
  font-family:var(--mono);font-size:9px;letter-spacing:.28em;
  color:var(--acc);text-transform:uppercase;
  display:flex;align-items:center;gap:10px;margin-bottom:24px;
}
.eye::before,.eye::after{content:'';flex:1;height:1px;background:var(--bdr)}
h1{font-size:30px;font-weight:700;letter-spacing:-.04em;line-height:1.1;margin-bottom:6px}
h1 em{font-style:normal;color:var(--acc)}
.sub{font-size:13px;color:var(--dim);margin-bottom:36px}
.fld{margin-bottom:18px}
label{display:block;font-family:var(--mono);font-size:9px;letter-spacing:.18em;
      color:var(--dim);text-transform:uppercase;margin-bottom:7px}
input{width:100%;background:var(--sur);border:1px solid var(--bdr);border-radius:6px;
      padding:13px 16px;color:var(--txt);font-family:var(--mono);font-size:13px;
      outline:none;transition:border-color .18s,box-shadow .18s}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(0,229,255,.07)}
button{
  width:100%;background:var(--acc2);color:#fff;border:none;border-radius:6px;
  padding:14px;font-family:var(--sans);font-size:14px;font-weight:700;cursor:pointer;
  letter-spacing:.02em;transition:all .2s;margin-top:8px;
}
button:hover{background:#6d54e8;transform:translateY(-1px);box-shadow:0 8px 24px rgba(123,97,255,.35)}
.err{background:rgba(255,77,109,.07);border:1px solid rgba(255,77,109,.2);
     border-radius:6px;padding:10px 14px;font-size:12px;color:var(--red);
     margin-bottom:18px;font-family:var(--mono)}
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
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07080f;--sur:#0e0f1c;--sur2:#12142a;--bdr:#1b1d32;--bdr2:#24273f;
  --acc:#00e5ff;--acc2:#7b61ff;
  --grn:#00ff88;--red:#ff4d6d;--amb:#ffbe0b;
  --txt:#dfe1f2;--txt2:#8082a4;--dim:#555776;
  --r:8px;
  --mono:'Space Mono',monospace;
  --sans:'DM Sans',sans-serif;
}
body{font-family:var(--sans);background:var(--bg);color:var(--txt);min-height:100vh;
     background-image:radial-gradient(ellipse 100% 25% at 50% 0%,rgba(123,97,255,.05) 0%,transparent 60%)}

/* scanlines */
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.025) 2px,rgba(0,0,0,.025) 4px)}

/* ── Nav ── */
.nav{
  position:sticky;top:0;z-index:50;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;height:50px;
  background:rgba(7,8,15,.93);border-bottom:1px solid var(--bdr);
  backdrop-filter:blur(14px);
}
.brand{font-family:var(--mono);font-size:12px;font-weight:700;
       letter-spacing:.12em;color:var(--acc);display:flex;align-items:center;gap:9px}
.brand-box{width:22px;height:22px;border:1.5px solid var(--acc);border-radius:4px;
           display:flex;align-items:center;justify-content:center;font-size:10px}
.nav-r{display:flex;align-items:center;gap:14px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;
      font-family:var(--mono);font-size:9px;letter-spacing:.08em;border:1px solid var(--bdr);color:var(--txt2)}
.pill.live{border-color:rgba(0,255,136,.25);color:var(--grn);background:rgba(0,255,136,.05)}
.pill.warn{border-color:rgba(255,190,11,.25);color:var(--amb);background:rgba(255,190,11,.05)}
.pill.bad {border-color:rgba(255,77,109,.25);color:var(--red);background:rgba(255,77,109,.05)}
.blink{width:5px;height:5px;border-radius:50%;background:currentColor;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.signout{font-family:var(--mono);font-size:9px;letter-spacing:.08em;
         color:var(--dim);text-decoration:none;padding:5px 11px;
         border:1px solid var(--bdr);border-radius:5px;transition:all .15s}
.signout:hover{color:var(--txt);border-color:var(--dim)}

/* ── Page ── */
.page{max-width:1200px;margin:0 auto;padding:26px 22px 80px;position:relative;z-index:1}
.sec-label{
  font-family:var(--mono);font-size:9px;letter-spacing:.22em;
  text-transform:uppercase;color:var(--dim);
  display:flex;align-items:center;gap:10px;margin-bottom:14px;margin-top:24px;
}
.sec-label::after{content:'';flex:1;height:1px;background:var(--bdr)}
.sec-label:first-child{margin-top:0}

/* ── Alert ── */
.alert{display:flex;align-items:flex-start;gap:14px;padding:13px 18px;
       border-radius:var(--r);margin-bottom:20px;font-size:13px;line-height:1.6}
.alert.warn{background:rgba(255,190,11,.06);border:1px solid rgba(255,190,11,.18)}
.alert.crit{background:rgba(255,77,109,.06);border:1px solid rgba(255,77,109,.18)}
.alert-ico{font-size:15px;margin-top:2px;flex-shrink:0}
.alert strong{font-weight:700}
.alert code{font-family:var(--mono);font-size:10px;background:rgba(255,255,255,.07);
            padding:1px 5px;border-radius:3px}

/* ── Hero row ── */
.hero{display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:stretch}
@media(max-width:680px){.hero{grid-template-columns:1fr}}

/* Balance card */
.balcard{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);
         padding:24px 26px;min-width:230px;position:relative;overflow:hidden}
.balcard::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--acc) 50%,transparent);opacity:.45}
.bal-lbl{font-family:var(--mono);font-size:9px;letter-spacing:.18em;
         text-transform:uppercase;color:var(--dim);margin-bottom:11px}
.bal-num{font-family:var(--mono);font-size:42px;font-weight:700;
         letter-spacing:-.04em;line-height:1}
.bal-num.ok {color:var(--grn)}
.bal-num.low{color:var(--amb)}
.bal-num.bad{color:var(--red)}
.bal-num.unk{color:var(--dim);font-size:28px}
.bal-sub{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:9px;line-height:1.5}
.bal-badge{display:inline-flex;align-items:center;gap:5px;margin-top:13px;
           padding:3px 10px;border-radius:20px;
           font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;font-weight:700}
.bal-badge.ok {background:rgba(0,255,136,.09);color:var(--grn);border:1px solid rgba(0,255,136,.18)}
.bal-badge.low{background:rgba(255,190,11,.09);color:var(--amb);border:1px solid rgba(255,190,11,.18)}
.bal-badge.bad{background:rgba(255,77,109,.09);color:var(--red);border:1px solid rgba(255,77,109,.18)}
.bal-badge.unk{background:rgba(85,87,118,.12);color:var(--dim);border:1px solid var(--bdr)}

/* Chart card */
.chartcard{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);
           padding:18px 22px;display:flex;flex-direction:column}
.ch-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.ch-title{font-family:var(--mono);font-size:10px;color:var(--txt2)}
.ch-pnl{font-family:var(--mono);font-size:22px;font-weight:700;letter-spacing:-.03em}
.ch-pnl.pos{color:var(--grn)}.ch-pnl.neg{color:var(--red)}
.ch-wrap{flex:1;min-height:85px;position:relative}
#eqchart{width:100%;height:100%;display:block}

/* ── KPI grid ── */
.kgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px}
.kpi{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);
     padding:16px 18px;position:relative;overflow:hidden}
.kpi-bar{position:absolute;top:0;left:0;width:3px;height:100%;
         border-radius:var(--r) 0 0 var(--r)}
.kpi-bar.g{background:var(--grn)}.kpi-bar.r{background:var(--red)}
.kpi-bar.a{background:var(--amb)}.kpi-bar.b{background:var(--acc2)}
.kpi-lbl{font-family:var(--mono);font-size:9px;letter-spacing:.14em;
         text-transform:uppercase;color:var(--dim);margin-bottom:9px}
.kpi-val{font-family:var(--mono);font-size:22px;font-weight:700;
         letter-spacing:-.02em;line-height:1}
.kpi-val.pos{color:var(--grn)}.kpi-val.neg{color:var(--red)}.kpi-val.neu{color:var(--txt)}
.kpi-sub{font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:5px}

/* ── Table ── */
.tcard{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);overflow:hidden}
.thead{padding:12px 18px;border-bottom:1px solid var(--bdr);
       display:flex;align-items:center;justify-content:space-between}
.thead-l{font-family:var(--mono);font-size:10px;color:var(--txt2)}
.thead-r{font-family:var(--mono);font-size:9px;color:var(--dim)}
.tscroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;white-space:nowrap}
thead th{padding:8px 14px;text-align:left;
         font-family:var(--mono);font-size:9px;letter-spacing:.13em;text-transform:uppercase;
         color:var(--dim);background:var(--sur2);border-bottom:1px solid var(--bdr)}
tbody td{padding:10px 14px;font-family:var(--mono);font-size:11px;
         border-bottom:1px solid rgba(27,29,50,.6);vertical-align:middle;color:var(--txt2)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:rgba(123,97,255,.04);color:var(--txt)}
.b{display:inline-block;padding:2px 7px;border-radius:3px;
   font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase}
.bw{background:rgba(0,255,136,.1);color:var(--grn);border:1px solid rgba(0,255,136,.18)}
.bl{background:rgba(255,77,109,.1);color:var(--red);border:1px solid rgba(255,77,109,.18)}
.bs{background:rgba(255,190,11,.1);color:var(--amb);border:1px solid rgba(255,190,11,.18)}
.bo{background:rgba(85,87,118,.1);color:var(--dim);border:1px solid var(--bdr)}
.bc{background:rgba(0,229,255,.08);color:var(--acc);border:1px solid rgba(0,229,255,.18)}
.pp{color:var(--grn)}.pn{color:var(--red)}
.tfoot{padding:9px 18px;border-top:1px solid var(--bdr);
       font-family:var(--mono);font-size:9px;color:var(--dim);
       display:flex;align-items:center;justify-content:space-between}
.empty{text-align:center;padding:60px 20px;
       font-family:var(--mono);font-size:12px;color:var(--dim);line-height:2}
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
  var W=rc.width||600,H=rc.height||90;
  cv.width=W*dpr;cv.height=H*dpr;
  cv.style.width=W+'px';cv.style.height=H+'px';
  var c=cv.getContext('2d');c.scale(dpr,dpr);
  var vals=raw.map(function(d){return d.val});
  var mn=Math.min.apply(null,vals.concat([0]));
  var mx=Math.max.apply(null,vals.concat([0]));
  var rng=mx-mn||1;
  var pt={t:8,b:8,l:4,r:4};
  var w=W-pt.l-pt.r,h=H-pt.t-pt.b;
  var xf=function(i){return pt.l+(i/(vals.length-1))*w};
  var yf=function(v){return pt.t+(1-(v-mn)/rng)*h};
  var zy=yf(0);
  var pos=vals[vals.length-1]>=0;
  var rgb=pos?'0,255,136':'255,77,109';
  var g=c.createLinearGradient(0,pt.t,0,H-pt.b);
  g.addColorStop(0,'rgba('+rgb+',.16)');g.addColorStop(1,'rgba('+rgb+',.0)');
  c.beginPath();c.moveTo(xf(0),zy);
  vals.forEach(function(v,i){c.lineTo(xf(i),yf(v))});
  c.lineTo(xf(vals.length-1),zy);c.closePath();
  c.fillStyle=g;c.fill();
  c.beginPath();c.strokeStyle='rgba(85,87,118,.35)';c.lineWidth=1;
  c.setLineDash([3,4]);c.moveTo(pt.l,zy);c.lineTo(W-pt.r,zy);c.stroke();c.setLineDash([]);
  c.beginPath();
  vals.forEach(function(v,i){i===0?c.moveTo(xf(i),yf(v)):c.lineTo(xf(i),yf(v))});
  c.strokeStyle=pos?'#00ff88':'#ff4d6d';c.lineWidth=1.5;c.lineJoin='round';c.stroke();
  var lx=xf(vals.length-1),ly=yf(vals[vals.length-1]);
  c.beginPath();c.arc(lx,ly,3,0,Math.PI*2);
  c.fillStyle=pos?'#00ff88':'#ff4d6d';c.fill();
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
