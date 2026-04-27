import os, uuid, json, io, csv, random
from flask import (Flask, render_template, request, session, jsonify,
                   redirect, url_for, Response)
from database import (init_db, fetchone, fetchall, get_db, wallet_balance,
                      post_transaction, push_notification, calc_withdrawal_fee,
                      get_user_wallets, get_default_wallet, convert_currency,
                      ROSCA_CREATION_FEES, WITHDRAWAL_FEES, CURRENCIES,
                      EXCHANGE_RATES, CONVERSION_FEE_RATE, ADMIN_ROLES,
                      LIMITS, get_period_total, generate_hanatag)
import auth, rosca, ncs_engine

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sohana-dev-secret-change-in-prod")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

@app.before_request
def ensure_db():
    if not hasattr(app, "_db_ready"):
        init_db()
        _seed_all()
        app._db_ready = True

# ── HELPERS ──────────────────────────────────────────────────────────────────

def _get_wallet(user_id, currency=None):
    if currency:
        return fetchone("SELECT * FROM wallets WHERE user_id=? AND currency=?", (user_id, currency))
    return get_default_wallet(user_id)

def admin_required(f):
    """Simple admin guard. Use as @admin_required on any admin route."""
    from functools import wraps
    @wraps(f)
    def _admin_guard(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("admin_login_page"))
        u = fetchone("SELECT is_admin FROM users WHERE id=?", (session["user_id"],))
        if not u or not u["is_admin"]:
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return _admin_guard

# Alias for backwards compatibility
any_admin_required = admin_required

# ── PUBLIC PAGES ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Logged-in users go straight to dashboard; everyone else sees the landing page
    if "user_id" in session:
        check = fetchone("SELECT id FROM users WHERE id=?", (session["user_id"],))
        if check:
            return redirect(url_for("dashboard"))
    return render_template("landing_new.html")

@app.route("/auth")
def auth_page():
    return redirect(url_for("dashboard")) if "user_id" in session else render_template("auth.html")

@app.route("/blog")
def blog_page():
    posts = fetchall("SELECT * FROM blog_posts WHERE is_published=1 ORDER BY published_at DESC LIMIT 20")
    user  = auth.get_current_user() if "user_id" in session else None
    return render_template("blog.html", posts=posts, user=user)

@app.route("/blog/<slug>")
def blog_post(slug):
    post = fetchone("SELECT * FROM blog_posts WHERE slug=? AND is_published=1", (slug,))
    if not post: return redirect(url_for("blog_page"))
    user = auth.get_current_user() if "user_id" in session else None
    return render_template("blog_post.html", post=post, user=user)

# ── USER PAGES ───────────────────────────────────────────────────────────────

@app.route("/dashboard")
@auth.login_required
def dashboard():
    user = auth.get_current_user()
    wallets   = get_user_wallets(user["id"])
    def_wallet= next((w for w in wallets if w["is_default"]), wallets[0] if wallets else None)
    balance   = def_wallet["balance"] if def_wallet else 0
    recent_tx = fetchall("SELECT * FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC LIMIT 5",
                         (def_wallet["id"],)) if def_wallet else []
    my_roscas  = rosca.get_user_roscas(user["id"])
    badges     = fetchall("SELECT * FROM badges WHERE user_id=? ORDER BY earned_at DESC LIMIT 4", (user["id"],))
    tier       = ncs_engine.get_tier(user["ncs_score"])
    marketplace= rosca.get_marketplace(limit=3)
    unread     = fetchone("SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0", (user["id"],))["c"]
    session["user_name"] = user["full_name"]
    session["is_admin"]  = bool(user["is_admin"])
    return render_template("dashboard.html", user=user, balance=balance, wallets=wallets,
                           def_wallet=def_wallet, recent_tx=recent_tx, my_roscas=my_roscas,
                           badges=badges, tier=tier, marketplace=marketplace, unread=unread,
                           currencies=CURRENCIES)

@app.route("/wallet")
@auth.login_required
def wallet_page():
    user    = auth.get_current_user()
    wallets = get_user_wallets(user["id"])
    def_wallet = next((w for w in wallets if w["is_default"]), wallets[0] if wallets else None)
    balance    = def_wallet["balance"] if def_wallet else 0
    active_cur = request.args.get("currency", def_wallet["currency"] if def_wallet else "EUR")
    active_wallet = next((w for w in wallets if w["currency"] == active_cur), def_wallet)
    all_tx = fetchall("SELECT * FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC LIMIT 100",
                      (active_wallet["id"],)) if active_wallet else []
    tier = ncs_engine.get_tier(user["ncs_score"])
    open_currencies = {w["currency"] for w in wallets}
    available_to_open = {k: v for k, v in CURRENCIES.items() if k not in open_currencies}
    return render_template("wallet.html", user=user, balance=balance, wallets=wallets,
                           active_wallet=active_wallet, transactions=all_tx, tier=tier,
                           currencies=CURRENCIES, exchange_rates=EXCHANGE_RATES,
                           available_to_open=available_to_open,
                           conversion_fee_pct=CONVERSION_FEE_RATE*100,
                           withdrawal_fees=WITHDRAWAL_FEES)

@app.route("/history")
@auth.login_required
def history_page():
    user = auth.get_current_user()
    contribs = fetchall("""SELECT c.*, cy.cycle_number, r.name as rosca_name
                           FROM contributions c JOIN cycles cy ON cy.id=c.cycle_id
                           JOIN roscas r ON r.id=c.rosca_id
                           WHERE c.user_id=? ORDER BY c.created_at DESC LIMIT 100""", (user["id"],))
    tier = ncs_engine.get_tier(user["ncs_score"])
    return render_template("history.html", user=user, contribs=contribs, tier=tier)

@app.route("/profile")
@app.route("/profile/<user_id>")
@auth.login_required
def profile_page(user_id=None):
    me = auth.get_current_user()
    viewing_self = (user_id is None or user_id == me["id"])
    profile_user = me if viewing_self else fetchone(
        "SELECT * FROM users WHERE id=?", (user_id,))
    if not profile_user: return redirect(url_for("dashboard"))
    badges       = fetchall("SELECT * FROM badges WHERE user_id=? ORDER BY earned_at DESC", (profile_user["id"],))
    endorsements = fetchone("SELECT COUNT(*) as c FROM endorsements WHERE to_id=?", (profile_user["id"],))["c"]
    roscas_done  = fetchone("SELECT COUNT(*) as c FROM rosca_members rm JOIN roscas r ON r.id=rm.rosca_id WHERE rm.user_id=? AND r.status='completed'", (profile_user["id"],))["c"]
    tier         = ncs_engine.get_tier(profile_user["ncs_score"])
    pay_methods  = fetchall("SELECT * FROM payment_methods WHERE user_id=? ORDER BY is_default DESC, created_at", (me["id"],)) if viewing_self else []
    all_badges   = ncs_engine.BADGE_DEFINITIONS
    total_saved  = fetchone("SELECT COALESCE(SUM(amount_cents),0) as s FROM contributions WHERE user_id=? AND status IN ('paid','late')", (profile_user["id"],))["s"]
    wallets      = get_user_wallets(me["id"]) if viewing_self else []
    return render_template("profile.html", user=me, profile_user=dict(profile_user),
                           badges=badges, endorsements=endorsements, roscas_done=roscas_done,
                           tier=tier, pay_methods=pay_methods, viewing_self=viewing_self,
                           all_badges=all_badges, total_saved=total_saved, wallets=wallets,
                           currencies=CURRENCIES)

@app.route("/notifications")
@auth.login_required
def notifications_page():
    user  = auth.get_current_user()
    notifs= fetchall("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (user["id"],))
    with get_db() as db:
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    return render_template("notifications.html", user=user, notifications=notifs)

@app.route("/circles")
@auth.login_required
def circles_page():
    user = auth.get_current_user()
    search = request.args.get("q", "")
    market = rosca.get_marketplace(search=search or None)
    my     = rosca.get_user_roscas(user["id"])
    tier   = ncs_engine.get_tier(user["ncs_score"])
    creation_fee = ROSCA_CREATION_FEES.get(user["ncs_tier"], 500)
    return render_template("circles.html", user=user, marketplace=market, my_roscas=my,
                           search=search, tier=tier, creation_fee=creation_fee)

@app.route("/circles/<rosca_id>")
@auth.login_required
def circle_detail(rosca_id):
    user = auth.get_current_user()
    r    = rosca.get_rosca(rosca_id)
    if not r: return redirect(url_for("circles_page"))
    members    = rosca.get_rosca_members(rosca_id)
    cycle_info = rosca.get_cycle_status(rosca_id)
    is_member  = any(m["user_id"] == user["id"] for m in members)
    is_organiser = r["organiser_id"] == user["id"]
    my_contrib = None
    if cycle_info:
        for c in cycle_info["contributions"]:
            if c["user_id"] == user["id"]: my_contrib = c
    leaderboard = ncs_engine.get_leaderboard(rosca_id)
    my_endorsements = {e["to_id"] for e in fetchall("SELECT to_id FROM endorsements WHERE from_id=? AND rosca_id=?", (user["id"], rosca_id))}
    return render_template("circle_detail.html", user=user, rosca=r, members=members,
                           cycle_info=cycle_info, is_member=is_member, is_organiser=is_organiser,
                           my_contrib=my_contrib, leaderboard=leaderboard, my_endorsements=my_endorsements)

@app.route("/ncs")
@auth.login_required
def ncs_page():
    user       = auth.get_current_user()
    history    = ncs_engine.get_score_history(user["id"])
    components = ncs_engine.get_component_breakdown(user["id"])
    tier       = ncs_engine.get_tier(user["ncs_score"])
    badges     = fetchall("SELECT * FROM badges WHERE user_id=? ORDER BY earned_at DESC", (user["id"],))
    events     = fetchall("SELECT * FROM ncs_events WHERE user_id=? ORDER BY recorded_at DESC LIMIT 20", (user["id"],))
    loan_eligibility = {lt: ncs_engine.check_loan_eligibility(user["id"], lt) for lt in ["emergency","early_payout","rosca_backed"]}
    return render_template("ncs.html", user=user, history=history, components=components,
                           tier=tier, badges=badges, events=events,
                           loan_eligibility=loan_eligibility, all_badges=ncs_engine.BADGE_DEFINITIONS)

@app.route("/organiser/<rosca_id>")
@auth.login_required
def organiser_dashboard(rosca_id):
    user = auth.get_current_user()
    r    = rosca.get_rosca(rosca_id)
    if not r or r["organiser_id"] != user["id"]: return redirect(url_for("circles_page"))
    members    = rosca.get_rosca_members(rosca_id)
    pending    = rosca.get_pending_members(rosca_id)
    cycle_info = rosca.get_cycle_status(rosca_id)
    all_cycles = fetchall("SELECT * FROM cycles WHERE rosca_id=? ORDER BY cycle_number", (rosca_id,))
    all_contribs = fetchall("""SELECT c.*, u.full_name FROM contributions c
                               JOIN users u ON u.id=c.user_id
                               WHERE c.rosca_id=? ORDER BY c.created_at DESC""", (rosca_id,))
    report     = rosca.get_circle_report(rosca_id)
    return render_template("organiser.html", user=user, rosca=dict(r),
                           members=members, pending=pending,
                           cycle_info=cycle_info, all_cycles=all_cycles,
                           all_contribs=all_contribs, report=report)

# ── ADMIN SIGN-IN ─────────────────────────────────────────────────────────────

@app.route("/admin/login")
def admin_login_page():
    if "user_id" in session:
        u = fetchone("SELECT is_admin FROM users WHERE id=?", (session["user_id"],))
        if u and u["is_admin"]: return redirect(url_for("admin_home"))
    return render_template("admin_login.html")

@app.route("/admin/home")
@any_admin_required
def admin_home():
    u = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    role = u["admin_role"] if u else "operations"
    routes = {
        "ceo":        "admin_executive",
        "cto":        "admin_engineering",
        "cco":        "admin_compliance",
        "operations": "admin_operations",
        "compliance": "admin_compliance",
        "fraud":      "admin_fraud",
        "credit":     "admin_credit",
        "business":   "admin_dashboard",
    }
    return redirect(url_for(routes.get(role, "admin_dashboard")))

# ── ADMIN DASHBOARDS ──────────────────────────────────────────────────────────

def _admin_stats():
    """Shared stats used across dashboards."""
    return {
        "total_users":      fetchone("SELECT COUNT(*) as c FROM users WHERE is_admin=0")["c"],
        "total_roscas":     fetchone("SELECT COUNT(*) as c FROM roscas")["c"],
        "active_roscas":    fetchone("SELECT COUNT(*) as c FROM roscas WHERE status='active'")["c"],
        "active_members":   fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE status='active'")["c"],
        "total_tx":         fetchone("SELECT COUNT(*) as c FROM wallet_transactions")["c"],
        "total_volume":     fetchone("SELECT COALESCE(SUM(ABS(amount_cents)),0) as c FROM wallet_transactions WHERE amount_cents>0")["c"],
        "pending_deposits": fetchone("SELECT COUNT(*) as c FROM wallet_transactions WHERE tx_type='deposit' AND created_at>=datetime('now','-1 day')")["c"],
        "pending_withdrawals": fetchone("SELECT COUNT(*) as c FROM wallet_transactions WHERE tx_type='withdrawal' AND created_at>=datetime('now','-1 day')")["c"],
        "late_contributions": fetchone("SELECT COUNT(*) as c FROM contributions WHERE status='late'")["c"],
        "missed_contributions": fetchone("SELECT COUNT(*) as c FROM contributions WHERE status='missed'")["c"],
        "avg_ncs":          fetchone("SELECT COALESCE(AVG(ncs_score),300) as c FROM users WHERE is_admin=0")["c"],
        "loans_disbursed":  fetchone("SELECT COUNT(*) as c FROM wallet_transactions WHERE tx_type='rosca_payout'")["c"],
        "fraud_alerts":     fetchone("SELECT COUNT(*) as c FROM fraud_alerts WHERE status='open'")["c"] if _table_exists("fraud_alerts") else 0,
        "total_revenue":    fetchone("SELECT COALESCE(SUM(amount_cents),0) as c FROM wallet_transactions WHERE tx_type='fee'")["c"],
        "escrow":           fetchone("SELECT COALESCE(SUM(pot_cents),0) as c FROM cycles WHERE status='collecting'")["c"],
        "contributed_week": fetchone("SELECT COALESCE(SUM(amount_cents),0) as c FROM contributions WHERE status IN ('paid','late') AND created_at>=datetime('now','-7 days')")["c"],
        "new_users_week":   fetchone("SELECT COUNT(*) as c FROM users WHERE created_at>=datetime('now','-7 days')")["c"],
        "fraud_prevented":  25000000,
        "total_earnings":   fetchone("SELECT COALESCE(SUM(ABS(amount_cents)),0) as c FROM wallet_transactions WHERE amount_cents>0 AND tx_type='rosca_payout'")["c"],
        "platform_earnings":fetchone("SELECT COUNT(*) as c FROM wallet_transactions WHERE tx_type='fee'")["c"],
        "late_members":     fetchone("SELECT COUNT(DISTINCT user_id) as c FROM contributions WHERE status='late'")["c"],
    }

def _table_exists(name):
    r = fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(r)

@app.route("/admin")
@app.route("/admin/dashboard")
@any_admin_required
def admin_dashboard():
    user = auth.get_current_user()
    stats = _admin_stats()
    all_roscas = fetchall("""SELECT r.*, u.full_name as organiser_name, COUNT(rm.id) as member_count
                             FROM roscas r JOIN users u ON u.id=r.organiser_id
                             LEFT JOIN rosca_members rm ON rm.rosca_id=r.id AND rm.status='active'
                             GROUP BY r.id ORDER BY r.created_at DESC LIMIT 20""")
    at_risk = fetchall("""SELECT u.full_name, COUNT(*) as missed, r.name as rosca_name
                          FROM contributions c JOIN users u ON u.id=c.user_id JOIN roscas r ON r.id=c.rosca_id
                          WHERE c.status='missed' GROUP BY c.user_id ORDER BY missed DESC LIMIT 5""")
    organiser_alerts = fetchall("""SELECT u.full_name, r.name as rosca_name, COUNT(*) as issue_count
                                   FROM contributions c JOIN roscas r ON r.id=c.rosca_id
                                   JOIN users u ON u.id=r.organiser_id
                                   WHERE c.status IN ('missed','late') GROUP BY r.organiser_id
                                   ORDER BY issue_count DESC LIMIT 3""")
    return render_template("admin_dashboard.html", user=user, stats=stats,
                           all_roscas=all_roscas, at_risk=at_risk,
                           organiser_alerts=organiser_alerts, admin_roles=ADMIN_ROLES)

@app.route("/admin/executive")
@admin_required
def admin_executive():
    user  = auth.get_current_user()
    stats = _admin_stats()
    recent_users = fetchall("SELECT * FROM users WHERE is_admin=0 ORDER BY created_at DESC LIMIT 10")
    recent_tx    = fetchall("SELECT wt.*, u.full_name FROM wallet_transactions wt JOIN wallets w ON w.id=wt.wallet_id JOIN users u ON u.id=w.user_id ORDER BY wt.created_at DESC LIMIT 10")
    return render_template("admin_executive.html", user=user, stats=stats,
                           recent_users=recent_users, recent_tx=recent_tx, admin_roles=ADMIN_ROLES)

@app.route("/admin/operations")
@admin_required
def admin_operations():
    user  = auth.get_current_user()
    stats = _admin_stats()
    payments = fetchall("""SELECT c.*, u.full_name as member_name, r.name as rosca_name
                           FROM contributions c JOIN users u ON u.id=c.user_id JOIN roscas r ON r.id=c.rosca_id
                           ORDER BY c.created_at DESC LIMIT 30""")
    all_roscas = fetchall("""SELECT r.*, u.full_name as organiser_name, COUNT(rm.id) as member_count
                             FROM roscas r JOIN users u ON u.id=r.organiser_id
                             LEFT JOIN rosca_members rm ON rm.rosca_id=r.id AND rm.status='active'
                             GROUP BY r.id ORDER BY r.created_at DESC LIMIT 20""")
    return render_template("admin_operations.html", user=user, stats=stats,
                           payments=payments, all_roscas=all_roscas, admin_roles=ADMIN_ROLES)

@app.route("/admin/compliance")
@admin_required
def admin_compliance():
    user  = auth.get_current_user()
    stats = _admin_stats()
    flagged = fetchall("""SELECT * FROM wallet_transactions WHERE ABS(amount_cents) > 500000
                          ORDER BY created_at DESC LIMIT 20""")
    users = fetchall("SELECT * FROM users WHERE is_admin=0 ORDER BY ncs_score ASC LIMIT 20")
    return render_template("admin_compliance.html", user=user, stats=stats,
                           flagged=flagged, users=users, admin_roles=ADMIN_ROLES)

@app.route("/admin/fraud")
@admin_required
def admin_fraud():
    user  = auth.get_current_user()
    stats = _admin_stats()
    high_risk = fetchall("SELECT * FROM users WHERE ncs_score < 450 AND is_admin=0 ORDER BY ncs_score ASC LIMIT 20")
    large_tx  = fetchall("""SELECT wt.*, u.full_name, w.currency FROM wallet_transactions wt
                            JOIN wallets w ON w.id=wt.wallet_id JOIN users u ON u.id=w.user_id
                            WHERE ABS(wt.amount_cents) > 200000 ORDER BY wt.created_at DESC LIMIT 20""")
    alerts = fetchall("SELECT * FROM fraud_alerts ORDER BY created_at DESC LIMIT 20") if _table_exists("fraud_alerts") else []
    return render_template("admin_fraud.html", user=user, stats=stats,
                           high_risk=high_risk, large_tx=large_tx, alerts=alerts, admin_roles=ADMIN_ROLES)

@app.route("/admin/credit")
@admin_required
def admin_credit():
    user  = auth.get_current_user()
    stats = _admin_stats()
    score_dist = {
        "excellent": fetchone("SELECT COUNT(*) as c FROM users WHERE ncs_score>=750 AND is_admin=0")["c"],
        "good":      fetchone("SELECT COUNT(*) as c FROM users WHERE ncs_score>=650 AND ncs_score<750 AND is_admin=0")["c"],
        "fair":      fetchone("SELECT COUNT(*) as c FROM users WHERE ncs_score>=550 AND ncs_score<650 AND is_admin=0")["c"],
        "poor":      fetchone("SELECT COUNT(*) as c FROM users WHERE ncs_score>=350 AND ncs_score<550 AND is_admin=0")["c"],
        "very_poor": fetchone("SELECT COUNT(*) as c FROM users WHERE ncs_score<350 AND is_admin=0")["c"],
    }
    recent_events = fetchall("""SELECT ne.*, u.full_name FROM ncs_events ne
                                JOIN users u ON u.id=ne.user_id
                                ORDER BY ne.recorded_at DESC LIMIT 20""")
    return render_template("admin_credit.html", user=user, stats=stats,
                           score_dist=score_dist, recent_events=recent_events, admin_roles=ADMIN_ROLES)

@app.route("/admin/engineering")
@admin_required
def admin_engineering():
    user  = auth.get_current_user()
    stats = _admin_stats()
    db_stats = {
        "total_records": fetchone("SELECT COUNT(*) as c FROM wallet_transactions")["c"],
        "total_users":   stats["total_users"],
        "total_wallets": fetchone("SELECT COUNT(*) as c FROM wallets")["c"],
    }
    return render_template("admin_engineering.html", user=user, stats=stats,
                           db_stats=db_stats, admin_roles=ADMIN_ROLES)

@app.route("/admin/payments")
@any_admin_required
def admin_payments():
    user = auth.get_current_user()
    payments = fetchall("""SELECT c.*, u.full_name as member_name, r.name as rosca_name
                           FROM contributions c JOIN users u ON u.id=c.user_id JOIN roscas r ON r.id=c.rosca_id
                           ORDER BY c.created_at DESC LIMIT 50""")
    stats = {
        "total":       fetchone("SELECT COALESCE(SUM(amount_cents),0) as c FROM contributions WHERE status IN ('paid','late')")["c"],
        "pending_cnt": fetchone("SELECT COUNT(*) as c FROM contributions WHERE status='pending'")["c"],
        "pending_amt": fetchone("SELECT COALESCE(SUM(amount_cents),0) as c FROM contributions WHERE status='pending'")["c"],
        "overdue_cnt": fetchone("SELECT COUNT(*) as c FROM contributions WHERE status='missed'")["c"],
        "overdue_amt": fetchone("SELECT COALESCE(SUM(amount_cents),0) as c FROM contributions WHERE status='missed'")["c"],
        "escrow":      fetchone("SELECT COALESCE(SUM(pot_cents),0) as c FROM cycles WHERE status='collecting'")["c"],
    }
    return render_template("admin_payments.html", user=user, payments=payments, stats=stats)

@app.route("/admin/admins")
@admin_required
def admin_admins():
    user = auth.get_current_user()
    admins = fetchall("""SELECT u.*, COUNT(DISTINCT r.id) as managed_roscas FROM users u
                         LEFT JOIN roscas r ON r.organiser_id=u.id WHERE u.is_admin=1 GROUP BY u.id ORDER BY u.created_at""")
    stats = {"total_admins": len([a for a in admins]),
             "active_members": fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE status='active'")["c"],
             "total_roscas":   fetchone("SELECT COUNT(*) as c FROM roscas")["c"]}
    return render_template("admin_admins.html", user=user, admins=admins, stats=stats, admin_roles=ADMIN_ROLES)

@app.route("/admin/users")
@any_admin_required
def admin_users():
    user  = auth.get_current_user()
    users = fetchall("SELECT * FROM users WHERE is_admin=0 ORDER BY created_at DESC LIMIT 100")
    return render_template("admin_users.html", user=user, users=users)

@app.route("/admin/blog")
@any_admin_required
def admin_blog():
    user  = auth.get_current_user()
    posts = fetchall("SELECT * FROM blog_posts ORDER BY created_at DESC")
    return render_template("admin_blog.html", user=user, posts=posts)

# ── AUTH API ──────────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d = request.json or {}
    try:
        uid = auth.register_user(d.get("phone",""), d.get("full_name",""), d.get("password",""),
                                 email=d.get("email"), country=d.get("country","RW"))
        session["user_id"] = uid
        push_notification(uid, "Welcome to SOHANA! 🎉", "Your account is ready. Start by joining a circle.", "success", "/circles")
        return jsonify({"ok": True, "user_id": uid})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.json or {}
    try:
        user = auth.login_user(d.get("phone",""), d.get("password",""))
        session["user_id"] = user["id"]
        session["user_name"] = user["full_name"]
        return jsonify({"ok": True, "user": {"id": user["id"], "name": user["full_name"]}})
    except ValueError as e:
        return jsonify({"error": str(e)}), 401

@app.route("/api/auth/admin-login", methods=["POST"])
def api_admin_login():
    d = request.json or {}
    try:
        user = auth.login_user(d.get("email_or_phone",""), d.get("password",""))
        u = fetchone("SELECT is_admin, admin_role FROM users WHERE id=?", (user["id"],))
        if not u or not u["is_admin"]:
            return jsonify({"error": "No admin access for this account"}), 403
        session["user_id"]  = user["id"]
        session["user_name"] = user["full_name"]
        session["is_admin"] = True
        session["admin_role"] = u["admin_role"]
        return jsonify({"ok": True, "role": u["admin_role"]})
    except ValueError as e:
        return jsonify({"error": str(e)}), 401

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/logout")
def logout():
    """GET logout — clears session and redirects to landing. Used by sidebar Sign out link."""
    session.clear()
    return redirect(url_for("index"))

@app.route("/api/waitlist", methods=["POST"])
def api_waitlist():
    """Capture waitlist signups from the landing page."""
    d = request.json or {}
    email = d.get("email", "").strip().lower()
    name  = d.get("name", "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400
    # Store in waitlist table (created if not exists)
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS waitlist (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("INSERT OR IGNORE INTO waitlist(id, email, name) VALUES(?,?,?)",
                       (str(uuid.uuid4()), email, name))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": True})  # Always return OK - don't expose DB errors

@app.route("/admin/waitlist")
@admin_required
def admin_waitlist():
    """Admin view of waitlist signups."""
    user = auth.get_current_user()
    try:
        signups = fetchall("SELECT email, name, created_at FROM waitlist ORDER BY created_at DESC")
    except Exception:
        signups = []
    return render_template("admin_waitlist.html", user=user, signups=signups)

@app.route("/admin/waitlist/export")
@admin_required
def admin_waitlist_export():
    import io, csv
    try:
        signups = fetchall("SELECT email, name, created_at FROM waitlist ORDER BY created_at DESC")
    except Exception:
        signups = []
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Email", "Name", "Signed Up"])
    for s in signups:
        writer.writerow([s["email"], s["name"] or "", s["created_at"][:16]])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sohana-waitlist.csv"})

# ── WALLET API ────────────────────────────────────────────────────────────────

@app.route("/api/wallet/balances")
@auth.login_required
def api_wallet_balances():
    wallets = get_user_wallets(session["user_id"])
    return jsonify({"wallets": wallets})

@app.route("/api/wallet/open-currency", methods=["POST"])
@auth.login_required
def api_open_currency():
    d = request.json or {}
    currency = d.get("currency","").upper()
    if currency not in CURRENCIES:
        return jsonify({"error": "Unsupported currency"}), 400
    existing = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (session["user_id"], currency))
    if existing:
        return jsonify({"error": f"You already have a {currency} balance"}), 400
    with get_db() as db:
        db.execute("INSERT INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,0)",
                   (str(uuid.uuid4()), session["user_id"], currency))
    return jsonify({"ok": True, "currency": currency})

@app.route("/api/wallet/convert", methods=["POST"])
@auth.login_required
def api_convert():
    d = request.json or {}
    from_cur = d.get("from_currency","EUR")
    to_cur   = d.get("to_currency","GBP")
    amount   = int(float(d.get("amount", 0)) * 100)
    otp      = str(d.get("otp",""))
    if len(otp) != 6 or not otp.isdigit():
        return jsonify({"error": "Invalid verification code"}), 400
    if amount <= 0: return jsonify({"error": "Invalid amount"}), 400
    try:
        to_amount, fee = convert_currency(session["user_id"], from_cur, to_cur, amount)
        return jsonify({"ok": True, "to_amount": to_amount, "fee_cents": fee,
                        "to_amount_display": to_amount/100})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/wallet/deposit", methods=["POST"])
@auth.login_required
def api_deposit():
    d = request.json or {}
    cents    = int(float(d.get("amount", 0)) * 100)
    currency = d.get("currency", "EUR")
    if cents <= 0: return jsonify({"error": "Invalid amount"}), 400
    wallet = _get_wallet(session["user_id"], currency)
    if not wallet: return jsonify({"error": f"No {currency} wallet"}), 400
    # ── Daily deposit limit ───────────────────────────────────────────
    lim = LIMITS["standard"]
    deposited_today = get_period_total(wallet["id"], "deposit", "in", "day")
    remaining = lim["deposit_daily_cents"] - deposited_today
    if cents > remaining:
        return jsonify({"error": f"Daily deposit limit reached. You can still deposit "
                                  f"€{remaining/100:,.2f} today (limit €10,000/day).".replace(',', ' ')}), 400
    # ─────────────────────────────────────────────────────────────────
    new_bal = post_transaction(wallet["id"], cents, f"Deposit ({currency})", tx_type="deposit")
    ncs_engine.apply_event(session["user_id"], "wallet_deposit")
    return jsonify({"ok": True, "new_balance_cents": new_bal})

@app.route("/api/wallet/withdraw", methods=["POST"])
@auth.login_required
def api_withdraw():
    d = request.json or {}
    cents    = int(float(d.get("amount", 0)) * 100)
    method   = d.get("method", "bank_eu")
    currency = d.get("currency", "EUR")
    otp      = str(d.get("otp",""))
    if len(otp) != 6 or not otp.isdigit():
        return jsonify({"error": "Invalid verification code"}), 400
    if cents <= 0: return jsonify({"error": "Invalid amount"}), 400
    wallet = _get_wallet(session["user_id"], currency)
    if not wallet: return jsonify({"error": f"No {currency} wallet"}), 400
    # ── Withdrawal limits ─────────────────────────────────────────────
    lim = LIMITS["standard"]
    withdrawn_today   = get_period_total(wallet["id"], "withdrawal", "out", "day")
    withdrawn_month   = get_period_total(wallet["id"], "withdrawal", "out", "month")
    daily_remaining   = lim["withdraw_daily_cents"]   - withdrawn_today
    monthly_remaining = lim["withdraw_monthly_cents"] - withdrawn_month
    if cents > daily_remaining:
        return jsonify({"error": f"Daily withdrawal limit reached. Remaining today: "
                                  f"€{daily_remaining/100:,.2f} (limit €3 000/day).".replace(',', ' ')}), 400
    if cents > monthly_remaining:
        return jsonify({"error": f"Monthly withdrawal limit reached. Remaining this month: "
                                  f"€{monthly_remaining/100:,.2f} (limit €10 000/month).".replace(',', ' ')}), 400
    # ─────────────────────────────────────────────────────────────────
    bal  = wallet_balance(wallet["id"])
    fee  = calc_withdrawal_fee(cents, method)
    total = cents + fee
    if total > bal: return jsonify({"error": f"Insufficient balance"}), 400
    dest = d.get("destination_name", "account")
    post_transaction(wallet["id"], -total, f"Withdrawal → {dest} (fee: {fee/100:.2f})", tx_type="withdrawal")
    push_notification(session["user_id"], "Withdrawal submitted ✓", f"{CURRENCIES.get(currency,{}).get('symbol','')}{cents/100:.2f} is on its way to {dest}.", "info")
    return jsonify({"ok": True, "withdrawn_cents": cents, "fee_cents": fee})

@app.route("/api/wallet/pay", methods=["POST"])
@auth.login_required
def api_pay():
    d       = request.json or {}
    cents   = int(float(d.get("amount", 0)) * 100)
    hanatag = d.get("hanatag","").strip()
    note    = d.get("note","")
    otp     = str(d.get("otp",""))
    currency= d.get("currency","EUR")
    if len(otp) != 6 or not otp.isdigit():
        return jsonify({"error": "Invalid verification code"}), 400
    if cents <= 0: return jsonify({"error": "Invalid amount"}), 400
    if not hanatag.startswith("@"): hanatag = f"@{hanatag}"
    recipient = fetchone("SELECT id, full_name FROM users WHERE hanatag=?", (hanatag,))
    if not recipient: return jsonify({"error": "Hanatag not found"}), 404
    if recipient["id"] == session["user_id"]: return jsonify({"error": "Cannot pay yourself"}), 400
    sw = _get_wallet(session["user_id"], currency)
    rw = _get_wallet(recipient["id"], currency)
    if not sw: return jsonify({"error": f"No {currency} wallet"}), 400
    if not rw:
        # Recipient doesn't have this currency — open it
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,0)",
                       (str(uuid.uuid4()), recipient["id"], currency))
        rw = _get_wallet(recipient["id"], currency)
    bal = wallet_balance(sw["id"])
    # ── Pay fee: 2% on amounts over €5,000 ───────────────────────────
    lim = LIMITS["standard"]
    pay_fee = 0
    if cents > lim["pay_fee_threshold_cents"]:
        pay_fee = int(cents * lim["pay_fee_rate"])
    total_debit = cents + pay_fee
    if total_debit > bal: return jsonify({"error": "Insufficient balance"}), 400
    # ─────────────────────────────────────────────────────────────────
    ref = str(uuid.uuid4())
    sender = fetchone("SELECT full_name, hanatag FROM users WHERE id=?", (session["user_id"],))
    stag = sender["hanatag"] or session.get("user_name","user")
    fee_note = f" (incl. €{pay_fee/100:.2f} fee)" if pay_fee else ""
    post_transaction(sw["id"], -total_debit, f"Pay to {hanatag}" + (f" — {note}" if note else "") + fee_note, tx_type="pay_out", ref_id=ref)
    post_transaction(rw["id"], +cents, f"Pay from {stag}" + (f" — {note}" if note else ""), tx_type="pay_in",  ref_id=ref)
    with get_db() as db:
        db.execute("INSERT INTO hanatag_payments(id,sender_id,recipient_id,amount_cents,currency,note) VALUES(?,?,?,?,?,?)",
                   (ref, session["user_id"], recipient["id"], cents, currency, note))
    sym = CURRENCIES.get(currency,{}).get('symbol','')
    push_notification(recipient["id"], f"You received {sym}{cents/100:,.2f}!".replace(',', ' '),
                      f"From {sender['full_name']}" + (f": {note}" if note else ""), "success", "/wallet")
    return jsonify({"ok": True, "recipient_name": recipient["full_name"], "fee_cents": pay_fee})

@app.route("/api/wallet/statement")
@auth.login_required
def api_statement():
    user     = auth.get_current_user()
    currency = request.args.get("currency","EUR")
    wallet   = _get_wallet(user["id"], currency)
    txs      = fetchall("SELECT * FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC", (wallet["id"],)) if wallet else []
    sym      = CURRENCIES.get(currency,{}).get("symbol","")
    output   = io.StringIO()
    writer   = csv.writer(output)
    writer.writerow(["Date","Description","Type",f"Amount ({currency})","Balance","Reference"])
    for tx in txs:
        writer.writerow([tx["created_at"][:16].replace("T"," "), tx["description"],
                         tx["tx_type"].replace("_"," ").title(),
                         f"{tx['amount_cents']/100:+.2f}", f"{tx['balance_after']/100:.2f}", tx["ref_id"] or ""])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="sohana-{currency}-statement.csv"'})

# ── CURRENCY API ──────────────────────────────────────────────────────────────

@app.route("/api/currency/rates")
def api_rates():
    return jsonify({"rates": EXCHANGE_RATES, "base": "EUR"})

@app.route("/api/currency/preview-conversion")
@auth.login_required
def api_preview_conversion():
    from_cur = request.args.get("from","EUR")
    to_cur   = request.args.get("to","GBP")
    amount   = float(request.args.get("amount",0))
    cents    = int(amount * 100)
    from_rate = EXCHANGE_RATES.get(from_cur,1.0)
    to_rate   = EXCHANGE_RATES.get(to_cur,1.0)
    eur_amount = cents / from_rate
    to_amount  = int(eur_amount * to_rate)
    fee        = max(50, int(cents * CONVERSION_FEE_RATE))
    return jsonify({"from_amount": cents, "to_amount": to_amount,
                    "fee_cents": fee, "rate": to_rate/from_rate,
                    "fee_pct": CONVERSION_FEE_RATE*100})

# ── PROFILE API ───────────────────────────────────────────────────────────────

@app.route("/api/profile/update", methods=["POST"])
@auth.login_required
def api_profile_update():
    d = request.json or {}
    uid = session["user_id"]
    fields, params = [], []
    for f in ["full_name","email","bio","language","base_currency","notif_email","notif_push","notif_sms"]:
        if f in d: fields.append(f"{f}=?"); params.append(d[f])
    if not fields: return jsonify({"error": "No fields"}), 400
    params.append(uid)
    with get_db() as db:
        db.execute(f"UPDATE users SET {','.join(fields)}, updated_at=datetime('now') WHERE id=?", params)
    return jsonify({"ok": True})

@app.route("/api/profile/hanatag", methods=["POST"])
@auth.login_required
def api_set_hanatag():
    d   = request.json or {}
    tag = d.get("hanatag","").strip().lstrip("@").lower()
    if not tag or len(tag) < 3: return jsonify({"error": "Min 3 characters"}), 400
    tag = f"@{tag}"
    existing = fetchone("SELECT id FROM users WHERE hanatag=?", (tag,))
    if existing and existing["id"] != session["user_id"]:
        return jsonify({"error": "Already taken"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET hanatag=? WHERE id=?", (tag, session["user_id"]))
    return jsonify({"ok": True, "hanatag": tag})

@app.route("/api/profile/lookup-hanatag")
@auth.login_required
def api_lookup_hanatag():
    tag = request.args.get("tag","").strip()
    if not tag.startswith("@"): tag = f"@{tag}"
    u = fetchone("SELECT id, full_name, ncs_score, ncs_tier FROM users WHERE hanatag=?", (tag,))
    if not u: return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True, "user": dict(u)})

@app.route("/api/profile/payment-method", methods=["POST"])
@auth.login_required
def api_add_payment_method():
    d = request.json or {}
    with get_db() as db:
        if d.get("is_default"):
            db.execute("UPDATE payment_methods SET is_default=0 WHERE user_id=?", (session["user_id"],))
        db.execute("INSERT INTO payment_methods(id,user_id,method_type,label,details,is_default) VALUES(?,?,?,?,?,?)",
                   (str(uuid.uuid4()), session["user_id"], d.get("method_type","bank"),
                    d.get("label",""), json.dumps(d.get("details",{})), 1 if d.get("is_default") else 0))
    return jsonify({"ok": True})

# ── NOTIFICATIONS API ─────────────────────────────────────────────────────────

@app.route("/api/notifications")
@auth.login_required
def api_notifications():
    notifs = fetchall("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (session["user_id"],))
    unread = fetchone("SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0", (session["user_id"],))["c"]
    return jsonify({"notifications": [dict(n) for n in notifs], "unread": unread})

@app.route("/api/notifications/mark-read", methods=["POST"])
@auth.login_required
def api_mark_read():
    with get_db() as db:
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (session["user_id"],))
    return jsonify({"ok": True})

# ── ROSCA API ─────────────────────────────────────────────────────────────────

@app.route("/api/rosca/create", methods=["POST"])
@auth.login_required
def api_create_rosca():
    d = request.json or {}
    try:
        rid, fee = rosca.create_rosca(
            organiser_id=session["user_id"], name=d.get("name","").strip(),
            description=d.get("description",""),
            contribution_cents=int(float(d.get("contribution",50))*100),
            max_members=int(d.get("max_members",8)),
            frequency_days=int(d.get("frequency_days",30)),
            ncs_min=int(d.get("ncs_min",300)), is_public=bool(d.get("is_public",True)))
        if fee > 0:
            wallet = get_default_wallet(session["user_id"])
            if wallet and wallet_balance(wallet["id"]) >= fee:
                post_transaction(wallet["id"], -fee, f"ROSCA creation fee: {d.get('name','')}", tx_type="fee")
        return jsonify({"ok": True, "rosca_id": rid, "creation_fee_cents": fee})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/join", methods=["POST"])
@auth.login_required
def api_join_rosca(rosca_id):
    """Request to join — creates pending membership for organiser approval."""
    try:
        rosca.request_to_join(rosca_id, session["user_id"])
        return jsonify({"ok": True, "status": "pending"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/pending")
@auth.login_required
def api_pending_members(rosca_id):
    """Get list of pending join requests (organiser only)."""
    r = rosca.get_rosca(rosca_id)
    if not r or r["organiser_id"] != session["user_id"]:
        return jsonify({"error": "Unauthorised"}), 403
    pending = rosca.get_pending_members(rosca_id)
    return jsonify({"pending": [dict(p) for p in pending]})

@app.route("/api/rosca/<rosca_id>/approve/<user_id>", methods=["POST"])
@auth.login_required
def api_approve_member(rosca_id, user_id):
    try:
        rosca.approve_member(rosca_id, user_id, session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/reject/<user_id>", methods=["POST"])
@auth.login_required
def api_reject_member(rosca_id, user_id):
    try:
        rosca.reject_member(rosca_id, user_id, session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/remove/<user_id>", methods=["POST"])
@auth.login_required
def api_remove_member(rosca_id, user_id):
    d = request.json or {}
    try:
        rosca.remove_member(rosca_id, user_id, session["user_id"], reason=d.get("reason",""))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/add-member", methods=["POST"])
@auth.login_required
def api_add_member_direct(rosca_id):
    """Organiser directly adds a member by hanatag or phone."""
    d = request.json or {}
    identifier = d.get("identifier","").strip()
    # Look up by hanatag or phone
    user = None
    if identifier.startswith("@"):
        user = fetchone("SELECT id FROM users WHERE hanatag=?", (identifier,))
    else:
        user = fetchone("SELECT id FROM users WHERE phone=? OR email=?", (identifier, identifier))
    if not user:
        return jsonify({"error": "User not found. Check hanatag or phone number."}), 404
    try:
        rosca.add_member_direct(rosca_id, user["id"], session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/report")
@auth.login_required
def api_rosca_report(rosca_id):
    """Full circle performance report — organiser only."""
    r = rosca.get_rosca(rosca_id)
    if not r or r["organiser_id"] != session["user_id"]:
        return jsonify({"error": "Unauthorised"}), 403
    report = rosca.get_circle_report(rosca_id)
    return jsonify({"report": report})

@app.route("/api/rosca/<rosca_id>/report/csv")
@auth.login_required
def api_rosca_report_csv(rosca_id):
    """Download circle report as CSV."""
    import io, csv
    r = rosca.get_rosca(rosca_id)
    if not r or r["organiser_id"] != session["user_id"]:
        return jsonify({"error": "Unauthorised"}), 403
    report = rosca.get_circle_report(rosca_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Rank","Member","NCS Score","Tier","On-Time","Late","Missed","On-Time Rate","Total Contributed","Payout Received"])
    for m in report["member_stats"]:
        writer.writerow([
            m["rank"], m["full_name"], m["ncs_score"], m["ncs_tier"].title(),
            m["paid_on_time"], m["paid_late"], m["missed"],
            f"{m['on_time_rate']}%",
            f"€{m['total_paid_cents']/100:.2f}",
            f"€{m['payout_received_cents']/100:.2f}" if m["payout_received_cents"] else "Pending"
        ])
    output.seek(0)
    circle_name = r["name"].replace(" ","-").lower()
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=sohana-report-{circle_name}.csv"})

@app.route("/api/rosca/<rosca_id>/contribute", methods=["POST"])
@auth.login_required
def api_contribute(rosca_id):
    try:
        cycle = rosca.get_or_create_active_cycle(rosca_id)
        rosca.pay_contribution(session["user_id"], cycle["id"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/<rosca_id>/activate", methods=["POST"])
@auth.login_required
def api_activate_rosca(rosca_id):
    r = rosca.get_rosca(rosca_id)
    if not r or r["organiser_id"] != session["user_id"]: return jsonify({"error": "Unauthorized"}), 403
    with get_db() as db:
        db.execute("UPDATE roscas SET status='active' WHERE id=?", (rosca_id,))
    return jsonify({"ok": True})

@app.route("/api/rosca/<rosca_id>/start-cycle", methods=["POST"])
@auth.login_required
def api_start_cycle(rosca_id):
    r = rosca.get_rosca(rosca_id)
    if not r or r["organiser_id"] != session["user_id"]: return jsonify({"error": "Unauthorized"}), 403
    try:
        cycle = rosca.get_or_create_active_cycle(rosca_id)
        return jsonify({"ok": True, "cycle_id": cycle["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rosca/marketplace")
def api_marketplace():
    items = rosca.get_marketplace(search=request.args.get("q"))
    return jsonify({"roscas": [dict(r) for r in items]})

# ── NCS API ───────────────────────────────────────────────────────────────────

@app.route("/api/ncs/score")
@auth.login_required
def api_ncs_score():
    user = auth.get_current_user()
    tier = ncs_engine.get_tier(user["ncs_score"])
    return jsonify({"score": user["ncs_score"], "tier": tier["name"], "tier_label": tier["label"]})

@app.route("/api/ncs/recalculate", methods=["POST"])
@auth.login_required
def api_ncs_recalculate():
    score, components = ncs_engine.recalculate(session["user_id"])
    return jsonify({"score": score, "components": components})

# ── ENDORSEMENT API ───────────────────────────────────────────────────────────

@app.route("/api/endorsement", methods=["POST"])
@auth.login_required
def api_endorse():
    d = request.json or {}
    to_id    = d.get("user_id")
    rosca_id = d.get("rosca_id")
    action   = d.get("action","endorse")
    if not to_id or to_id == session["user_id"]: return jsonify({"error": "Invalid"}), 400
    if action == "unendorse":
        with get_db() as db:
            db.execute("DELETE FROM endorsements WHERE from_id=? AND to_id=? AND rosca_id IS ?",
                       (session["user_id"], to_id, rosca_id))
        ncs_engine.apply_event(to_id, "peer_endorsement_removed", ref_type="endorsement")
        return jsonify({"ok": True, "action": "removed"})
    existing = fetchone("SELECT id FROM endorsements WHERE from_id=? AND to_id=? AND rosca_id IS ?",
                        (session["user_id"], to_id, rosca_id))
    if existing: return jsonify({"error": "Already endorsed"}), 400
    with get_db() as db:
        db.execute("INSERT INTO endorsements(id,from_id,to_id,rosca_id) VALUES(?,?,?,?)",
                   (str(uuid.uuid4()), session["user_id"], to_id, rosca_id))
    ncs_engine.apply_event(to_id, "peer_endorsement", ref_type="endorsement")
    return jsonify({"ok": True, "action": "endorsed"})

# ── ADMIN API ─────────────────────────────────────────────────────────────────

@app.route("/api/admin/invite", methods=["POST"])
@any_admin_required
def api_admin_invite():
    email = (request.json or {}).get("email","").strip()
    if not email: return jsonify({"error": "Email required"}), 400
    return jsonify({"ok": True, "message": f"Invite sent to {email}"})

@app.route("/api/admin/blog", methods=["POST"])
@any_admin_required
def api_create_blog():
    d = request.json or {}
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", d.get("title","").lower().strip())[:60]
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO blog_posts(id,title,slug,excerpt,body,category,author_id) VALUES(?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()), d.get("title"), slug, d.get("excerpt",""), d.get("body",""), d.get("category","news"), session["user_id"]))
    return jsonify({"ok": True})

# ── SEED DATA ─────────────────────────────────────────────────────────────────

def _seed_all():
    if fetchone("SELECT id FROM users WHERE phone='+33611000001'"): return

    # Regular users
    regular_users = [
        ("+33611000001","Maria Ngono",   "demo123","FR",480,0,None),
        ("+33611000002","Samuel Eto",    "demo123","CM",680,0,None),
        ("+25078100001","Alice Uwase",   "demo123","RW",750,0,None),
        ("+44795000001","Kwame Asante",  "demo123","GB",560,0,None),
        ("+33611000003","Fatou Diallo",  "demo123","FR",390,0,None),
    ]
    # Admin users — 7 roles + CEO super admin
    admin_users = [
        ("+00000000001","Kwame Mensah",    "Admin@2024","CM",800,1,"ceo"),
        ("+00000000002","Kojo Agyeman",    "Admin@2024","GH",800,1,"cto"),
        ("+00000000003","Akosua Mensah",   "Admin@2024","GH",800,1,"cco"),
        ("+00000000004","Daniel Owusu",    "Admin@2024","GH",800,1,"fraud"),
        ("+00000000005","Philip Mensah",   "Admin@2024","GH",800,1,"credit"),
        ("+00000000006","Samuel Mensah",   "Admin@2024","GH",800,1,"operations"),
        ("+00000000007","Ama Boateng",     "Admin@2024","GH",800,1,"compliance"),
        ("+00000000008","Emmanuel Asante", "Admin@2024","GH",800,1,"business"),
    ]

    uids = []
    for phone,name,pw,country,score,is_admin,admin_role in regular_users + admin_users:
        uid = str(uuid.uuid4()); wid = str(uuid.uuid4())
        tier = ncs_engine.get_tier(score)["name"]
        hanatag = generate_hanatag(name)
        email = f"{name.lower().replace(' ','.')}@sohana.app" if is_admin else None
        with get_db() as db:
            db.execute("""INSERT OR IGNORE INTO users(id,phone,email,full_name,password_hash,country,
                          ncs_score,ncs_tier,is_admin,admin_role,hanatag) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (uid,phone,email,name,auth.hash_password(pw),country,score,tier,is_admin,admin_role,hanatag))
            real = db.execute("SELECT id FROM users WHERE phone=?",(phone,)).fetchone()
            real_uid = real["id"] if real else uid
            db.execute("INSERT OR IGNORE INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,1)",
                       (wid,real_uid,"EUR"))
            rw = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency='EUR'",(real_uid,)).fetchone()
            real_wid = rw["id"] if rw else wid
            bal = db.execute("SELECT balance_after FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC LIMIT 1",(real_wid,)).fetchone()
            if not bal:
                post_transaction(real_wid, 50000, "Welcome deposit", tx_type="deposit", _db=db)
        if not is_admin:
            uids.append((real_uid, real_wid))

    # Seed demo ROSCA
    if uids:
        rid, _ = rosca.create_rosca(uids[1][0],"Marseille Njangi Circle",
                    "A monthly savings circle for the Marseille Cameroonian community.",
                    5000,max_members=5,frequency_days=30,ncs_min=300,is_public=True)
        for uid,_ in uids[2:]:
            try: rosca.join_rosca(rid, uid)
            except: pass
        with get_db() as db:
            db.execute("UPDATE roscas SET status='active' WHERE id=?", (rid,))

    # Seed blog posts
    admin_uid = fetchone("SELECT id FROM users WHERE admin_role='ceo'")
    admin_id  = admin_uid["id"] if admin_uid else None
    posts = [
        ("ROSCA vs Traditional Banking: The Key Differences","rosca-vs-banking",
         "ROSCAs have served communities for centuries. Here's how they compare.",
         "Rotating Savings and Credit Associations (ROSCAs) are community-based savings groups that predate formal banking by centuries. Unlike banks that charge interest and require credit histories, ROSCAs operate on social trust and mutual accountability. SOHANA brings the best of both worlds by digitising this trusted system while adding credit scoring, dispute resolution, and formal financial products.",
         "education"),
        ("5 Tips to Grow Your NCS Score Fast","improve-ncs-score",
         "Your NCS score unlocks better rates and larger circles. Here's how to build it.",
         "Your Njangi Credit Score (NCS) is the key to unlocking SOHANA's full potential. Pay contributions on time every single cycle — this alone accounts for 35% of your score. Complete full cycles without defaulting. Build peer endorsements by being a reliable circle member. Keep a healthy, regular wallet balance. Each positive action compounds over time.",
         "tips"),
        ("SOHANA Expands to East Africa","sohana-east-africa",
         "SOHANA officially launches operations in Rwanda, Uganda and Kenya.",
         "We are thrilled to announce that SOHANA is now fully operational in East Africa, with regulatory approval from Rwanda's National Bank (BNR). Users in Rwanda, Kenya, and Uganda can now join circles, build their NCS score, and access financial products designed for their communities. Mobile Money integration with MTN and Airtel is now live.",
         "news"),
        ("Understanding the Hanatag System","hanatag-explained",
         "How your @hanatag becomes your financial identity on SOHANA.",
         "Your Hanatag is your unique financial address on SOHANA — think of it like an email address, but for money. With your @hanatag, anyone on the platform can send you money instantly, for free, in any supported currency. No need to share your phone number or bank details. Your hanatag is tied to your NCS score and wallet history, making it a trusted identity layer across the entire SOHANA ecosystem.",
         "education"),
    ]
    with get_db() as db:
        for title,slug,excerpt,body,cat in posts:
            db.execute("INSERT OR IGNORE INTO blog_posts(id,title,slug,excerpt,body,category,author_id) VALUES(?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()),title,slug,excerpt,body,cat,admin_id))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
