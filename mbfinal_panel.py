"""
Admin web panel (runs inside the bot process, no extra dependencies).
Login with ADMIN_PASSWORD. Shows stats, live playback, groups and errors.
No member join/leave history is stored or shown.
"""
import hashlib
import hmac
import http.server
import json
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

SESSION_SECONDS = 7 * 24 * 3600


def start_panel(db, get_live, do_action, port, password):
    secret = hashlib.sha256(("panel:" + password).encode()).digest()

    def make_token():
        exp = str(int(time.time()) + SESSION_SECONDS)
        sig = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def valid_token(tok):
        try:
            exp, sig = tok.split(".", 1)
            good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, good) and int(exp) > time.time()
        except Exception:
            return False

    def overview(rng):
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = {
            "today": midnight.timestamp(),
            "7d": (now - timedelta(days=7)).timestamp(),
            "30d": (now - timedelta(days=30)).timestamp(),
            "all": 0,
        }.get(rng, midnight.timestamp())

        def count(t):
            return db("SELECT COUNT(*) FROM events WHERE type=? AND ts>=?", (t, since))[0][0]

        start14 = (midnight - timedelta(days=13)).timestamp()
        buckets = {(midnight - timedelta(days=i)).date(): 0 for i in range(13, -1, -1)}
        for (ts,) in db("SELECT ts FROM events WHERE type='play' AND ts>=?", (start14,)):
            d = datetime.fromtimestamp(ts).date()
            if d in buckets:
                buckets[d] += 1

        groups = db(
            "SELECT g.chat_id,g.title,g.active,g.added_ts,"
            "(SELECT COUNT(*) FROM events e WHERE e.type='play' AND e.chat_id=g.chat_id AND e.ts>=?),"
            "(SELECT COUNT(*) FROM events e WHERE e.type='error' AND e.chat_id=g.chat_id AND e.ts>=?) "
            "FROM groups g ORDER BY 5 DESC, g.added_ts DESC LIMIT 200",
            (since, since),
        )
        errors = db(
            "SELECT e.ts,e.chat_id,COALESCE(g.title,'Unknown'),e.detail FROM events e "
            "LEFT JOIN groups g ON g.chat_id=e.chat_id WHERE e.type='error' "
            "ORDER BY e.ts DESC LIMIT 20"
        )
        top = db(
            "SELECT detail,COUNT(*) FROM events WHERE type='play' AND ts>=? "
            "GROUP BY detail ORDER BY 2 DESC LIMIT 10",
            (since,),
        )
        return {
            "totals": {
                "groups": db("SELECT COUNT(*) FROM groups WHERE active=1")[0][0],
                "users": db("SELECT COUNT(*) FROM users")[0][0],
                "songs": db("SELECT COUNT(*) FROM songs")[0][0],
            },
            "range": {
                "plays": count("play"),
                "errors": count("error"),
                "added": count("bot_added"),
                "removed": count("bot_removed"),
            },
            "daily": [{"d": d.strftime("%d %b"), "n": n} for d, n in buckets.items()],
            "top": [{"title": t, "n": n} for t, n in top],
            "groups": [
                {"id": g[0], "title": g[1] or "Untitled", "active": bool(g[2]),
                 "added": datetime.fromtimestamp(g[3]).strftime("%d %b %Y") if g[3] else "",
                 "plays": g[4], "errors": g[5]}
                for g in groups
            ],
            "errors": [
                {"time": datetime.fromtimestamp(e[0]).strftime("%d %b %H:%M"), "id": e[1],
                 "group": e[2], "detail": (e[3] or "")[:160]}
                for e in errors
            ],
            "live": get_live(),
        }

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "MusicPanel"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session" and valid_token(v):
                    return True
            return False

        def do_HEAD(self):
            self._send(200, b"")

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/health":
                return self._send(200, "ok", "text/plain")
            if url.path == "/logout":
                return self._send(302, "", extra=[("Location", "/"), ("Set-Cookie", "session=; Max-Age=0; Path=/")])
            if url.path == "/api/overview":
                if not self._authed():
                    return self._send(401, '{"error":"auth"}', "application/json")
                rng = urllib.parse.parse_qs(url.query).get("range", ["today"])[0]
                return self._send(200, json.dumps(overview(rng)), "application/json")
            if url.path == "/":
                return self._send(200, DASHBOARD if self._authed() else login_page(""))
            self._send(404, "Not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 10000)).decode("utf-8", "ignore")
            if self.path == "/login":
                pw = urllib.parse.parse_qs(raw).get("password", [""])[0]
                if hmac.compare_digest(pw.encode(), password.encode()):
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                    cookie = f"session={make_token()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}{secure}"
                    return self._send(302, "", extra=[("Location", "/"), ("Set-Cookie", cookie)])
                time.sleep(1.5)
                return self._send(401, login_page("Incorrect password."))
            if self.path == "/api/action":
                if not self._authed():
                    return self._send(401, '{"error":"auth"}', "application/json")
                try:
                    body = json.loads(raw)
                    msg = do_action(body["action"], int(body["chat_id"]))
                    return self._send(200, json.dumps({"ok": True, "message": msg}), "application/json")
                except Exception as e:
                    return self._send(400, json.dumps({"ok": False, "message": str(e)[:200]}), "application/json")
            self._send(404, "Not found", "text/plain")

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


STYLE = """
:root{--bg:#f4f5f7;--card:#fff;--text:#111827;--muted:#6b7280;--line:#e5e7eb;--accent:#4f46e5;--accent-soft:#eef2ff;--ok:#059669;--bad:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0b0d12;--card:#141821;--text:#e5e7eb;--muted:#8b93a3;--line:#232937;--accent:#818cf8;--accent-soft:#1c2140;--ok:#34d399;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
"""

LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Music Bot Admin</title><style>%STYLE%
.box{max-width:360px;margin:12vh auto;padding:0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px}
h1{font-size:20px;margin:0 0 4px}p{color:var(--muted);margin:0 0 20px}
input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px}
button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:10px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.err{color:var(--bad);margin-top:12px;min-height:20px}</style></head><body><div class="box"><div class="card">
<h1>Music Bot Admin</h1><p>Sign in to continue.</p>
<form method="post" action="/login"><input type="password" name="password" placeholder="Password" autofocus required>
<button type="submit">Sign in</button></form><div class="err">%ERR%</div></div></div></body></html>"""


def login_page(err):
    return LOGIN.replace("%STYLE%", STYLE).replace("%ERR%", err)


DASHBOARD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Music Bot Admin</title><style>%STYLE%
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:16px 20px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{font-weight:700;font-size:16px}.brand span{color:var(--muted);font-weight:500;margin-left:8px;font-size:12px}
.tabs{display:flex;gap:4px;background:var(--bg);padding:4px;border-radius:10px}
.tabs button{border:0;background:none;color:var(--muted);padding:6px 12px;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer}
.tabs button.on{background:var(--card);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.12)}
a.out{color:var(--muted);text-decoration:none;font-size:13px}
main{max-width:1100px;margin:0 auto;padding:20px;display:grid;gap:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.kpi .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.kpi .v{font-size:26px;font-weight:700;margin-top:2px}
h2{font-size:14px;margin:0 0 12px;font-weight:700}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.row{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--line);align-items:center}.row:first-of-type{border-top:0}
.row .t{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.muted{color:var(--muted);font-size:12px}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;font-weight:600;background:var(--accent-soft);color:var(--accent);white-space:nowrap}
.pill.ok{background:rgba(5,150,105,.14);color:var(--ok)}.pill.bad{background:rgba(220,38,38,.14);color:var(--bad)}
.btn{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:5px 10px;font-size:12px;font-weight:600;cursor:pointer;margin-left:6px}
.btn.d{color:var(--bad)}.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:520px}
th{text-align:left;color:var(--muted);font-size:12px;font-weight:600;padding:8px 6px;border-bottom:1px solid var(--line)}td{padding:9px 6px;border-bottom:1px solid var(--line)}
.empty{color:var(--muted);padding:8px 0}svg text{fill:var(--muted);font-size:10px}</style></head><body>
<header><div class="brand">Music Bot<span>Admin</span></div>
<div class="tabs" id="tabs"></div><a class="out" href="/logout">Sign out</a></header>
<main>
<div class="kpis" id="kpis"></div>
<div class="card"><h2>Plays, last 14 days</h2><div id="chart"></div></div>
<div class="cols"><div class="card"><h2>Live now</h2><div id="live"></div></div>
<div class="card"><h2>Top songs</h2><div id="top"></div></div></div>
<div class="card"><h2>Groups</h2><div class="wrap"><table><thead><tr><th>Group</th><th>Status</th><th>Plays</th><th>Errors</th><th>Added</th></tr></thead><tbody id="groups"></tbody></table></div></div>
<div class="card"><h2>Recent errors</h2><div id="errors"></div></div>
</main>
<script>
let range="today";const $=s=>document.querySelector(s);
function h(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function clear(e){while(e.firstChild)e.removeChild(e.firstChild)}
const tabs=[["today","Today"],["7d","7 days"],["30d","30 days"],["all","All time"]];
function drawTabs(){const t=$("#tabs");clear(t);tabs.forEach(([k,l])=>{const b=h("button",k===range?"on":"",l);b.onclick=()=>{range=k;drawTabs();load()};t.appendChild(b)})}
function kpi(l,v){const c=h("div","card kpi");c.appendChild(h("div","l",l));c.appendChild(h("div","v",String(v)));return c}
function empty(box,msg){box.appendChild(h("div","empty",msg))}
async function act(action,id){try{const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,chat_id:id})});const j=await r.json();if(!j.ok)alert(j.message)}catch(e){alert("Request failed")}load()}
function chart(data){const box=$("#chart");clear(box);const W=700,H=170,pad=22,max=Math.max(1,...data.map(d=>d.n));
const ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox","0 0 "+W+" "+H);svg.style.width="100%";
const bw=(W-pad)/data.length;data.forEach((d,i)=>{const bh=(H-40)*d.n/max,x=pad+i*bw+4;
const r=document.createElementNS(ns,"rect");r.setAttribute("x",x);r.setAttribute("y",H-22-bh);r.setAttribute("width",bw-8);r.setAttribute("height",Math.max(bh,2));r.setAttribute("rx",4);r.setAttribute("fill","var(--accent)");r.setAttribute("opacity",d.n?1:.25);svg.appendChild(r);
if(d.n){const t=document.createElementNS(ns,"text");t.setAttribute("x",x+(bw-8)/2);t.setAttribute("y",H-26-bh);t.setAttribute("text-anchor","middle");t.textContent=d.n;svg.appendChild(t)}
if(i%2===0){const t=document.createElementNS(ns,"text");t.setAttribute("x",x+(bw-8)/2);t.setAttribute("y",H-6);t.setAttribute("text-anchor","middle");t.textContent=d.d;svg.appendChild(t)}});box.appendChild(svg)}
async function load(){let d;try{const r=await fetch("/api/overview?range="+range);if(r.status===401){location.href="/";return}d=await r.json()}catch(e){return}
const k=$("#kpis");clear(k);[["Active groups",d.totals.groups],["Bot users",d.totals.users],["Songs in catalog",d.totals.songs],["Songs played",d.range.plays],["Playback errors",d.range.errors],["Groups added",d.range.added],["Groups removed",d.range.removed]].forEach(([l,v])=>k.appendChild(kpi(l,v)));
chart(d.daily);
const live=$("#live");clear(live);if(!d.live.length)empty(live,"Nothing is playing right now.");
d.live.forEach(x=>{const r=h("div","row");const left=h("div","t");left.appendChild(h("div","t",x.track));left.appendChild(h("div","muted",x.group+" · "+x.queue+" in queue"+(x.radio?" · Radio":"")+(x.paused?" · Paused":"")));r.appendChild(left);
const b=h("div");const s=h("button","btn","Skip");s.onclick=()=>act("skip",x.chat_id);const p=h("button","btn d","Stop");p.onclick=()=>act("stop",x.chat_id);b.appendChild(s);b.appendChild(p);r.appendChild(b);live.appendChild(r)});
const top=$("#top");clear(top);if(!d.top.length)empty(top,"No plays in this period.");
d.top.forEach((x,i)=>{const r=h("div","row");r.appendChild(h("div","t",(i+1)+". "+x.title));r.appendChild(h("span","pill",x.n+" plays"));top.appendChild(r)});
const g=$("#groups");clear(g);if(!d.groups.length){const tr=h("tr");const td=h("td","empty","No groups yet.");td.colSpan=5;tr.appendChild(td);g.appendChild(tr)}
d.groups.forEach(x=>{const tr=h("tr");const a=h("td");a.appendChild(h("div","",x.title));a.appendChild(h("div","muted",String(x.id)));tr.appendChild(a);
const s=h("td");s.appendChild(h("span","pill "+(x.active?"ok":"bad"),x.active?"Active":"Removed"));tr.appendChild(s);
tr.appendChild(h("td","",String(x.plays)));tr.appendChild(h("td","",String(x.errors)));tr.appendChild(h("td","muted",x.added));g.appendChild(tr)});
const e=$("#errors");clear(e);if(!d.errors.length)empty(e,"No errors recorded.");
d.errors.forEach(x=>{const r=h("div","row");const l=h("div","t");l.appendChild(h("div","",x.group));l.appendChild(h("div","muted",x.detail));r.appendChild(l);r.appendChild(h("span","muted",x.time));e.appendChild(r)})}
drawTabs();load();setInterval(load,8000);
</script></body></html>""".replace("%STYLE%", STYLE)
