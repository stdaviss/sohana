"""
SOHANA demo environment (AFTS 2026 booth).

Loaded only when the DEMO_MODE=1 environment variable is set, from a separate
Railway service with its own database. It never runs on the live site.

What it adds to the real app:
  * /demo                 mobile-first welcome page (EN/FR): name + email -> a real
                          account in the demo database, signed in, with simulated funds
  * every new account     gets a @hanatag, verified KYC and starting balances, so
                          transfers, circles and campaigns work immediately
  * payment codes         shown on screen (no SMS/email in the demo)
  * banner                "Demo environment - simulated money" on every page
  * live updates          signed-in pages poll /api/demo/pulse and refresh when the
                          visitor's balance, score, circles or notifications change
  * /admin/demo           control room: live list of demo sign-ups with balances and
                          NCS, a "credit funds" action and a CSV export of the leads
"""

import csv
import io
import os
import random
import string
import uuid

from flask import Response, jsonify, redirect, render_template, request, session

import auth
from database import fetchall, fetchone, get_db, post_transaction, push_notification

DEMO_MODE = os.environ.get("DEMO_MODE") == "1"

# Simulated money every new demo account starts with: (currency, cents)
START_FUNDS = [("EUR", 50000), ("RWF", 30000000)]
START_LABEL = "Demo starting balance"

# Pages where a change is applied by reloading automatically once the visitor is idle.
# Fictional accounts the platform seeds on a fresh database; hidden from the control room.
SEED_PHONES = ("+33611000001", "+33611000002", "+25078100001", "+44795000001", "+33611000003")

LIVE_PAGES = ("/dashboard", "/wallet", "/ncs", "/circles", "/history", "/profile", "/passport", "/notifications")


# --------------------------------------------------------------------------- helpers

def _unique_hanatag(name):
    from database import generate_hanatag

    for _ in range(20):
        tag = generate_hanatag(name or "member")
        if not fetchone("SELECT 1 FROM users WHERE hanatag=?", (tag,)):
            return tag
    return "@demo" + uuid.uuid4().hex[:8]


def _short_tag(first_name, last_name):
    """Readable handle like @amina.u when free; otherwise the platform's generator."""
    import re

    base = re.sub(r"[^a-z0-9]", "", (first_name or "").lower())[:14]
    last = re.sub(r"[^a-z0-9]", "", (last_name or "").lower())[:1]
    if base:
        for cand in ([f"@{base}.{last}"] if last else []) + [f"@{base}"] + [
            f"@{base}{random.randint(10, 99)}" for _ in range(5)
        ]:
            if len(cand) >= 4 and not fetchone("SELECT 1 FROM users WHERE hanatag=?", (cand,)):
                return cand
    return _unique_hanatag(f"{first_name} {last_name}")


def provision(uid):
    """Make a fresh account usable for the demo. Safe to call more than once."""
    u = fetchone("SELECT id, full_name, first_name, last_name, hanatag FROM users WHERE id=?", (uid,))
    if not u:
        return
    first = u["first_name"] or (u["full_name"] or "").split(" ")[0]
    last = u["last_name"] or ((u["full_name"] or "").split(" ")[-1] if " " in (u["full_name"] or "") else "")
    with get_db() as db:
        if not u["hanatag"]:
            db.execute("UPDATE users SET hanatag=? WHERE id=?", (_short_tag(first, last), uid))
        db.execute("UPDATE users SET kyc_status='verified' WHERE id=?", (uid,))
        try:
            db.execute("UPDATE users SET kyc_level='full' WHERE id=?", (uid,))
        except Exception:
            pass
    already = fetchone(
        """SELECT 1 FROM wallet_transactions wt JOIN wallets w ON w.id = wt.wallet_id
           WHERE w.user_id=? AND wt.description=?""",
        (uid, START_LABEL),
    )
    if already:
        return
    for cur, cents in START_FUNDS:
        w = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (uid, cur))
        if not w:
            with get_db() as db:
                db.execute(
                    "INSERT INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,0)",
                    (str(uuid.uuid4()), uid, cur),
                )
            w = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (uid, cur))
        post_transaction(w["id"], cents, START_LABEL, tx_type="deposit")
    push_notification(
        uid,
        "Welcome to the SOHANA demo",
        "You have simulated money in EUR and RWF. Try a transfer, join a circle or back a campaign.",
        "success",
        "/wallet",
    )


def _pulse_signature(uid):
    tx = fetchone(
        """SELECT COUNT(*) AS c, COALESCE(SUM(wt.amount_cents),0) AS s
           FROM wallet_transactions wt JOIN wallets w ON w.id = wt.wallet_id WHERE w.user_id=?""",
        (uid,),
    )
    u = fetchone("SELECT ncs_score FROM users WHERE id=?", (uid,))
    n = fetchone("SELECT COUNT(*) AS c FROM notifications WHERE user_id=?", (uid,))
    m = fetchone("SELECT COUNT(*) AS c FROM rosca_members WHERE user_id=?", (uid,))
    c = fetchone("SELECT COUNT(*) AS c FROM contributions WHERE user_id=?", (uid,))
    return f"{tx['c']}.{tx['s']}.{u['ncs_score'] if u else 0}.{n['c']}.{m['c']}.{c['c']}"


def _latest_notification(uid):
    row = fetchone(
        "SELECT id, title, body FROM notifications WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (uid,),
    )
    return dict(row) if row else None


HEAD_HTML = """
<link rel="stylesheet" href="/static/css/demo-theme.css?v=1">
<script>try{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('sohana-theme','dark');}catch(e){}</script>
<script src="/static/js/demo-ui.js?v=1" defer></script>
"""


def _balances(uid):
    rows = fetchall(
        """SELECT w.currency,
                  COALESCE((SELECT wt.balance_after FROM wallet_transactions wt
                            WHERE wt.wallet_id = w.id ORDER BY wt.created_at DESC, wt.rowid DESC LIMIT 1), 0) AS bal
           FROM wallets w WHERE w.user_id=? ORDER BY w.is_default DESC, w.currency""",
        (uid,),
    )
    return [{"currency": r["currency"], "cents": r["bal"]} for r in rows]


BANNER_HTML = """
<style>a[href="/pools"],a[href^="/pools/"]{display:none!important}#survey-popup{display:none!important}body:has(.modal-backdrop.open) #demo-banner,body:has(.modal-backdrop.open) #demo-live{display:none!important}</style>
<div id="demo-banner" aria-label="Demo environment, simulated money" style="position:fixed;right:10px;bottom:84px;
 z-index:2147483000;pointer-events:none;background:#F4C06A;color:#14130F;border-radius:999px;padding:3px 10px;
 font:700 10px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;letter-spacing:.08em;text-transform:uppercase;white-space:nowrap;
 box-shadow:0 2px 8px rgba(0,0,0,.25)">Demo &middot; simulated money</div>
"""

LIVE_JS = """
<div id="demo-live" style="display:none;position:fixed;left:50%%;bottom:18px;transform:translateX(-50%%);z-index:2147483001;
 background:#9EE493;color:#0E120F;font:600 14px/1.2 system-ui,-apple-system,Segoe UI,sans-serif;padding:12px 18px;
 border-radius:999px;box-shadow:0 8px 24px rgba(0,0,0,.35);cursor:pointer">New activity &middot; tap to refresh</div>
<script>
(function(){
  var LIVE=%(live)s, sig=null, seen=null, last=Date.now(), pill=document.getElementById('demo-live');
  ['pointerdown','keydown','touchstart','scroll'].forEach(function(e){addEventListener(e,function(){last=Date.now();},{passive:true});});
  pill.addEventListener('click',function(){location.reload();});
  function busy(){
    var a=document.activeElement;
    if(a&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))return true;
    return !!document.querySelector('.modal-backdrop.open,.modal.open,[aria-modal="true"]');
  }
  function tick(){
    if(document.visibilityState!=='visible')return;
    fetch('/api/demo/pulse',{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.json():null;}).then(function(d){
      if(!d||!d.sig)return;
      if(sig===null){sig=d.sig;seen=d.n&&d.n.id;return;}
      if(d.sig!==sig){
        sig=d.sig;
        var fresh=d.n&&d.n.id!==seen; if(d.n)seen=d.n.id;
        if(fresh&&window.dzToast){window.dzToast(d.n.title,d.n.body);}
        if(LIVE&&!busy()&&Date.now()-last>4000){setTimeout(function(){if(!busy())location.reload();else pill.style.display='block';},fresh?2600:0);return;}
        pill.style.display='block';
      }
    }).catch(function(){});
  }
  tick(); setInterval(tick,4000);

  // Booth devices (opened once with /demo?booth=1): sign out after 2.5 minutes untouched.
  if(/(?:^|; )demo_booth=1/.test(document.cookie)){
    var warn=null;
    setInterval(function(){
      var idle=Date.now()-last;
      if(idle>165000){fetch('/api/auth/logout',{method:'POST',credentials:'same-origin'}).finally(function(){location.href='/demo';});return;}
      if(idle>150000){
        if(!warn){warn=document.createElement('div');
          warn.style.cssText='position:fixed;inset:0;z-index:2147483002;background:rgba(14,18,15,.85);display:grid;place-items:center;font:600 20px system-ui,sans-serif;color:#F4F2EC;text-align:center;padding:24px';
          warn.textContent='Still there? Tap anywhere to keep going. / Toujours là ? Touchez l’écran pour continuer.';
          warn.addEventListener('pointerdown',function(){last=Date.now();warn.remove();warn=null;});
          document.body.appendChild(warn);}
      }
    },1000);
  }
})();
</script>
"""


# --------------------------------------------------------------------------- install

def install(app, admin_required):
    @app.before_request
    def _demo_root():
        if request.path == "/" and "user_id" not in session:
            # Serve the welcome page directly (200, not a redirect) so platform
            # health checks on "/" keep passing.
            return render_template("demo_welcome.html", user=None)
        # Contribution pools are not part of the booth demo (two competing pool schemas
        # still need reconciling); send visitors to circles instead of an error page.
        if request.path == "/pools" or request.path.startswith("/pools/"):
            return redirect("/circles")
        return None

    @app.after_request
    def _demo_after(resp):
        try:
            path = request.path
            # 1. every account created through the normal sign-up form gets demo set-up too
            if path == "/api/auth/register" and resp.status_code == 200 and session.get("user_id"):
                provision(session["user_id"])

            # 2. show the one-time payment code on screen
            if path == "/api/wallet/pay/initiate" and resp.status_code == 200 and resp.is_json:
                data = resp.get_json(silent=True) or {}
                iid = data.get("intent_id")
                if iid and data.get("challenge") == "email_otp" and session.get("user_id"):
                    row = fetchone(
                        """SELECT code FROM otp_requests WHERE user_id=? AND purpose=? AND used=0
                           ORDER BY created_at DESC LIMIT 1""",
                        (session["user_id"], f"pay:{iid}"),
                    )
                    if row:
                        data["demo_code"] = row["code"]
                        data["masked_target"] = "this screen (demo code: " + row["code"] + ")"
                        resp.set_data(__import__("json").dumps(data))

            # 3. banner + live updates on every HTML page
            if (
                resp.status_code == 200
                and resp.mimetype == "text/html"
                and not resp.direct_passthrough
                and not path.startswith("/api/")
            ):
                html = resp.get_data(as_text=True)
                # app screens (the signed-in shell) get the demo theme and UI layer
                if 'class="app-shell"' in html and "demo-theme.css" not in html and "</head>" in html:
                    k = html.find("</head>")
                    html = html[:k] + HEAD_HTML + html[k:]
                if "<body" in html and 'id="demo-banner"' not in html and path != "/demo/live":
                    i = html.find(">", html.find("<body")) + 1
                    html = html[:i] + BANNER_HTML + html[i:]
                    if session.get("user_id") and not path.startswith("/admin"):
                        live = "true" if path.startswith(LIVE_PAGES) else "false"
                        j = html.rfind("</body>")
                        snippet = LIVE_JS % {"live": live}
                        html = html[:j] + snippet + html[j:] if j != -1 else html + snippet
                    resp.set_data(html)
        except Exception:
            app.logger.exception("demo after_request failed")
        return resp

    @app.route("/demo")
    def demo_welcome():
        user = auth.get_current_user() if "user_id" in session else None
        return render_template("demo_welcome.html", user=user)

    @app.route("/api/demo/start", methods=["POST"])
    def api_demo_start():
        d = request.get_json(silent=True) or {}
        name = " ".join((d.get("name") or "").split())[:60]
        email = (d.get("email") or "").strip().lower()[:254]
        consent = d.get("consent") is True
        lang = "fr" if d.get("lang") == "fr" else "en"
        if len(name) < 2:
            return jsonify({"error": "name"}), 400
        import re

        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify({"error": "email"}), 400

        # A returning visitor typing the same email gets their demo account back.
        if email:
            existing = fetchone("SELECT id FROM users WHERE lower(email)=? AND COALESCE(is_admin,0)=0", (email,))
            if existing:
                session["user_id"] = existing["id"]
                session.permanent = True
                return jsonify({"ok": True, "returning": True, "next": "/dashboard"})

        parts = name.split(" ")
        first, last = parts[0], (" ".join(parts[1:]) or None)
        phone = "+999" + "".join(random.choices(string.digits, k=9))
        while fetchone("SELECT 1 FROM users WHERE phone=?", (phone,)):
            phone = "+999" + "".join(random.choices(string.digits, k=9))
        password = uuid.uuid4().hex + "Aa1!"
        try:
            uid = auth.register_user(
                phone=phone, full_name=name, password=password, email=email or None,
                country="RW", first_name=first, last_name=last,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with get_db() as db:
            db.execute("UPDATE users SET language=? WHERE id=?", (lang, uid))
            if consent and email:
                db.execute(
                    """UPDATE users SET marketing_consent=1, marketing_consent_at=datetime('now'),
                       marketing_consent_source='afts_demo' WHERE id=?""",
                    (uid,),
                )
        provision(uid)
        session["user_id"] = uid
        session.permanent = True
        return jsonify({"ok": True, "next": "/dashboard"})

    @app.route("/api/demo/pulse")
    def api_demo_pulse():
        uid = session.get("user_id")
        if not uid:
            return jsonify({"sig": None})
        return jsonify({"sig": _pulse_signature(uid), "n": _latest_notification(uid)})

    # ---------------- booth screen (public, first names only) ----------------

    def _short(name):
        parts = (name or "").split()
        if not parts:
            return "Someone"
        return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")

    def _money(cents, cur):
        dp = 0 if cur in ("RWF", "XAF", "NGN") else 2
        return f"{cur} {cents / 100:,.{dp}f}"

    @app.route("/demo/live")
    def demo_live():
        return render_template("demo_live.html")

    @app.route("/api/demo/wall")
    def api_demo_wall():
        seed = ",".join("?" * len(SEED_PHONES))
        visitors = f"SELECT id FROM users WHERE COALESCE(is_admin,0)=0 AND phone NOT IN ({seed})"
        V = tuple(SEED_PHONES)
        stats = {
            "members": fetchone(f"SELECT COUNT(*) AS c FROM ({visitors})", V)["c"],
            "transfers": fetchone(
                f"SELECT COUNT(*) AS c FROM payment_intents WHERE status='completed' AND sender_id IN ({visitors})", V
            )["c"],
            "circles": fetchone(f"SELECT COUNT(*) AS c FROM roscas WHERE organiser_id IN ({visitors})", V)["c"],
            "contributions": fetchone(
                f"SELECT COUNT(*) AS c FROM contributions WHERE status IN ('paid','late') AND user_id IN ({visitors})", V
            )["c"],
            "avg_ncs": fetchone(f"SELECT COALESCE(ROUND(AVG(ncs_score)),0) AS c FROM users WHERE id IN ({visitors})", V)["c"],
        }
        vol = fetchall(
            f"""SELECT currency, SUM(amount_cents) AS s FROM payment_intents
                WHERE status='completed' AND sender_id IN ({visitors}) GROUP BY currency ORDER BY s DESC""",
            V,
        )
        stats["volume"] = [_money(r["s"], r["currency"]) for r in vol]

        events = []
        for r in fetchall(f"SELECT full_name, created_at FROM users WHERE id IN ({visitors}) ORDER BY created_at DESC LIMIT 15", V):
            events.append({"t": r["created_at"], "kind": "join", "text": f"{_short(r['full_name'])} joined SOHANA"})
        for r in fetchall(
            f"""SELECT s.full_name AS a, r.full_name AS b, p.amount_cents, p.currency, COALESCE(p.consumed_at, p.created_at) AS t
                FROM payment_intents p JOIN users s ON s.id=p.sender_id JOIN users r ON r.id=p.recipient_id
                WHERE p.status='completed' AND p.sender_id IN ({visitors}) ORDER BY t DESC LIMIT 15""",
            V,
        ):
            events.append({"t": r["t"], "kind": "send",
                           "text": f"{_short(r['a'])} sent {_money(r['amount_cents'], r['currency'])} to {_short(r['b'])}"})
        for r in fetchall(
            f"""SELECT u.full_name, ro.name, ro.created_at AS t FROM roscas ro JOIN users u ON u.id=ro.organiser_id
                WHERE ro.organiser_id IN ({visitors}) ORDER BY t DESC LIMIT 10""",
            V,
        ):
            events.append({"t": r["t"], "kind": "circle", "text": f"{_short(r['full_name'])} started the circle “{r['name']}”"})
        for r in fetchall(
            f"""SELECT u.full_name, ro.name, c.paid_at AS t FROM contributions c JOIN users u ON u.id=c.user_id
                JOIN roscas ro ON ro.id=c.rosca_id
                WHERE c.status IN ('paid','late') AND c.paid_at IS NOT NULL AND c.user_id IN ({visitors})
                ORDER BY t DESC LIMIT 15""",
            V,
        ):
            events.append({"t": r["t"], "kind": "contribution", "text": f"{_short(r['full_name'])} paid into “{r['name']}”"})
        try:
            for r in fetchall(
                f"""SELECT d.donor_name, d.is_anonymous, d.amount_cents, ca.currency, ca.title, d.created_at AS t
                    FROM campaign_donations d JOIN campaigns ca ON ca.id=d.campaign_id
                    WHERE d.donor_id IN ({visitors}) ORDER BY t DESC LIMIT 10""",
                V,
            ):
                who = "Someone" if r["is_anonymous"] else _short(r["donor_name"])
                events.append({"t": r["t"], "kind": "donation",
                               "text": f"{who} backed “{r['title']}” with {_money(r['amount_cents'], r['currency'] or 'EUR')}"})
        except Exception:
            app.logger.exception("wall donations")
        events = [e for e in events if e["t"]]
        events.sort(key=lambda e: e["t"], reverse=True)
        return jsonify({"stats": stats, "events": events[:24],
                        "now": fetchone("SELECT datetime('now') AS n")["n"]})

    # ---------------- control room ----------------

    def _demo_users(limit=500):
        rows = fetchall(
            """SELECT u.id, u.full_name, u.email, u.hanatag, u.ncs_score, u.ncs_tier, u.created_at,
                      COALESCE(u.marketing_consent,0) AS consent, u.language, u.phone,
                      (SELECT COUNT(*) FROM rosca_members rm WHERE rm.user_id=u.id AND rm.status='active') AS circles
               FROM users u WHERE COALESCE(u.is_admin,0)=0 AND u.phone NOT IN ({seed})
               ORDER BY u.created_at DESC LIMIT ?""".format(seed=",".join("?" * len(SEED_PHONES))),
            (*SEED_PHONES, limit),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["balances"] = _balances(d["id"])
            d.pop("phone", None)
            out.append(d)
        return out

    @app.route("/admin/demo")
    @admin_required
    def admin_demo():
        return render_template("demo_control.html", user=auth.get_current_user())

    @app.route("/admin/demo/feed")
    @admin_required
    def admin_demo_feed():
        users = _demo_users()
        today = sum(1 for u in users if (u.get("created_at") or "")[:10] == fetchone("SELECT date('now') AS d")["d"])
        tx = fetchone(
            "SELECT COUNT(*) AS c FROM payment_intents WHERE status='completed' AND date(created_at)=date('now')"
        )
        return jsonify(
            {
                "users": users,
                "stats": {
                    "total": len(users),
                    "today": today,
                    "emails": sum(1 for u in users if u.get("email")),
                    "consented": sum(1 for u in users if u.get("consent")),
                    "transfers_today": tx["c"] if tx else 0,
                },
            }
        )

    @app.route("/admin/demo/credit", methods=["POST"])
    @admin_required
    def admin_demo_credit():
        d = request.get_json(silent=True) or {}
        uid = d.get("user_id")
        cur = (d.get("currency") or "EUR").upper()
        try:
            cents = int(round(float(d.get("amount") or 0) * 100))
        except (TypeError, ValueError):
            cents = 0
        if not uid or cents <= 0 or cents > 100000000:
            return jsonify({"error": "Enter a valid amount"}), 400
        w = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (uid, cur))
        if not w:
            with get_db() as db:
                db.execute(
                    "INSERT INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,0)",
                    (str(uuid.uuid4()), uid, cur),
                )
            w = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (uid, cur))
        post_transaction(w["id"], cents, "Demo credit from SOHANA", tx_type="deposit")
        push_notification(uid, "You received demo funds", f"{cur} {cents/100:,.2f} added by the SOHANA team.", "success", "/wallet")
        return jsonify({"ok": True})

    @app.route("/admin/demo/export.csv")
    @admin_required
    def admin_demo_export():
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Signed up", "Name", "Email", "Marketing consent", "Language", "Hanatag", "NCS", "Circles"])
        for u in _demo_users(100000):
            w.writerow(
                [
                    (u.get("created_at") or "")[:16], u.get("full_name") or "", u.get("email") or "",
                    "yes" if u.get("consent") else "no", u.get("language") or "", u.get("hanatag") or "",
                    u.get("ncs_score") or "", u.get("circles") or 0,
                ]
            )
        return Response(
            out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=sohana-afts-demo-leads.csv"},
        )
