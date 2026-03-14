"""
Polymarket Bot Dashboard — v4 update
- Balance card: reads balance_after from latest trade in trades.json
- Low-balance alert banner when balance < 1.00 (the stake)
- All Telegram code removed
- No external dependencies — pure Python stdlib
"""

import os, json, hashlib, secrets, time
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

TRADES_FILE  = Path("trades.json")
DASH_USER    = os.environ.get("DASH_USER", "admin")
DASH_PASS    = os.environ.get("DASH_PASS", "changeme")
PORT         = int(os.environ.get("DASH_PORT", "8080"))
SESSION_TTL  = 3600
LOW_BAL_WARN = 1.00  # show alert banner when balance drops below stake

sessions: dict = {}

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

def get_latest_balance(trades):
    """
    Walk trades in reverse to find the most recent balance_after.
    Falls back to 0 if no completed trades yet.
    """
    for t in reversed(trades):
        bal = t.get("balance_after")
        if bal is not None and bal > 0:
            return float(bal)
    return None  # None = not yet known

def compute_stats(trades):
    done   = [t for t in trades if t.get("outcome") in ("win","loss")]
    wins   = [t for t in done if t["outcome"] == "win"]
    losses = [t for t in done if t["outcome"] == "loss"]
    total_net   = sum(t.get("net_profit", t.get("profit", 0)) for t in done)
    total_fees  = sum(t.get("fee_usdc", 0) for t in done)
    total_gross = sum(t.get("gross_profit", 0) for t in done)
    win_rate    = (len(wins) / len(done) * 100) if done else 0

    streak, streak_type = 0, ""
    for t in reversed(done):
        if streak == 0: streak_type, streak = t["outcome"], 1
        elif t["outcome"] == streak_type: streak += 1
        else: break

    best  = max(done, key=lambda t: t.get("net_profit", 0), default=None)
    worst = min(done, key=lambda t: t.get("net_profit", 0), default=None)

    return {
        "total": len(done), "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_net": round(total_net, 4),
        "total_gross": round(total_gross, 4),
        "total_fees": round(total_fees, 6),
        "streak": streak, "streak_type": streak_type,
        "best": round(best["net_profit"] if best else 0, 4),
        "worst": round(worst["net_profit"] if worst else 0, 4),
        "skipped": len([t for t in trades if t.get("outcome") == "skip"]),
    }

# ─── HTML pages ───────────────────────────────────────────────────────────────

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot — Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#09090f;--sur:#111119;--bdr:#1c1c2c;--acc:#7c6af7;--grn:#4ade80;--txt:#dddaf0;--mut:#6b6880;--err:#f87171;--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif}
body{font-family:var(--sans);background:var(--bg);color:var(--txt);min-height:100vh;display:flex;align-items:center;justify-content:center;background-image:radial-gradient(ellipse 60% 40% at 50% 0%,rgba(124,106,247,.07) 0%,transparent 70%)}
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:16px;padding:48px 40px;width:380px}
.logo{font-family:var(--mono);font-size:11px;letter-spacing:.2em;color:var(--acc);text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:8px}
.dot{width:6px;height:6px;background:var(--grn);border-radius:50%;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
h1{font-size:24px;font-weight:600;margin-bottom:6px;letter-spacing:-.02em}
.sub{font-size:13px;color:var(--mut);margin-bottom:36px}
label{display:block;font-size:11px;font-family:var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
input{width:100%;background:var(--bg);border:1px solid var(--bdr);border-radius:8px;padding:12px 14px;color:var(--txt);font-family:var(--mono);font-size:14px;outline:none;margin-bottom:20px;transition:border-color .15s}
input:focus{border-color:var(--acc)}
button{width:100%;background:var(--acc);color:white;border:none;border-radius:8px;padding:13px;font-family:var(--sans);font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.85}
.error{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);border-radius:8px;padding:10px 14px;font-size:13px;color:var(--err);margin-bottom:20px;font-family:var(--mono)}
</style></head>
<body><div class="card">
<div class="logo"><span class="dot"></span>Polybot Dashboard</div>
<h1>Sign in</h1><p class="sub">View trade analytics and balance</p>
{error}
<form method="POST" action="/login">
<label>Username</label><input type="text" name="username" autocomplete="username" required autofocus>
<label>Password</label><input type="password" name="password" autocomplete="current-password" required>
<button type="submit">Sign in →</button>
</form></div></body></html>"""

SHELL = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot Dashboard</title>
<meta http-equiv="refresh" content="20">
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#09090f;--sur:#111119;--sur2:#16161f;--bdr:#1c1c2c;--acc:#7c6af7;--grn:#4ade80;--red:#f87171;--amb:#fbbf24;--txt:#dddaf0;--mut:#6b6880;--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif}
body{font-family:var(--sans);background:var(--bg);color:var(--txt);min-height:100vh;background-image:radial-gradient(ellipse 80% 30% at 50% 0%,rgba(124,106,247,.05) 0%,transparent 60%)}
nav{display:flex;align-items:center;justify-content:space-between;padding:0 32px;height:56px;border-bottom:1px solid var(--bdr);position:sticky;top:0;background:rgba(9,9,15,.92);backdrop-filter:blur(10px);z-index:10}
.brand{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--acc);letter-spacing:.05em}
.nav-r{display:flex;align-items:center;gap:20px;font-size:12px;color:var(--mut);font-family:var(--mono)}
.ldot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px;animation:pulse 2s infinite}
.ldot.ok{background:var(--grn)}.ldot.warn{background:var(--amb)}.ldot.bad{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
a.out{color:var(--mut);text-decoration:none;padding:5px 12px;border:1px solid var(--bdr);border-radius:6px;transition:all .15s;font-family:var(--mono);font-size:11px}
a.out:hover{color:var(--txt);border-color:var(--mut)}
main{max-width:1100px;margin:0 auto;padding:28px 24px 64px}
h2{font-size:11px;font-family:var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.12em;margin-bottom:14px}

/* ── Alert banner ── */
.alert-banner{display:flex;align-items:center;gap:12px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:10px;padding:14px 18px;margin-bottom:24px;font-size:13px}
.alert-banner.critical{background:rgba(248,113,113,.08);border-color:rgba(248,113,113,.3)}
.alert-icon{font-size:18px;flex-shrink:0}
.alert-text{color:var(--txt);line-height:1.5}
.alert-text strong{font-weight:600}

/* ── Balance hero ── */
.balance-hero{background:var(--sur);border:1px solid var(--bdr);border-radius:14px;padding:24px 28px;margin-bottom:20px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.bh-left{flex:1}
.bh-label{font-size:11px;font-family:var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.12em;margin-bottom:8px}
.bh-amount{font-family:var(--mono);font-weight:600;letter-spacing:-.03em;line-height:1}
.bh-amount.ok{color:var(--grn);font-size:42px}
.bh-amount.low{color:var(--amb);font-size:42px}
.bh-amount.empty{color:var(--red);font-size:42px}
.bh-amount.unknown{color:var(--mut);font-size:32px}
.bh-sub{font-size:12px;color:var(--mut);margin-top:8px;font-family:var(--mono)}
.bh-right{text-align:right}
.bh-status{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;font-size:11px;font-family:var(--mono);font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.bh-status.ok{background:rgba(74,222,128,.12);color:var(--grn)}
.bh-status.low{background:rgba(251,191,36,.12);color:var(--amb)}
.bh-status.empty{background:rgba(248,113,113,.12);color:var(--red)}
.bh-status.unknown{background:rgba(107,104,128,.2);color:var(--mut)}
.status-dot{width:6px;height:6px;border-radius:50%;background:currentColor}

/* ── Stat grid ── */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:28px}
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;padding:18px 20px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--acc);opacity:.5}
.card.g::before{background:var(--grn)}.card.r::before{background:var(--red)}.card.a::before{background:var(--amb)}
.cl{font-size:11px;font-family:var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.cv{font-size:26px;font-weight:600;font-family:var(--mono);letter-spacing:-.02em;line-height:1}
.cv.pos{color:var(--grn)}.cv.neg{color:var(--red)}
.cs{font-size:11px;color:var(--mut);margin-top:5px;font-family:var(--mono)}

/* ── Table ── */
.sec{margin-bottom:28px}
.tw{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;overflow:hidden}
.th2{padding:14px 20px;border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between}
.th2 span{font-size:12px;font-family:var(--mono);color:var(--mut)}
table{width:100%;border-collapse:collapse}
th{padding:9px 16px;text-align:left;font-size:10px;font-family:var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.1em;background:var(--sur2);border-bottom:1px solid var(--bdr);white-space:nowrap}
td{padding:11px 16px;font-family:var(--mono);font-size:12px;border-bottom:1px solid rgba(28,28,44,.6);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(124,106,247,.03)}
.b{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.bw{background:rgba(74,222,128,.12);color:var(--grn)}.bl{background:rgba(248,113,113,.12);color:var(--red)}.bo{background:rgba(107,104,128,.2);color:var(--mut)}
.pp{color:var(--grn)}.pn{color:var(--red)}
.empty{text-align:center;padding:56px 20px;color:var(--mut);font-family:var(--mono);font-size:13px}
.note{font-size:11px;color:var(--mut);font-family:var(--mono);text-align:right;margin-top:6px}
</style></head>
<body>
<nav>
  <div class="brand">⬡ POLYBOT</div>
  <div class="nav-r">
    <span><span class="ldot {nav_dot_class}"></span>{nav_status}</span>
    <a href="/logout" class="out">sign out</a>
  </div>
</nav>
<main>{content}</main></body></html>"""

def build_content(trades):
    s   = compute_stats(trades)
    bal = get_latest_balance(trades)

    # ── Balance hero card ────────────────────────────────────────────────
    if bal is None:
        bal_class    = "unknown"
        bal_display  = "—"
        bal_sub      = "No trades recorded yet"
        status_class = "unknown"
        status_text  = "Awaiting data"
        nav_dot      = "ok"
        nav_status   = "Live · 20s refresh"
        alert_html   = ""
    elif bal <= 0:
        bal_class    = "empty"
        bal_display  = f"${bal:.4f}"
        bal_sub      = "Cannot place trades — balance is zero"
        status_class = "empty"
        status_text  = "Out of funds"
        nav_dot      = "bad"
        nav_status   = "ALERT: Out of funds"
        alert_html   = """<div class="alert-banner critical">
          <span class="alert-icon">&#9888;</span>
          <div class="alert-text"><strong>Balance is zero.</strong> The bot cannot place any more trades.
          Top up your Polymarket account with USDC to resume.</div></div>"""
    elif bal < 1.00:
        bal_class    = "low"
        bal_display  = f"${bal:.4f}"
        bal_sub      = f"Below $1.00 stake — bot is paused"
        status_class = "low"
        status_text  = "Low balance"
        nav_dot      = "warn"
        nav_status   = f"LOW BALANCE: ${bal:.4f}"
        alert_html   = f"""<div class="alert-banner">
          <span class="alert-icon">&#9888;</span>
          <div class="alert-text"><strong>Balance is below the $1.00 stake.</strong>
          Current: <strong>${bal:.4f}</strong>. The bot will pause until you top up your Polymarket account.</div></div>"""
    else:
        bal_class    = "ok"
        bal_display  = f"${bal:.4f}"
        bal_sub      = "Sufficient for next trade"
        status_class = "ok"
        status_text  = "Funded"
        nav_dot      = "ok"
        nav_status   = "Live · 20s refresh"
        alert_html   = ""

    balance_hero = f"""<div class="balance-hero">
      <div class="bh-left">
        <div class="bh-label">Current USDC balance</div>
        <div class="bh-amount {bal_class}">{bal_display}</div>
        <div class="bh-sub">{bal_sub}</div>
      </div>
      <div class="bh-right">
        <div class="bh-status {status_class}">
          <span class="status-dot"></span>{status_text}
        </div>
      </div>
    </div>"""

    # ── Stats grid ───────────────────────────────────────────────────────
    pc  = "pos" if s["total_net"] >= 0 else "neg"
    pcc = "g"   if s["total_net"] >= 0 else "r"
    wcc = "g"   if s["win_rate"] >= 60 else ("r" if s["win_rate"] < 40 else "")
    streak_str = (("🔥 " if s["streak_type"]=="win" else "🧊 ") + f"{s['streak']} {s['streak_type']}") if s["streak"] else "—"

    stats_grid = f"""<div class="sec">
      <h2>Performance overview</h2>
      <div class="grid">
        <div class="card {pcc}"><div class="cl">Net P&amp;L</div>
          <div class="cv {pc}">${s['total_net']:+.4f}</div>
          <div class="cs">gross ${s['total_gross']:+.4f} · fees ${s['total_fees']:.5f}</div></div>
        <div class="card {wcc}"><div class="cl">Win rate</div>
          <div class="cv">{s['win_rate']}%</div>
          <div class="cs">{s['wins']}W / {s['losses']}L</div></div>
        <div class="card"><div class="cl">Trades</div>
          <div class="cv">{s['total']}</div>
          <div class="cs">{s['skipped']} skipped</div></div>
        <div class="card"><div class="cl">Streak</div>
          <div class="cv" style="font-size:18px">{streak_str}</div><div class="cs">&nbsp;</div></div>
        <div class="card g"><div class="cl">Best trade</div>
          <div class="cv pos">${s['best']:+.4f}</div><div class="cs">net</div></div>
        <div class="card r"><div class="cl">Worst trade</div>
          <div class="cv neg">${s['worst']:+.4f}</div><div class="cs">net</div></div>
      </div>
    </div>"""

    # ── Trade history table ───────────────────────────────────────────────
    recent = list(reversed(trades[-100:]))
    if recent:
        rows = ""
        for t in recent:
            out = t.get("outcome","")
            if out == "win":
                badge = '<span class="b bw">WIN</span>'
            elif out == "loss":
                badge = '<span class="b bl">LOSS</span>'
            elif out == "stop_loss":
                badge = '<span class="b" style="background:rgba(251,191,36,.12);color:var(--amb)">STOP-LOSS</span>'
            elif out == "skip":
                reason = t.get("skip_reason","")
                badge = f'<span class="b bo">skip: {reason}</span>'
            else:
                badge = f'<span class="b bo">{out}</span>'

            net  = t.get("net_profit", t.get("profit", 0))
            pc2  = "pp" if net>0 else ("pn" if net<0 else "")
            ns   = f"${net:+.4f}" if net!=0 else "—"
            fee  = t.get("fee_usdc", 0)
            fs   = f"${fee:.5f}" if fee else "—"
            baft = t.get("balance_after")
            bs   = f"${baft:.4f}" if baft is not None else "—"
            ts   = t.get("timestamp","")[:19].replace("T"," ")
            side = t.get("side","—").upper()
            ep   = f"{t.get('entry_price',0):.4f}" if t.get("entry_price") else "—"
            stk  = f"${t.get('stake',0):.2f}" if t.get("stake",0)>0 else "—"
            ex = t.get("exit_price", 0)
            exs = f"{ex:.4f}" if ex and ex > 0 else "—"
            rows += f"<tr><td>{ts}</td><td>{side}</td><td>{ep}</td><td>{exs}</td><td>{stk}</td><td>{badge}</td><td class='{pc2}'>{ns}</td><td>{fs}</td><td>{bs}</td></tr>"

        table_html = f"""<div class="sec">
          <h2>Trade history</h2>
          <div class="tw">
            <div class="th2"><span>Last {len(recent)} trades</span><span>BTC 5m · $1 stake</span></div>
            <table><thead><tr>
              <th>Time (UTC)</th><th>Side</th><th>Entry</th><th>Exit</th><th>Stake</th>
              <th>Outcome</th><th>Net P&amp;L</th><th>Fee</th><th>Balance after</th>
            </tr></thead><tbody>{rows}</tbody></table>
          </div>
          <div class="note">Auto-refreshes every 20s</div>
        </div>"""
    else:
        table_html = """<div class="sec"><h2>Trade history</h2>
          <div class="tw"><div class="empty">No trades yet — bot is monitoring BTC 5m markets...</div></div>
        </div>"""

    content = alert_html + balance_hero + stats_grid + table_html
    return content, nav_dot, nav_status

# ─── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ct="text/html; charset=utf-8", hdrs=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        if hdrs:
            for k,v in hdrs.items(): self.send_header(k,v)
        self.end_headers()
        self.wfile.write(b)

    def _tok(self): return _get_cookie(self.headers, "session")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/logout":
            sessions.pop(self._tok(), None)
            self._send(302,"",hdrs={"Location":"/","Set-Cookie":"session=; Max-Age=0; Path=/"})
            return

        if path in ("/","/login"):
            if _valid_session(self._tok()): self._send(302,"",hdrs={"Location":"/dashboard"})
            else: self._send(200, LOGIN_PAGE.replace("{error}",""))
            return

        if path == "/dashboard":
            if not _valid_session(self._tok()):
                self._send(302,"",hdrs={"Location":"/"})
                return
            trades = load_trades()
            content, nav_dot, nav_status = build_content(trades)
            page = (SHELL
                    .replace("{content}", content)
                    .replace("{nav_dot_class}", nav_dot)
                    .replace("{nav_status}", nav_status))
            self._send(200, page)
            return

        self._send(404,"<h1>404</h1>")

    def do_POST(self):
        if urlparse(self.path).path == "/login":
            length = int(self.headers.get("Content-Length",0))
            body   = self.rfile.read(length).decode()
            p      = parse_qs(body)
            user   = p.get("username",[""])[0]
            pw     = p.get("password",[""])[0]
            ok_u   = secrets.compare_digest(user.encode(), DASH_USER.encode())
            ok_p   = secrets.compare_digest(pw.encode(),   DASH_PASS.encode())
            if ok_u and ok_p:
                tok = _new_session()
                self._send(302,"",hdrs={
                    "Location":"/dashboard",
                    "Set-Cookie":f"session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}",
                })
            else:
                self._send(200, LOGIN_PAGE.replace("{error}",'<div class="error">Invalid credentials.</div>'))
            return
        self._send(405,"Method Not Allowed")

if __name__ == "__main__":
    print(f"Dashboard → http://0.0.0.0:{PORT}  (user: {DASH_USER})")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
