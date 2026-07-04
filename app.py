import os, uuid, json, io, csv, random
from datetime import datetime
from flask import (Flask, render_template, request, session, jsonify,
                   redirect, url_for, Response, send_from_directory)
from database import (init_db, fetchone, fetchall, get_db, wallet_balance,
                      post_transaction, push_notification, calc_withdrawal_fee,
                      get_user_wallets, get_default_wallet, convert_currency,
                      ROSCA_CREATION_FEES, WITHDRAWAL_FEES, CURRENCIES,
                      EXCHANGE_RATES, CONVERSION_FEE_RATE, ADMIN_ROLES,
                      LIMITS, get_period_total, generate_hanatag)
import auth, rosca, pool, campaign, ncs_engine
import status as status_module

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sohana-dev-secret-change-in-prod")
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"
app.config["SESSION_COOKIE_SECURE"]    = True   # only send over HTTPS
app.config["SESSION_COOKIE_HTTPONLY"]  = True   # not accessible from JS
app.config["SESSION_COOKIE_NAME"]      = "sohana_session"

# ── RATE LIMITING (auth endpoints only) ───────────────────────────────────────
# Protects against brute-force attacks on login, password reset, and TOTP.
# Wrapped in try/except so app boots even if flask-limiter fails to import
# (e.g. on first deploy before requirements install).
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    def _rate_limit_key():
        """
        Use IP + user_id when logged in, else IP alone.
        Behind Cloudflare/Railway, request.remote_addr sees the proxy — trust the
        X-Forwarded-For header (Railway sets this correctly, Cloudflare grey-cloud
        passes through).
        """
        fwd = request.headers.get("X-Forwarded-For", "")
        ip  = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")
        uid = session.get("user_id", "")
        return f"{ip}:{uid}" if uid else ip

    limiter = Limiter(
        key_func = _rate_limit_key,
        app      = app,
        default_limits = [],  # no global default — apply per-route only
        storage_uri    = "memory://",  # in-process; fine for a single Railway service
        headers_enabled = True,
    )

    def _limit_error_handler(e):
        """Return a clean JSON error when the rate limit is hit."""
        return jsonify({
            "error": "Too many attempts. Please wait a minute and try again.",
            "retry_after_seconds": getattr(e, "retry_after", 60)
        }), 429

    app.register_error_handler(429, _limit_error_handler)
    RATE_LIMITING_ENABLED = True
except Exception as _rl_err:
    import sys
    print(f"[rate-limit] flask-limiter unavailable — running without rate limits: {_rl_err}",
          file=sys.stderr, flush=True)
    RATE_LIMITING_ENABLED = False
    # Provide a no-op decorator so route decorators below still work
    class _NoOpLimiter:
        def limit(self, *a, **kw):
            def _dec(f): return f
            return _dec
    limiter = _NoOpLimiter()

@app.before_request
def ensure_db():
    if not hasattr(app, "_db_ready"):
        init_db()
        _seed_all()
        _ensure_survey_article()   # runs independently of _seed_all guard
        _sync_admin_passwords()    # re-hashes admin passwords from env var every deploy
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

# ── FREEZE HELPERS ────────────────────────────────────────────────────────────

FREEZE_AUTHORIZED_ROLES = {"ceo", "cco", "cfo"}   # roles that can freeze others
CEO_ONLY = {"ceo"}                                  # CEO can freeze admins too

def _get_freeze_status(user_id):
    """Return (freeze_deposits, freeze_withdrawals, reason) for a user."""
    row = fetchone("SELECT freeze_deposits, freeze_withdrawals, freeze_reason FROM users WHERE id=?", (user_id,))
    if not row: return False, False, None
    return bool(row["freeze_deposits"]), bool(row["freeze_withdrawals"]), row["freeze_reason"]

def _can_freeze(actor_role, target_is_admin):
    """Check if actor has permission to freeze a target user."""
    if actor_role not in FREEZE_AUTHORIZED_ROLES:
        return False
    if target_is_admin and actor_role not in CEO_ONLY:
        return False  # Only CEO can freeze other admins
    return True

FROZEN_DEPOSIT_MSG = (
    "Your deposits are currently restricted. "
    "Please contact our customer service team at support@sohana.app "
    "or visit the Help Centre to resolve this."
)
FROZEN_WITHDRAW_MSG = (
    "Your withdrawals are currently restricted. "
    "Please contact our customer service team at support@sohana.app "
    "or visit the Help Centre to resolve this."
)

@app.route("/kyc")
@auth.login_required
def kyc_page():
    user = auth.get_current_user()
    subs = fetchall(
        "SELECT * FROM kyc_submissions WHERE user_id=? ORDER BY submitted_at DESC LIMIT 10",
        (user["id"],)
    )
    return render_template("kyc.html", user=user, submissions=subs)


# ── KYC API ───────────────────────────────────────────────────────────────────

KYC_APPROVE_ROLES = {"ceo", "cco", "cfo"}

@app.route("/api/kyc/submit", methods=["POST"])
@auth.login_required
def api_kyc_submit():
    d    = request.json or {}
    uid  = session["user_id"]
    level = d.get("level", "")
    if level not in ("id", "address", "funds"):
        return jsonify({"error": "Invalid KYC level. Must be id, address, or funds."}), 400
    # Prevent duplicate pending submission for same level
    existing = fetchone(
        "SELECT id FROM kyc_submissions WHERE user_id=? AND level=? AND status='pending'",
        (uid, level)
    )
    if existing:
        return jsonify({"error": "You already have a pending submission for this level."}), 400
    sid = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            """INSERT INTO kyc_submissions(id,user_id,level,doc_type_id,doc_type_addr,doc_type_funds,notes)
               VALUES(?,?,?,?,?,?,?)""",
            (sid, uid, level,
             d.get("doc_type_id") or None,
             d.get("doc_type_addr") or None,
             d.get("doc_type_funds") or None,
             d.get("notes") or None)
        )
        # Mark user kyc_status as pending if not already verified
        user = fetchone("SELECT kyc_status FROM users WHERE id=?", (uid,))
        if user and user["kyc_status"] not in ("verified",):
            db.execute("UPDATE users SET kyc_status='pending' WHERE id=?", (uid,))
    push_notification(uid,
        "KYC submission received ✓",
        "We've received your documents and will review them within 1–2 business days.",
        "info", "/kyc")
    return jsonify({"ok": True, "submission_id": sid})


@app.route("/api/admin/kyc/<submission_id>/approve", methods=["POST"])
@admin_required
def api_admin_kyc_approve(submission_id):
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in KYC_APPROVE_ROLES:
        return jsonify({"error": "Only CEO, CCO, or CFO can approve KYC submissions."}), 403
    sub = fetchone("SELECT * FROM kyc_submissions WHERE id=?", (submission_id,))
    if not sub:
        return jsonify({"error": "Submission not found."}), 404
    if sub["status"] != "pending":
        return jsonify({"error": f"Submission is already {sub['status']}."}), 400
    # Determine new kyc_level based on submission level
    level_map = {"id": "id_verified", "address": "id_verified", "funds": "full"}
    new_level = level_map.get(sub["level"], "id_verified")
    with get_db() as db:
        db.execute(
            """UPDATE kyc_submissions SET status='approved', reviewed_by=?, reviewed_at=datetime('now')
               WHERE id=?""",
            (session["user_id"], submission_id)
        )
        db.execute(
            "UPDATE users SET kyc_level=?, kyc_status='verified' WHERE id=?",
            (new_level, sub["user_id"])
        )
    push_notification(sub["user_id"],
        "Identity verified ✓",
        "Your KYC documents have been approved. You now have access to higher limits.",
        "success", "/kyc")
    return jsonify({"ok": True, "new_kyc_level": new_level})


@app.route("/api/admin/kyc/<submission_id>/reject", methods=["POST"])
@admin_required
def api_admin_kyc_reject(submission_id):
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in KYC_APPROVE_ROLES:
        return jsonify({"error": "Only CEO, CCO, or CFO can reject KYC submissions."}), 403
    d = request.json or {}
    note = d.get("note", "").strip()
    if not note:
        return jsonify({"error": "A rejection reason is required."}), 400
    sub = fetchone("SELECT * FROM kyc_submissions WHERE id=?", (submission_id,))
    if not sub:
        return jsonify({"error": "Submission not found."}), 404
    if sub["status"] != "pending":
        return jsonify({"error": f"Submission is already {sub['status']}."}), 400
    with get_db() as db:
        db.execute(
            """UPDATE kyc_submissions SET status='rejected', reviewed_by=?, reviewed_at=datetime('now'),
               rejection_note=? WHERE id=?""",
            (session["user_id"], note, submission_id)
        )
        db.execute("UPDATE users SET kyc_status='rejected' WHERE id=?", (sub["user_id"],))
    push_notification(sub["user_id"],
        "KYC review update",
        f"Your document submission was not approved. Reason: {note}. Please resubmit with the correct documents.",
        "danger", "/kyc")
    return jsonify({"ok": True})


@app.route("/api/admin/kyc/manual-approve", methods=["POST"])
@admin_required
def api_admin_kyc_manual_approve():
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in KYC_APPROVE_ROLES:
        return jsonify({"error": "Only CEO, CCO, or CFO can manually approve KYC."}), 403
    d         = request.json or {}
    target_id = d.get("user_id", "").strip()
    kyc_level = d.get("kyc_level", "id_verified")
    if kyc_level not in ("id_verified", "full"):
        return jsonify({"error": "kyc_level must be id_verified or full."}), 400
    if not target_id:
        return jsonify({"error": "user_id required."}), 400
    target = fetchone("SELECT id, full_name FROM users WHERE id=?", (target_id,))
    if not target:
        return jsonify({"error": "User not found."}), 404
    with get_db() as db:
        db.execute(
            "UPDATE users SET kyc_level=?, kyc_status='verified' WHERE id=?",
            (kyc_level, target_id)
        )
    push_notification(target_id,
        "Identity manually verified ✓",
        "Your account has been verified by our team. You now have full platform access.",
        "success", "/kyc")
    return jsonify({"ok": True, "user_name": target["full_name"], "kyc_level": kyc_level})


# ── ADMIN KYC PANEL ───────────────────────────────────────────────────────────

@app.route("/admin/kyc")
@admin_required
def admin_kyc_panel():
    user = auth.get_current_user()
    actor_role = user.get("admin_role", "")
    if actor_role not in KYC_APPROVE_ROLES:
        return redirect(url_for("admin_home"))
    pending   = fetchall(
        """SELECT ks.*, u.full_name, u.phone, u.email, u.hanatag
           FROM kyc_submissions ks JOIN users u ON u.id=ks.user_id
           WHERE ks.status='pending' ORDER BY ks.submitted_at ASC"""
    )
    reviewed  = fetchall(
        """SELECT ks.*, u.full_name, u.phone, a.full_name as reviewer_name
           FROM kyc_submissions ks JOIN users u ON u.id=ks.user_id
           LEFT JOIN users a ON a.id=ks.reviewed_by
           WHERE ks.status IN ('approved','rejected')
           ORDER BY ks.reviewed_at DESC LIMIT 50"""
    )
    all_users = fetchall(
        """SELECT id, full_name, phone, email, hanatag, kyc_level, kyc_status, created_at
           FROM users WHERE is_admin=0 ORDER BY created_at DESC LIMIT 200"""
    )
    return render_template("admin_kyc.html", user=user, actor_role=actor_role,
                           pending=pending, reviewed=reviewed, all_users=all_users)


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
    session["user_name"]  = user["full_name"]
    session["is_admin"]   = bool(user["is_admin"])
    if user.get("is_admin"):
        session["admin_role"] = user.get("admin_role", "")
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
    _totp_row      = fetchone("SELECT totp_enabled FROM users WHERE id=?", (profile_user["id"],))
    _profile_dict  = dict(profile_user)
    _profile_dict["totp_enabled"] = bool(_totp_row["totp_enabled"]) if _totp_row else False
    return render_template("profile.html", user=me, profile_user=_profile_dict,
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
    try:
        return _circle_detail_inner(rosca_id)
    except Exception as _e:
        import traceback, sys
        tb = traceback.format_exc()
        print(f"[circle_detail ERROR] {_e}\n{tb}", file=sys.stderr, flush=True)
        return render_template("error.html",
                               message="This circle couldn\'t be loaded. Please try again.",
                               back_url="/circles"), 500


def _circle_detail_inner(rosca_id):
    user = auth.get_current_user()
    r    = rosca.get_rosca(rosca_id)
    if not r: return redirect(url_for("circles_page"))
    r = dict(r)   # convert sqlite3.Row → dict so .get() works throughout
    # Fetch members enriched with user data (full_name, ncs_score, hanatag)
    # sqlite3.Row supports row["key"] but NOT row.get("key") — use explicit key access
    _raw_members = rosca.get_rosca_members(rosca_id)
    _member_ids  = [m["user_id"] for m in _raw_members]
    _user_lookup = {}
    if _member_ids:
        placeholders = ",".join(["?"] * len(_member_ids))
        for _ur in fetchall(
            f"SELECT id, full_name, hanatag, ncs_score, ncs_tier FROM users WHERE id IN ({placeholders})",
            tuple(_member_ids)
        ):
            # Convert sqlite3.Row → plain dict so .get() works safely downstream
            _user_lookup[_ur["id"]] = dict(_ur)
    members = []
    for _rm in _raw_members:
        _rm_dict = dict(_rm)   # convert Row → dict so .get() works
        _ud      = _user_lookup.get(_rm_dict.get("user_id", ""), {})
        members.append({
            "user_id":         _rm_dict.get("user_id", ""),
            "rosca_id":        _rm_dict.get("rosca_id", rosca_id),
            "status":          _rm_dict.get("status", "active"),
            "payout_position": _rm_dict.get("payout_position") or 0,
            "joined_at":       _rm_dict.get("joined_at", ""),
            "full_name":       _ud.get("full_name", "Member"),
            "hanatag":         _ud.get("hanatag", ""),
            "ncs_score":       _ud.get("ncs_score", 300) or 300,
            "ncs_tier":        _ud.get("ncs_tier", "Probation"),
        })
    cycle_info = rosca.get_cycle_status(rosca_id)
    is_member    = any(m["user_id"] == user["id"] for m in members)
    is_organiser = r["organiser_id"] == user["id"]

    # Safely unpack cycle_info — key names differ depending on circle state
    my_contrib     = None
    cycle_num      = int(r["current_cycle"] or 1) if r.get("current_cycle") else 1
    cycle_due      = None
    cycle_recip    = None
    cycle_contribs = []

    if cycle_info:
        try:
            ci = dict(cycle_info) if not isinstance(cycle_info, dict) else cycle_info
        except Exception:
            ci = {}
        cycle_num      = int(ci.get("cycle_number") or ci.get("number") or
                             ci.get("current_cycle") or cycle_num or 1)
        cycle_due      = ci.get("due_date")
        cycle_recip    = ci.get("recipient_id")
        cycle_contribs = ci.get("contributions") or []
        for c in cycle_contribs:
            try:
                if c["user_id"] == user["id"]:
                    my_contrib = c
            except Exception:
                pass
    try:
        leaderboard = ncs_engine.get_leaderboard(rosca_id)
    except AttributeError:
        leaderboard = []  # function may not exist in current ncs_engine version
    my_endorsements = {e["to_id"] for e in fetchall("SELECT to_id FROM endorsements WHERE from_id=?", (user["id"],))}
    _ensure_circle_tables()
    # Recent activity (last 20)
    activity = fetchall("""SELECT ca.*, u.full_name, u.hanatag
                           FROM circle_activity ca LEFT JOIN users u ON u.id=ca.actor_id
                           WHERE ca.rosca_id=? ORDER BY ca.created_at DESC LIMIT 20""",
                        (rosca_id,))
    # Pinned + recent announcements
    announcements = fetchall("""SELECT ca.*, u.full_name
                                FROM circle_announcements ca JOIN users u ON u.id=ca.author_id
                                WHERE ca.rosca_id=?
                                ORDER BY ca.is_pinned DESC, ca.created_at DESC LIMIT 10""",
                             (rosca_id,))
    # Open votes
    votes = fetchall("""SELECT v.*,
                               (SELECT COUNT(*) FROM circle_vote_responses r WHERE r.vote_id=v.id) as response_count,
                               (SELECT response FROM circle_vote_responses r WHERE r.vote_id=v.id AND r.user_id=?) as my_vote
                        FROM circle_votes v WHERE v.rosca_id=? AND v.status='open'
                        ORDER BY v.created_at DESC""",
                     (user["id"], rosca_id))
    # Chat unread count (messages since user's last visit — simplified: total count)
    chat_count = fetchone("SELECT COUNT(*) as c FROM circle_messages WHERE rosca_id=?", (rosca_id,))
    return render_template("circle_detail.html", user=user, rosca=r, members=members,
                           cycle_info=cycle_info,
                           cycle_num=cycle_num, cycle_due=cycle_due,
                           cycle_recip=cycle_recip, cycle_contribs=cycle_contribs,
                           is_member=is_member, is_organiser=is_organiser,
                           my_contrib=my_contrib, leaderboard=leaderboard,
                           my_endorsements=my_endorsements,
                           activity=activity, announcements=announcements, votes=votes,
                           chat_count=chat_count["c"] if chat_count else 0)

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
        "cfo":        "admin_executive",
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

def _safe_count(sql, params=()):
    """Run a COUNT query and return 0 on any error (e.g. missing column)."""
    try:
        row = fetchone(sql, params)
        return row["c"] if row else 0
    except Exception:
        return 0


def _run_safe_migrations():
    """
    Add new columns introduced post-launch without breaking existing DB.
    Each ALTER TABLE is wrapped in try/except so re-runs are harmless.
    """
    migrations = [
        "ALTER TABLE users              ADD COLUMN is_suspended  INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users              ADD COLUMN risk_level    TEXT",
        "ALTER TABLE users              ADD COLUMN google_id     TEXT",
        "ALTER TABLE users              ADD COLUMN picture_url   TEXT",
        "ALTER TABLE users              ADD COLUMN totp_secret   TEXT",
        "ALTER TABLE users              ADD COLUMN totp_enabled  INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users              ADD COLUMN email_notifs  INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users              ADD COLUMN twofa_method  TEXT    NOT NULL DEFAULT 'totp'",
        "ALTER TABLE wallet_transactions ADD COLUMN flagged_for_review INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE wallet_transactions ADD COLUMN flag_reason  TEXT",
        "ALTER TABLE wallet_transactions ADD COLUMN reversed_by  TEXT",
        "ALTER TABLE wallet_transactions ADD COLUMN reversed_at  TEXT",

        # ── STATUS PAGE TABLES (v7.3) ──
        """CREATE TABLE IF NOT EXISTS service_components (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'operational',
            display_order INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS status_incidents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            component_id TEXT,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            scheduled_start TEXT,
            scheduled_end TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS status_incident_updates (
            id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS status_checks_log (
            id TEXT PRIMARY KEY,
            component_id TEXT NOT NULL,
            is_up INTEGER NOT NULL,
            response_ms INTEGER,
            source TEXT NOT NULL DEFAULT 'internal',
            checked_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS status_subscribers (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            is_confirmed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_status_checks_component ON status_checks_log(component_id, checked_at)",
        "CREATE INDEX IF NOT EXISTS idx_status_incidents_status ON status_incidents(status, severity)",
    ]
    for sql in migrations:
        try:
            with get_db() as db:
                db.execute(sql)
        except Exception:
            pass  # Column already exists — safe to ignore

    # Seed default service components (idempotent)
    try:
        with get_db() as db:
            for cid, name, order in [
                ('web_app',       'Web App',           1),
                ('auth',          'Authentication',    2),
                ('wallet',        'Wallet & Payments', 3),
                ('circles_pools', 'Circles & Pools',   4),
                ('hanapay',       'Hanapay',           5),
                ('comms',         'Email & SMS',       6),
            ]:
                db.execute(
                    "INSERT OR IGNORE INTO service_components (id, name, display_order) VALUES (?,?,?)",
                    (cid, name, order)
                )
    except Exception:
        pass


# Run migrations once at import time (harmless on repeat calls)
try:
    _run_safe_migrations()
except Exception:
    pass


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
        "fraud_prevented":  _safe_count("SELECT COALESCE(SUM(ABS(amount_cents)),0) as c FROM wallet_transactions WHERE tx_type='reversal'"),
        "total_earnings":   fetchone("SELECT COALESCE(SUM(ABS(amount_cents)),0) as c FROM wallet_transactions WHERE amount_cents>0 AND tx_type='rosca_payout'")["c"],
        "platform_earnings":fetchone("SELECT COUNT(*) as c FROM wallet_transactions WHERE tx_type='fee'")["c"],
        "late_members":     fetchone("SELECT COUNT(DISTINCT user_id) as c FROM contributions WHERE status='late'")["c"],
        "suspended_users":  _safe_count("SELECT COUNT(*) as c FROM users WHERE is_suspended=1 AND is_admin=0"),
        "platform_ctrl":    _get_platform_controls(),
    }

def _table_exists(name):
    r = fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(r)


# ── PLATFORM CONTROLS ─────────────────────────────────────────────────

def _ensure_platform_controls():
    """Create and seed platform_controls on first use."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS platform_controls (
                id                       INTEGER PRIMARY KEY DEFAULT 1,
                deposits_enabled         INTEGER NOT NULL DEFAULT 1,
                withdrawals_enabled      INTEGER NOT NULL DEFAULT 1,
                transfers_enabled        INTEGER NOT NULL DEFAULT 1,
                rosca_payouts_enabled    INTEGER NOT NULL DEFAULT 1,
                new_registrations_enabled INTEGER NOT NULL DEFAULT 1,
                maintenance_mode         INTEGER NOT NULL DEFAULT 0,
                updated_by_admin_id      TEXT,
                updated_at               TEXT DEFAULT (datetime('now')),
                reason                   TEXT
            )""")
            # Seed one row if empty
            existing = fetchone("SELECT id FROM platform_controls WHERE id=1")
            if not existing:
                db.execute("""INSERT INTO platform_controls
                    (id,deposits_enabled,withdrawals_enabled,transfers_enabled,
                     rosca_payouts_enabled,new_registrations_enabled,maintenance_mode)
                    VALUES(1,1,1,1,1,1,0)""")
    except Exception as e:
        import sys; print(f"[_ensure_platform_controls] {e}", file=sys.stderr, flush=True)


def _get_platform_controls():
    """Return the current platform control row (always row id=1)."""
    _ensure_platform_controls()
    row = fetchone("SELECT * FROM platform_controls WHERE id=1")
    if not row:
        return {"deposits_enabled":1,"withdrawals_enabled":1,"transfers_enabled":1,
                "rosca_payouts_enabled":1,"new_registrations_enabled":1,"maintenance_mode":0}
    return dict(row)


def _platform_check(flag: str, error_msg: str):
    """Raise a 503 JSON response if a platform control flag is disabled."""
    ctrl = _get_platform_controls()
    if not ctrl.get(flag, 1):
        from flask import abort
        abort(503, description=error_msg)


# ── ADMIN AUDIT LOG ───────────────────────────────────────────────────

def _ensure_audit_log():
    """Create admin_audit_logs table on first use."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id            TEXT PRIMARY KEY,
                admin_id      TEXT NOT NULL,
                admin_role    TEXT,
                action_type   TEXT NOT NULL,
                entity_type   TEXT,
                entity_id     TEXT,
                previous_data TEXT,
                new_data      TEXT,
                reason        TEXT,
                ip_address    TEXT,
                user_agent    TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_logs(admin_id, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON admin_audit_logs(entity_type, entity_id)")
    except Exception as e:
        import sys; print(f"[_ensure_audit_log] {e}", file=sys.stderr, flush=True)


def log_admin_action(action_type, entity_type=None, entity_id=None,
                     previous_data=None, new_data=None, reason=None):
    """Write one row to admin_audit_logs. Call from every admin action endpoint."""
    import json as _json
    _ensure_audit_log()
    try:
        admin_id   = session.get("user_id", "system")
        admin_row  = fetchone("SELECT admin_role FROM users WHERE id=?", (admin_id,))
        admin_role = admin_row["admin_role"] if admin_row else None
        ip         = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ua         = request.headers.get("User-Agent", "")[:256]
        with get_db() as db:
            db.execute("""INSERT INTO admin_audit_logs
                (id,admin_id,admin_role,action_type,entity_type,entity_id,
                 previous_data,new_data,reason,ip_address,user_agent)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), admin_id, admin_role, action_type,
                 entity_type, str(entity_id) if entity_id else None,
                 _json.dumps(previous_data) if previous_data else None,
                 _json.dumps(new_data) if new_data else None,
                 reason, ip, ua))
    except Exception as e:
        import sys; print(f"[log_admin_action] {e}", file=sys.stderr, flush=True)


# ── ADMIN NOTES ───────────────────────────────────────────────────────

def _ensure_admin_notes():
    """Create admin_notes table on first use."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS admin_notes (
                id          TEXT PRIMARY KEY,
                admin_id    TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                note        TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_notes_entity ON admin_notes(entity_type, entity_id, created_at DESC)")
    except Exception as e:
        import sys; print(f"[_ensure_admin_notes] {e}", file=sys.stderr, flush=True)



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
    users = fetchall("""SELECT u.*,
        (SELECT COUNT(*) FROM rosca_members rm WHERE rm.user_id=u.id AND rm.status='active') as active_circles,
        (SELECT COUNT(*) FROM contributions c WHERE c.user_id=u.id AND c.status='missed') as missed_contribs
        FROM users u WHERE u.is_admin=0 ORDER BY u.created_at DESC LIMIT 200""")
    actor_role = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    can_freeze  = actor_role and actor_role["admin_role"] in {"ceo","cco","cfo"}
    return render_template("admin_users.html", user=user, users=users,
                           can_freeze=can_freeze,
                           actor_role=actor_role["admin_role"] if actor_role else None)

@app.route("/admin/blog")
@any_admin_required
def admin_blog():
    user  = auth.get_current_user()
    posts = fetchall("SELECT * FROM blog_posts ORDER BY created_at DESC")
    return render_template("admin_blog.html", user=user, posts=posts)

# ── AUTH API ──────────────────────────────────────────────────────────────────

# ── FORGOT PASSWORD ───────────────────────────────────────────────────────────

@app.route("/forgot-password")
def forgot_password_page():
    return render_template("forgot_password.html")


@app.route("/api/auth/forgot-password", methods=["POST"])
@limiter.limit("3 per minute")
def api_forgot_password():
    """Initiate a password reset request.
    Phase 1 (no email): logs the request, returns support instructions.
    Phase 2 (with email): will send a reset link automatically."""
    d     = request.json or {}
    phone = d.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "Phone number is required."}), 400

    # Always return generic success (don't reveal whether account exists)
    user = fetchone("SELECT id, full_name, email, phone, is_admin FROM users WHERE phone=? OR email=?",
                    (phone, phone))
    if user and not user["is_admin"]:
        try:
            import secrets as _sec, os
            token      = _sec.token_urlsafe(32)
            base_url   = os.environ.get("APP_BASE_URL", "https://sohana.app")
            reset_link = f"{base_url}/reset-password/{token}"
            with get_db() as db:
                db.execute("UPDATE password_reset_tokens SET used=1 WHERE user_id=?",
                           (user["id"],))  # invalidate old tokens
                db.execute(
                    """INSERT INTO password_reset_tokens(id,user_id,token,expires_at)
                       VALUES(?,?,?,datetime('now','+1 hour'))""",
                    (str(uuid.uuid4()), user["id"], token)
                )
            # Send email if user has an email address
            if user.get("email"):
                import comms
                comms.send_email(
                    to_email     = user["email"],
                    to_name      = user.get("full_name") or "SOHANA Member",
                    template_key = "reset_pw",
                    template_data = {
                        "reset_link": reset_link,
                        "otp_ttl":    "60 minutes",
                    }
                )
        except Exception as e:
            import sys
            print(f"[forgot_password] {e}", file=sys.stderr, flush=True)
    return jsonify({
        "ok": True,
        "message": ("If an account exists for that phone number or email, "
                    "you will receive a password reset link shortly. "
                    "Check your email inbox. The link expires in 60 minutes.")
    })


@app.route("/api/admin/users/<user_id>/reset-password", methods=["POST"])
@admin_required
def api_admin_reset_password(user_id):
    """Admin-only: set a new temporary password for a user."""
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in {"ceo", "cco", "operations"}:
        return jsonify({"error": "Only CEO, CCO, or Operations can reset passwords."}), 403
    d        = request.json or {}
    new_pw   = d.get("password", "").strip()
    if not new_pw or len(new_pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    target = fetchone("SELECT id, full_name, is_admin FROM users WHERE id=?", (user_id,))
    if not target:
        return jsonify({"error": "User not found."}), 404
    if target["is_admin"]:
        return jsonify({"error": "Cannot reset admin passwords via this endpoint."}), 403
    try:
        new_hash = auth.hash_password(new_pw)
        with get_db() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, user_id))
        log_admin_action("user_password_reset", "user", user_id,
                         reason=d.get("reason", "Admin-initiated password reset"))
        return jsonify({"ok": True, "message": f"Password reset for {target['full_name']}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/change-password", methods=["POST"])
@limiter.limit("5 per minute")
@auth.login_required
def api_change_password():
    """Any authenticated user can change their own password."""
    d      = request.json or {}
    old_pw = d.get("current_password", "")
    new_pw = d.get("new_password", "")
    if not old_pw or not new_pw:
        return jsonify({"error": "Current and new password required."}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    try:
        user_row = fetchone("SELECT phone FROM users WHERE id=?", (session["user_id"],))
        auth.login_user(user_row["phone"], old_pw)   # validates current password
        new_hash = auth.hash_password(new_pw)
        with get_db() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (new_hash, session["user_id"]))
        return jsonify({"ok": True})
    except ValueError:
        return jsonify({"error": "Current password is incorrect."}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/admin/reset-own-password", methods=["POST"])
@admin_required
def api_admin_reset_own_password():
    """Admin resets their own password (requires current password verification)."""
    d       = request.json or {}
    old_pw  = d.get("current_password", "")
    new_pw  = d.get("new_password", "")
    if not old_pw or not new_pw:
        return jsonify({"error": "Current and new password required."}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    try:
        user = auth.login_user(
            fetchone("SELECT phone FROM users WHERE id=?", (session["user_id"],))["phone"],
            old_pw
        )
        new_hash = auth.hash_password(new_pw)
        with get_db() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (new_hash, session["user_id"]))
        return jsonify({"ok": True})
    except ValueError:
        return jsonify({"error": "Current password is incorrect."}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════════
# T2 — TOTP INFRASTRUCTURE (Google Authenticator / Authy)
# ══════════════════════════════════════════════════════════════════════════════

def _safe_migration_totp():
    """Ensure TOTP columns and otp_requests table exist."""
    migrations = [
        "ALTER TABLE users ADD COLUMN totp_secret     TEXT",
        "ALTER TABLE users ADD COLUMN totp_enabled    INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN email_notifs    INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN twofa_method    TEXT    NOT NULL DEFAULT 'totp'",
        """CREATE TABLE IF NOT EXISTS otp_requests (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            code        TEXT NOT NULL,
            method      TEXT NOT NULL DEFAULT 'email',
            purpose     TEXT NOT NULL DEFAULT '2fa',
            used        INTEGER NOT NULL DEFAULT 0,
            expires_at  TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_otp_lookup ON otp_requests(user_id, purpose, used)",
        """CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            token       TEXT UNIQUE NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            expires_at  TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_reset_token ON password_reset_tokens(token, used)",
    ]
    for sql in migrations:
        try:
            with get_db() as db:
                db.execute(sql)
        except Exception:
            pass


_safe_migration_totp()  # run at import time


@app.route("/api/auth/totp/setup", methods=["POST"])
@auth.login_required
def api_totp_setup():
    """Generate a new TOTP secret and return a QR code data URL."""
    try:
        import pyotp, qrcode, io, base64
        user = auth.get_current_user()
        secret = pyotp.random_base32()
        label  = f"SOHANA:{user.get('phone') or user.get('email') or 'user'}"
        uri    = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name="SOHANA")
        # Generate QR code as base64 PNG
        img    = qrcode.make(uri)
        buf    = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        # Store the secret temporarily (not yet enabled — user must confirm)
        with get_db() as db:
            db.execute("UPDATE users SET totp_secret=? WHERE id=?",
                       (secret, user["id"]))
        return jsonify({"ok": True, "secret": secret,
                        "qr": f"data:image/png;base64,{qr_b64}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/totp/confirm", methods=["POST"])
@limiter.limit("10 per minute")
@auth.login_required
def api_totp_confirm():
    """Verify the user has scanned the QR and can produce a valid code."""
    try:
        import pyotp
        code = (request.json or {}).get("code", "").strip()
        user = auth.get_current_user()
        secret = fetchone("SELECT totp_secret FROM users WHERE id=?", (user["id"],))
        if not secret or not secret["totp_secret"]:
            return jsonify({"error": "Run /api/auth/totp/setup first."}), 400
        totp = pyotp.TOTP(secret["totp_secret"])
        if totp.verify(code, valid_window=1):
            with get_db() as db:
                db.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (user["id"],))
            return jsonify({"ok": True, "message": "2FA enabled successfully."})
        return jsonify({"error": "Invalid code. Please try again."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/totp/disable", methods=["POST"])
@limiter.limit("5 per minute")
@auth.login_required
def api_totp_disable():
    """Disable TOTP — requires current TOTP code to confirm."""
    try:
        import pyotp
        code = (request.json or {}).get("code", "").strip()
        user = auth.get_current_user()
        row  = fetchone("SELECT totp_secret, totp_enabled FROM users WHERE id=?",
                        (user["id"],))
        if not row or not row["totp_enabled"]:
            return jsonify({"error": "2FA is not currently enabled."}), 400
        totp = pyotp.TOTP(row["totp_secret"])
        if not totp.verify(code, valid_window=1):
            return jsonify({"error": "Invalid code."}), 400
        with get_db() as db:
            db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?",
                       (user["id"],))
        return jsonify({"ok": True, "message": "2FA disabled."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _verify_totp_code(user_id: str, code: str) -> bool:
    """Helper: validate a TOTP code for a user. Returns True on success."""
    try:
        import pyotp
        row = fetchone("SELECT totp_secret, totp_enabled FROM users WHERE id=?",
                       (user_id,))
        if not row or not row["totp_enabled"] or not row["totp_secret"]:
            return True   # 2FA not enabled — allow through
        totp = pyotp.TOTP(row["totp_secret"])
        return totp.verify(code, valid_window=1)
    except Exception:
        return False


def _require_totp(user_id: str, code: str):
    """Return a 403 JSON response if TOTP is enabled and code is wrong, else None."""
    row = fetchone("SELECT totp_enabled FROM users WHERE id=?", (user_id,))
    if row and row["totp_enabled"]:
        if not code:
            return jsonify({"error": "2FA code required.",
                            "requires_2fa": True}), 403
        if not _verify_totp_code(user_id, code):
            return jsonify({"error": "Invalid 2FA code."}), 403
    return None   # all good


# ══════════════════════════════════════════════════════════════════════════════
# T3 — WIRE 2FA INTO LOGIN FLOWS
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/api/auth/login-step2", methods=["POST"])
@limiter.limit("10 per minute")
def api_login_step2():
    """Phase-2 login: validate TOTP code and grant session."""
    d     = request.json or {}
    token = d.get("pending_token", "").strip()
    code  = d.get("totp_code", "").strip()
    if not token or not code:
        return jsonify({"error": "Pending token and 2FA code are required."}), 400
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pending_logins (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL)""")
        row = fetchone(
            "SELECT user_id FROM pending_logins WHERE token=? AND expires_at > datetime('now')",
            (token,)
        )
        if not row:
            return jsonify({"error": "Token expired or invalid. Please start login again."}), 401
        user_id = row["user_id"]
        if not _verify_totp_code(user_id, code):
            return jsonify({"error": "Invalid 2FA code."}), 401
        # Delete the pending token
        with get_db() as db:
            db.execute("DELETE FROM pending_logins WHERE token=?", (token,))
        # Grant session
        user_row = fetchone("SELECT * FROM users WHERE id=?", (user_id,))
        user_d   = dict(user_row) if user_row else {}
        session["user_id"]   = user_d["id"]
        session["user_name"] = user_d["full_name"]
        session["is_admin"]  = bool(user_d.get("is_admin", 0))
        if user_d.get("is_admin"):
            session["admin_role"] = user_d.get("admin_role")
            log_admin_action("admin_login_2fa_success", "auth", user_id)
        return jsonify({"ok": True,
                        "is_admin":   bool(user_d.get("is_admin", 0)),
                        "admin_role": user_d.get("admin_role")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# T5 — EMAIL NOTIFICATION PREFERENCES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/profile/notification-prefs", methods=["POST"])
@auth.login_required
def api_notification_prefs():
    """Toggle email notification preference for current user."""
    enabled = bool((request.json or {}).get("email_notifs", True))
    try:
        with get_db() as db:
            db.execute("UPDATE users SET email_notifs=? WHERE id=?",
                       (int(enabled), session["user_id"]))
        return jsonify({"ok": True, "email_notifs": enabled})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _notify_user(user_id: str, subject: str, message: str,
                 template_key: str = "notification",
                 template_data: dict = None):
    """
    Send both in-app notification AND email (if user has email_notifs enabled).
    Wraps the existing push_notification() and adds email delivery.
    """
    push_notification(user_id, subject, message, "info")
    try:
        row = fetchone("SELECT full_name, email, email_notifs FROM users WHERE id=?",
                       (user_id,))
        if not row or not row["email"] or not row["email_notifs"]:
            return
        import comms
        data = {"subject_line": subject, "message_body": message}
        if template_data:
            data.update(template_data)
        comms.send_email(
            to_email   = row["email"],
            to_name    = row["full_name"] or "SOHANA Member",
            template_key = template_key,
            template_data = data
        )
    except Exception as e:
        import sys
        print(f"[_notify_user email] {e}", file=sys.stderr, flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# T6 — REAL EMAIL PASSWORD RESET
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/reset-password/<token>")
def reset_password_page(token):
    """Show the password reset form for a valid token."""
    row = fetchone(
        "SELECT user_id FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > datetime('now')",
        (token,)
    )
    if not row:
        return render_template("reset_password.html",
                               valid=False, token=token), 400
    return render_template("reset_password.html", valid=True, token=token)


@app.route("/api/auth/reset-password", methods=["POST"])
@limiter.limit("5 per minute")
def api_reset_password():
    """Consume a reset token and set the new password."""
    d       = request.json or {}
    token   = d.get("token", "").strip()
    new_pw  = d.get("password", "").strip()
    if not token or not new_pw:
        return jsonify({"error": "Token and new password are required."}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    row = fetchone(
        "SELECT user_id FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > datetime('now')",
        (token,)
    )
    if not row:
        return jsonify({"error": "This link has expired or already been used."}), 400
    try:
        new_hash = auth.hash_password(new_pw)
        with get_db() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (new_hash, row["user_id"]))
            db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?",
                       (token,))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# T7 — GOOGLE OAUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/auth/google")
def google_oauth_start():
    """Redirect user to Google OAuth consent screen."""
    try:
        import os, urllib.parse
        client_id    = os.environ.get("GOOGLE_CLIENT_ID", "")
        redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI",
                                      "https://sohana.app/auth/google/callback")
        if not client_id:
            return redirect(url_for("auth_page") + "?error=google_not_configured")
        import secrets as _sec
        state = _sec.token_urlsafe(16)
        session["oauth_state"] = state
        params = urllib.parse.urlencode({
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "response_type": "code",
            "scope":         "openid email profile",
            "state":         state,
            "access_type":   "online",
            "prompt":        "select_account",
        })
        return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
    except Exception as e:
        return redirect(url_for("auth_page") + f"?error={str(e)}")


@app.route("/auth/google/callback")
def google_oauth_callback():
    """Handle Google OAuth callback — create or log in user."""
    import os, requests as _req
    error = request.args.get("error")
    if error:
        return redirect(url_for("auth_page") + f"?error=google_{error}")

    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if state != session.get("oauth_state", ""):
        return redirect(url_for("auth_page") + "?error=invalid_state")

    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    redirect_uri  = os.environ.get("GOOGLE_REDIRECT_URI",
                                   "https://sohana.app/auth/google/callback")
    try:
        # Exchange code for tokens
        token_resp = _req.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=10)
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return redirect(url_for("auth_page") + "?error=token_exchange_failed")

        # Fetch user info from Google
        user_info = _req.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        ).json()

        google_email = user_info.get("email", "").lower()
        google_name  = user_info.get("name",  "")
        google_id    = user_info.get("sub",   "")
        picture      = user_info.get("picture", "")

        if not google_email:
            return redirect(url_for("auth_page") + "?error=no_email_from_google")

        # Look up existing user by email or google_id
        existing = (
            fetchone("SELECT * FROM users WHERE google_id=?", (google_id,)) or
            fetchone("SELECT * FROM users WHERE email=?",     (google_email,))
        )

        if existing:
            # Log them in
            existing = dict(existing)
            # Update google_id if not set
            if not existing.get("google_id"):
                with get_db() as db:
                    db.execute("UPDATE users SET google_id=? WHERE id=?",
                               (google_id, existing["id"]))
            session["user_id"]  = existing["id"]
            session["user_name"] = existing["full_name"]
            session["is_admin"]  = bool(existing.get("is_admin"))
            return redirect(url_for("dashboard"))
        else:
            # Create a new account
            # Generate hanatag from name
            first_name = google_name.split()[0] if google_name else "user"
            import random, string
            suffix   = "".join(random.choices(string.digits, k=4))
            hanatag  = f"@{first_name.lower()}{suffix}"
            # Ensure unique
            while fetchone("SELECT id FROM users WHERE hanatag=?", (hanatag,)):
                suffix  = "".join(random.choices(string.digits, k=4))
                hanatag = f"@{first_name.lower()}{suffix}"

            new_id = str(uuid.uuid4())
            # Use a long random unusable password (user can set one later)
            import secrets as _sec
            random_pw = _sec.token_hex(32)
            pw_hash   = auth.hash_password(random_pw)

            with get_db() as db:
                db.execute("""INSERT INTO users
                    (id, full_name, email, google_id, hanatag, password_hash,
                     country, base_currency, kyc_level, ncs_score, ncs_tier,
                     is_admin, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,0,datetime('now'))""",
                    (new_id, google_name or google_email.split("@")[0],
                     google_email, google_id, hanatag, pw_hash,
                     "FR", "EUR", "phone", 300, "Probation"))
                # Create default EUR wallet
                db.execute(
                    "INSERT INTO wallets(id,user_id,currency,balance_cents,is_default) VALUES(?,?,?,0,1)",
                    (str(uuid.uuid4()), new_id, "EUR")
                )
            session["user_id"]  = new_id
            session["user_name"] = google_name or google_email
            session["is_admin"]  = False
            # Send welcome email
            try:
                import comms
                comms.send_email(
                    to_email=google_email, to_name=google_name or "New member",
                    template_key="welcome",
                    template_data={"hanatag": hanatag}
                )
            except Exception:
                pass
            return redirect(url_for("dashboard"))

    except Exception as e:
        import sys
        print(f"[google_oauth_callback] {e}", file=sys.stderr, flush=True)
        return redirect(url_for("auth_page") + "?error=oauth_failed")


@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("5 per minute")
def api_register():
    _platform_check("new_registrations_enabled", "New registrations are temporarily paused. Please check back later.")
    d = request.json or {}
    # Support both full_name (legacy) and first_name+last_name (new spec)
    first = d.get("first_name", "").strip()
    last  = d.get("last_name", "").strip()
    full  = d.get("full_name", "").strip() or f"{first} {last}".strip()
    dob   = d.get("date_of_birth", "")
    # Basic age check (must be 18+)
    if dob:
        try:
            from datetime import date
            bdate = date.fromisoformat(dob)
            age = (date.today() - bdate).days // 365
            if age < 18:
                return jsonify({"error": "You must be at least 18 years old to register."}), 400
        except ValueError:
            return jsonify({"error": "Invalid date of birth format. Use YYYY-MM-DD."}), 400
    try:
        uid = auth.register_user(
            phone           = d.get("phone", ""),
            full_name       = full,
            password        = d.get("password", ""),
            email           = d.get("email") or None,
            country         = d.get("country", "RW"),
            first_name      = first or None,
            last_name       = last or None,
            gender          = d.get("gender") or None,
            date_of_birth   = dob or None,
            nationality     = d.get("nationality") or None,
            occupation      = d.get("occupation") or None,
            source_of_funds = d.get("source_of_funds") or None,
        )
        session["user_id"] = uid
        push_notification(uid, "Welcome to SOHANA! 🎉", "Your account is ready. Start by joining a circle.", "success", "/circles")
        return jsonify({"ok": True, "user_id": uid})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    """Main login endpoint — handles both regular and 2FA flows."""
    d  = request.json or {}
    ph = (d.get("phone") or d.get("email_or_phone") or d.get("email") or "").strip()
    pw = (d.get("password") or d.get("pw") or "").strip()
    if not ph or not pw:
        return jsonify({"error": "Phone and password are required."}), 400
    try:
        user    = auth.login_user(ph, pw)
        user_d  = dict(user)
        row     = fetchone(
            "SELECT totp_enabled, is_admin, admin_role FROM users WHERE id=?",
            (user_d["id"],)
        )
        row_d   = dict(row) if row else {}

        if row_d.get("totp_enabled"):
            # 2FA required — issue a short-lived pending token
            pending = str(uuid.uuid4())
            with get_db() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS pending_logins (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL)""")
                db.execute("DELETE FROM pending_logins WHERE user_id=?", (user_d["id"],))
                db.execute(
                    "INSERT INTO pending_logins(token,user_id,expires_at) "
                    "VALUES(?,?,datetime('now','+5 minutes'))",
                    (pending, user_d["id"])
                )
            return jsonify({"ok": False, "requires_2fa": True,
                            "pending_token": pending})

        # No 2FA — set session and return success
        session["user_id"]  = user_d["id"]
        session["user_name"] = user_d.get("full_name", "")
        session["is_admin"]  = bool(row_d.get("is_admin", 0))
        if row_d.get("is_admin"):
            session["admin_role"] = row_d.get("admin_role", "")

        return jsonify({
            "ok":       True,
            "is_admin": bool(row_d.get("is_admin", 0)),
            "user":     {"id": user_d["id"], "name": user_d.get("full_name", "")}
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        import sys, traceback
        print(f"[api_login] {e}\n{traceback.format_exc()}",
              file=sys.stderr, flush=True)
        return jsonify({"error": "Login failed — please try again."}), 500

@app.route("/api/auth/admin-login", methods=["POST"])
def api_admin_login():
    d = request.json or {}
    # Generic error message — never reveal whether the account exists or the role is wrong
    GENERIC_ERROR = "Invalid credentials."
    try:
        user = auth.login_user(d.get("email_or_phone",""), d.get("password",""))
        u = fetchone("SELECT is_admin, admin_role FROM users WHERE id=?", (user["id"],))
        if not u or not u["is_admin"]:
            return jsonify({"error": GENERIC_ERROR}), 403

        # Server-side role validation — if caller specified an expected_role, verify it
        expected_role = d.get("expected_role", "").strip()
        actual_role   = u["admin_role"] or ""
        if expected_role and expected_role != actual_role:
            # Log the mismatch attempt but return the same generic error
            log_admin_action("admin_login_role_mismatch",
                             entity_type="auth",
                             entity_id=user["id"],
                             previous_data={"expected_role": expected_role},
                             new_data={"actual_role": actual_role},
                             reason="Role mismatch on login attempt")
            return jsonify({"error": GENERIC_ERROR}), 403

        session["user_id"]   = user["id"]
        session["user_name"]  = user["full_name"]
        session["is_admin"]   = True
        session["admin_role"] = actual_role
        log_admin_action("admin_login_success",
                         entity_type="auth",
                         entity_id=user["id"],
                         new_data={"role": actual_role})
        return jsonify({"ok": True, "role": actual_role})
    except ValueError:
        return jsonify({"error": GENERIC_ERROR}), 401

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


# ── CAREERS APPLICATIONS ──────────────────────────────────────────────────────

@app.route("/api/careers/apply", methods=["POST"])
def api_careers_apply():
    """Capture career application submissions from the careers page."""
    d         = request.json or {}
    name      = d.get("name", "").strip()
    email     = d.get("email", "").strip().lower()
    phone     = d.get("phone", "").strip()
    role      = d.get("role", "").strip()
    portfolio = d.get("portfolio", "").strip()
    message   = d.get("message", "").strip()

    # Basic validation
    if not name or not email or not phone:
        return jsonify({"error": "Name, email, and phone are required."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "Please enter a valid email address."}), 400
    if not role:
        role = "general"

    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS career_applications (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL,
                phone       TEXT NOT NULL,
                role        TEXT NOT NULL,
                portfolio   TEXT,
                message     TEXT,
                status      TEXT NOT NULL DEFAULT 'new',
                reviewed_by TEXT,
                reviewed_at TEXT,
                notes       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.execute("""INSERT INTO career_applications
                          (id, name, email, phone, role, portfolio, message)
                          VALUES (?,?,?,?,?,?,?)""",
                       (str(uuid.uuid4()), name, email, phone, role,
                        portfolio or None, message or None))
        return jsonify({"ok": True})
    except Exception as e:
        # Don't expose DB errors — log internally and tell user generic
        import sys
        print(f"[careers_apply] failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Submission failed. Please email careers@sohana.app instead."}), 500


@app.route("/admin/careers")
@admin_required
def admin_careers():
    """Admin view of career applications."""
    user = auth.get_current_user()
    try:
        applications = fetchall(
            """SELECT id, name, email, phone, role, portfolio, message,
                      status, reviewed_by, reviewed_at, notes, created_at
               FROM career_applications ORDER BY created_at DESC"""
        )
    except Exception:
        applications = []
    # group by status for stats
    counts = {"new": 0, "reviewed": 0, "shortlisted": 0, "rejected": 0}
    for a in applications:
        s = a["status"] or "new"
        counts[s] = counts.get(s, 0) + 1
    return render_template("admin_careers.html",
                           user=user, applications=applications, counts=counts)


@app.route("/api/admin/careers/<app_id>/status", methods=["POST"])
@admin_required
def api_admin_careers_status(app_id):
    """Update application status (new / reviewed / shortlisted / rejected)."""
    d = request.json or {}
    new_status = d.get("status", "").strip()
    notes      = d.get("notes", "").strip() or None
    if new_status not in ("new", "reviewed", "shortlisted", "rejected"):
        return jsonify({"error": "Invalid status."}), 400
    try:
        with get_db() as db:
            db.execute(
                """UPDATE career_applications
                   SET status=?, reviewed_by=?, reviewed_at=datetime('now'), notes=?
                   WHERE id=?""",
                (new_status, session["user_id"], notes, app_id)
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "Update failed."}), 500


@app.route("/admin/careers/export")
@admin_required
def admin_careers_export():
    import io, csv
    try:
        rows = fetchall(
            """SELECT name, email, phone, role, portfolio, message, status, created_at
               FROM career_applications ORDER BY created_at DESC"""
        )
    except Exception:
        rows = []
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Email", "Phone", "Role", "Portfolio", "Message", "Status", "Submitted"])
    for r in rows:
        writer.writerow([r["name"], r["email"], r["phone"], r["role"],
                         r["portfolio"] or "", r["message"] or "",
                         r["status"], r["created_at"][:16]])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sohana-careers-applications.csv"})


# ── PRESS PAGE (public) ───────────────────────────────────────────────────────

def _ensure_press_tables():
    """Create press tables if they don't exist. Called lazily."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS press_mentions (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                source       TEXT NOT NULL,
                url          TEXT NOT NULL,
                summary      TEXT,
                image_url    TEXT,
                category     TEXT,
                published_at TEXT,
                position     INTEGER NOT NULL DEFAULT 0,
                is_active    INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS press_instagram_posts (
                id         TEXT PRIMARY KEY,
                url        TEXT NOT NULL,
                image_url  TEXT NOT NULL,
                caption    TEXT,
                position   INTEGER NOT NULL DEFAULT 0,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS press_inquiries (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                organisation TEXT NOT NULL,
                email       TEXT NOT NULL,
                phone       TEXT,
                reason      TEXT NOT NULL,
                timeline    TEXT,
                message     TEXT,
                status      TEXT NOT NULL DEFAULT 'new',
                reviewed_by TEXT,
                reviewed_at TEXT,
                notes       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
    except Exception as e:
        import sys
        print(f"[_ensure_press_tables] {e}", file=sys.stderr, flush=True)


@app.route("/press")
def press_page():
    """Public press page — loads mentions and Instagram posts from DB."""
    _ensure_press_tables()
    try:
        mentions = fetchall(
            """SELECT id, title, source, url, summary, image_url, category, published_at
               FROM press_mentions WHERE is_active=1
               ORDER BY position ASC, published_at DESC, created_at DESC"""
        )
    except Exception:
        mentions = []
    try:
        instagram_posts = fetchall(
            """SELECT id, url, image_url, caption
               FROM press_instagram_posts WHERE is_active=1
               ORDER BY position ASC, created_at DESC LIMIT 12"""
        )
    except Exception:
        instagram_posts = []
    return render_template("press.html",
                           mentions=mentions, instagram_posts=instagram_posts)


@app.route("/api/press/inquiry", methods=["POST"])
def api_press_inquiry():
    """Capture press inquiry submissions."""
    _ensure_press_tables()
    d         = request.json or {}
    name      = d.get("name", "").strip()
    org       = d.get("org", "").strip()
    email     = d.get("email", "").strip().lower()
    phone     = d.get("phone", "").strip()
    reason    = d.get("reason", "").strip() or "other"
    timeline  = d.get("timeline", "").strip()
    message   = d.get("message", "").strip()

    if not name or not org or not email:
        return jsonify({"error": "Name, organisation, and email are required."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "Please enter a valid email address."}), 400

    try:
        with get_db() as db:
            db.execute("""INSERT INTO press_inquiries
                          (id, name, organisation, email, phone, reason, timeline, message)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (str(uuid.uuid4()), name, org, email,
                        phone or None, reason, timeline or None, message or None))
        return jsonify({"ok": True})
    except Exception as e:
        import sys
        print(f"[press_inquiry] failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Submission failed. Please email press@sohana.app instead."}), 500


# ── PRESS ADMIN ────────────────────────────────────────────────────────────────

@app.route("/admin/press")
@admin_required
def admin_press():
    """Admin view — manage mentions, Instagram posts, and inquiries."""
    _ensure_press_tables()
    user = auth.get_current_user()
    try:
        mentions = fetchall(
            """SELECT id, title, source, url, summary, image_url, category, published_at,
                      position, is_active, created_at
               FROM press_mentions ORDER BY position ASC, created_at DESC"""
        )
    except Exception:
        mentions = []
    try:
        ig_posts = fetchall(
            """SELECT id, url, image_url, caption, position, is_active, created_at
               FROM press_instagram_posts ORDER BY position ASC, created_at DESC"""
        )
    except Exception:
        ig_posts = []
    try:
        inquiries = fetchall(
            """SELECT id, name, organisation, email, phone, reason, timeline, message,
                      status, reviewed_by, reviewed_at, notes, created_at
               FROM press_inquiries ORDER BY created_at DESC"""
        )
    except Exception:
        inquiries = []
    counts = {"new": 0, "reviewed": 0, "responded": 0, "archived": 0}
    for q in inquiries:
        s = q["status"] or "new"
        counts[s] = counts.get(s, 0) + 1
    return render_template("admin_press.html",
                           user=user, mentions=mentions, ig_posts=ig_posts,
                           inquiries=inquiries, counts=counts)


@app.route("/api/admin/press/mention", methods=["POST"])
@admin_required
def api_admin_press_mention_create():
    """Create a press mention."""
    _ensure_press_tables()
    d = request.json or {}
    title  = d.get("title", "").strip()
    source = d.get("source", "").strip()
    url    = d.get("url", "").strip()
    if not title or not source or not url:
        return jsonify({"error": "Title, source, and URL are required."}), 400
    try:
        mid = str(uuid.uuid4())
        with get_db() as db:
            db.execute(
                """INSERT INTO press_mentions
                   (id, title, source, url, summary, image_url, category, published_at, position, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (mid, title, source, url,
                 d.get("summary") or None, d.get("image_url") or None,
                 d.get("category") or None, d.get("published_at") or None,
                 int(d.get("position", 0) or 0))
            )
        return jsonify({"ok": True, "id": mid})
    except Exception as e:
        return jsonify({"error": "Create failed."}), 500


@app.route("/api/admin/press/mention/<mid>/delete", methods=["POST"])
@admin_required
def api_admin_press_mention_delete(mid):
    try:
        with get_db() as db:
            db.execute("DELETE FROM press_mentions WHERE id=?", (mid,))
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Delete failed."}), 500


@app.route("/api/admin/press/mention/<mid>/toggle", methods=["POST"])
@admin_required
def api_admin_press_mention_toggle(mid):
    try:
        with get_db() as db:
            db.execute("UPDATE press_mentions SET is_active=1-is_active WHERE id=?", (mid,))
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Toggle failed."}), 500


@app.route("/api/admin/press/instagram", methods=["POST"])
@admin_required
def api_admin_press_ig_create():
    """Create an Instagram post entry."""
    _ensure_press_tables()
    d = request.json or {}
    url       = d.get("url", "").strip()
    image_url = d.get("image_url", "").strip()
    if not url or not image_url:
        return jsonify({"error": "Post URL and image URL are required."}), 400
    try:
        pid = str(uuid.uuid4())
        with get_db() as db:
            db.execute(
                """INSERT INTO press_instagram_posts
                   (id, url, image_url, caption, position, is_active)
                   VALUES (?,?,?,?,?,1)""",
                (pid, url, image_url,
                 d.get("caption") or None,
                 int(d.get("position", 0) or 0))
            )
        return jsonify({"ok": True, "id": pid})
    except Exception:
        return jsonify({"error": "Create failed."}), 500


@app.route("/api/admin/press/instagram/<pid>/delete", methods=["POST"])
@admin_required
def api_admin_press_ig_delete(pid):
    try:
        with get_db() as db:
            db.execute("DELETE FROM press_instagram_posts WHERE id=?", (pid,))
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Delete failed."}), 500


@app.route("/api/admin/press/instagram/<pid>/toggle", methods=["POST"])
@admin_required
def api_admin_press_ig_toggle(pid):
    try:
        with get_db() as db:
            db.execute("UPDATE press_instagram_posts SET is_active=1-is_active WHERE id=?", (pid,))
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Toggle failed."}), 500


@app.route("/api/admin/press/inquiry/<iid>/status", methods=["POST"])
@admin_required
def api_admin_press_inquiry_status(iid):
    """Update inquiry status: new / reviewed / responded / archived."""
    d = request.json or {}
    new_status = d.get("status", "").strip()
    notes      = d.get("notes", "").strip() or None
    if new_status not in ("new", "reviewed", "responded", "archived"):
        return jsonify({"error": "Invalid status."}), 400
    try:
        with get_db() as db:
            db.execute(
                """UPDATE press_inquiries
                   SET status=?, reviewed_by=?, reviewed_at=datetime('now'), notes=?
                   WHERE id=?""",
                (new_status, session["user_id"], notes, iid)
            )
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Update failed."}), 500


@app.route("/admin/press/inquiries/export")
@admin_required
def admin_press_inquiries_export():
    import io, csv
    try:
        rows = fetchall(
            """SELECT name, organisation, email, phone, reason, timeline, message,
                      status, created_at FROM press_inquiries ORDER BY created_at DESC"""
        )
    except Exception:
        rows = []
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Organisation", "Email", "Phone", "Reason",
                     "Timeline", "Message", "Status", "Submitted"])
    for r in rows:
        writer.writerow([r["name"], r["organisation"], r["email"], r["phone"] or "",
                         r["reason"], r["timeline"] or "", r["message"] or "",
                         r["status"], r["created_at"][:16]])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sohana-press-inquiries.csv"})


# ── COMPLAINTS ───────────────────────────────────────────────────────────────

def _ensure_complaints_table():
    """Create complaints table on first use."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS complaints (
                id            TEXT PRIMARY KEY,
                reference     TEXT UNIQUE NOT NULL,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL,
                phone         TEXT,
                category      TEXT NOT NULL,
                rosca_name    TEXT,
                description   TEXT NOT NULL,
                evidence_url  TEXT,
                status        TEXT NOT NULL DEFAULT 'new',
                priority      TEXT NOT NULL DEFAULT 'normal',
                assigned_to   TEXT,
                resolution    TEXT,
                reviewed_by   TEXT,
                reviewed_at   TEXT,
                resolved_at   TEXT,
                user_id       TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_complaints_ref ON complaints(reference)")
    except Exception as e:
        import sys
        print(f"[_ensure_complaints_table] {e}", file=sys.stderr, flush=True)


def _gen_complaint_ref():
    """Generate a human-readable reference like SOH-CMP-2026-XXXX."""
    import datetime, secrets
    year = datetime.datetime.now().year
    suffix = secrets.token_hex(2).upper()  # 4 chars
    return f"SOH-CMP-{year}-{suffix}"


@app.route("/complaints")
def complaints_page():
    """Public complaints page."""
    _ensure_complaints_table()
    return render_template("complaints.html")


@app.route("/api/complaints/submit", methods=["POST"])
def api_complaints_submit():
    """Capture complaint submissions from the public page."""
    _ensure_complaints_table()
    d           = request.json or {}
    name        = d.get("name", "").strip()
    email       = d.get("email", "").strip().lower()
    phone       = d.get("phone", "").strip()
    category    = d.get("category", "").strip() or "other"
    rosca       = d.get("rosca", "").strip()
    description = d.get("description", "").strip()
    evidence    = d.get("evidence", "").strip()

    # Validation
    if not name or not email or not description:
        return jsonify({"error": "Name, email, and description are required."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "Please enter a valid email address."}), 400
    if len(description) < 20:
        return jsonify({"error": "Please provide at least a few sentences describing the issue."}), 400
    if category not in ("account", "transaction", "platform", "member", "data", "other"):
        category = "other"

    # Auto-set priority for data/security and account-freeze related
    priority = "normal"
    if category == "data":
        priority = "high"

    # If user is logged in, link the complaint to their account
    user_id = session.get("user_id")

    try:
        cid = str(uuid.uuid4())
        # Generate unique reference (retry on collision, max 5 tries)
        for _ in range(5):
            ref = _gen_complaint_ref()
            existing = fetchone("SELECT id FROM complaints WHERE reference=?", (ref,))
            if not existing:
                break
        with get_db() as db:
            db.execute("""INSERT INTO complaints
                          (id, reference, name, email, phone, category, rosca_name,
                           description, evidence_url, priority, user_id)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (cid, ref, name, email, phone or None, category,
                        rosca or None, description, evidence or None,
                        priority, user_id))
        return jsonify({"ok": True, "reference": ref})
    except Exception as e:
        import sys
        print(f"[complaints_submit] failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Submission failed. Please email complaints@sohana.app instead."}), 500


# ── COMPLAINTS ADMIN ─────────────────────────────────────────────────────────

@app.route("/admin/complaints")
@admin_required
def admin_complaints():
    """Admin view of complaints with status/priority workflow."""
    _ensure_complaints_table()
    user = auth.get_current_user()
    try:
        complaints = fetchall(
            """SELECT c.id, c.reference, c.name, c.email, c.phone, c.category,
                      c.rosca_name, c.description, c.evidence_url, c.status,
                      c.priority, c.resolution, c.reviewed_by, c.reviewed_at,
                      c.resolved_at, c.user_id, c.created_at,
                      r.full_name as reviewer_name
               FROM complaints c
               LEFT JOIN users r ON r.id = c.reviewed_by
               ORDER BY
                 CASE c.status
                   WHEN 'new' THEN 1
                   WHEN 'reviewing' THEN 2
                   WHEN 'escalated' THEN 3
                   WHEN 'resolved' THEN 4
                   WHEN 'closed' THEN 5
                   ELSE 6 END,
                 CASE c.priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 c.created_at DESC"""
        )
    except Exception:
        complaints = []
    counts = {"new": 0, "reviewing": 0, "resolved": 0, "escalated": 0, "closed": 0, "high": 0}
    for c in complaints:
        s = c["status"] or "new"
        counts[s] = counts.get(s, 0) + 1
        if c["priority"] == "high" and s in ("new", "reviewing"):
            counts["high"] += 1
    return render_template("admin_complaints.html",
                           user=user, complaints=complaints, counts=counts)


@app.route("/api/admin/complaints/<cid>/update", methods=["POST"])
@admin_required
def api_admin_complaints_update(cid):
    """Update complaint status, priority, or resolution."""
    d = request.json or {}
    status     = d.get("status", "").strip()
    priority   = d.get("priority", "").strip()
    resolution = d.get("resolution", "").strip()

    valid_statuses = ("new", "reviewing", "escalated", "resolved", "closed")
    valid_priority = ("low", "normal", "high")

    if status and status not in valid_statuses:
        return jsonify({"error": "Invalid status."}), 400
    if priority and priority not in valid_priority:
        return jsonify({"error": "Invalid priority."}), 400

    try:
        # Build update dynamically
        sets, params = [], []
        if status:
            sets.append("status=?"); params.append(status)
            if status == "resolved":
                sets.append("resolved_at=datetime('now')")
        if priority:
            sets.append("priority=?"); params.append(priority)
        if resolution:
            sets.append("resolution=?"); params.append(resolution)
        sets.append("reviewed_by=?"); params.append(session["user_id"])
        sets.append("reviewed_at=datetime('now')")
        params.append(cid)
        with get_db() as db:
            db.execute(f"UPDATE complaints SET {', '.join(sets)} WHERE id=?", params)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "Update failed."}), 500


@app.route("/admin/complaints/export")
@admin_required
def admin_complaints_export():
    import io, csv
    try:
        rows = fetchall(
            """SELECT reference, name, email, phone, category, rosca_name, description,
                      evidence_url, status, priority, resolution, created_at, resolved_at
               FROM complaints ORDER BY created_at DESC"""
        )
    except Exception:
        rows = []
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Reference", "Name", "Email", "Phone", "Category", "ROSCA",
                     "Description", "Evidence URL", "Status", "Priority",
                     "Resolution", "Submitted", "Resolved"])
    for r in rows:
        writer.writerow([r["reference"], r["name"], r["email"], r["phone"] or "",
                         r["category"], r["rosca_name"] or "", r["description"],
                         r["evidence_url"] or "", r["status"], r["priority"],
                         r["resolution"] or "",
                         r["created_at"][:16], r["resolved_at"][:16] if r["resolved_at"] else ""])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sohana-complaints.csv"})


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
    _platform_check("transfers_enabled", "Transfers are temporarily paused.")
    d = request.json or {}
    from_cur = d.get("from_currency","EUR")
    to_cur   = d.get("to_currency","GBP")
    amount   = int(float(d.get("amount", 0)) * 100)
    otp      = str(d.get("otp",""))
    # TOTP 2FA verification (replaces old random OTP system)
    _2fa_err = _require_totp(session.get("user_id",""), otp)
    if _2fa_err: return _2fa_err
    if False and (len(otp) != 6 or not otp.isdigit()):  # legacy check disabled
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
    _platform_check("deposits_enabled", "Deposits are temporarily paused. Please check back shortly.")
    d = request.json or {}
    cents    = int(float(d.get("amount", 0)) * 100)
    currency = d.get("currency", "EUR")
    if cents <= 0: return jsonify({"error": "Invalid amount"}), 400
    # ── Freeze check ──────────────────────────────────────────────────
    fd, fw, freason = _get_freeze_status(session["user_id"])
    if fd: return jsonify({"error": FROZEN_DEPOSIT_MSG, "frozen": True}), 403
    # ─────────────────────────────────────────────────────────────────
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
    _platform_check("withdrawals_enabled", "Withdrawals are temporarily paused. Please contact support@sohana.app.")
    d = request.json or {}
    cents    = int(float(d.get("amount", 0)) * 100)
    method   = d.get("method", "bank_eu")
    currency = d.get("currency", "EUR")
    otp      = str(d.get("otp",""))
    if len(otp) != 6 or not otp.isdigit():
        return jsonify({"error": "Invalid verification code"}), 400
    if cents <= 0: return jsonify({"error": "Invalid amount"}), 400
    # ── Freeze check ──────────────────────────────────────────────────
    fd, fw, freason = _get_freeze_status(session["user_id"])
    if fw: return jsonify({"error": FROZEN_WITHDRAW_MSG, "frozen": True}), 403
    # ─────────────────────────────────────────────────────────────────
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
    # ── Freeze check (Pay counts as outgoing transfer) ────────────────
    fd, fw, freason = _get_freeze_status(session["user_id"])
    if fw: return jsonify({"error": FROZEN_WITHDRAW_MSG, "frozen": True}), 403
    # ─────────────────────────────────────────────────────────────────
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
    return jsonify({
        "rates":      EXCHANGE_RATES,
        "base":       "EUR",
        "source":     EXCHANGE_RATES_META.get("source"),
        "updated_at": EXCHANGE_RATES_META.get("updated_at"),
    })

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
    # 2FA check for ROSCA contributions
    d_contrib = request.json or {}
    totp_c    = d_contrib.get("totp_code", "")
    _2fa_c    = _require_totp(session.get("user_id",""), totp_c)
    if _2fa_c: return _2fa_c
    try:
        cycle = rosca.get_or_create_active_cycle(rosca_id)
        rosca.pay_contribution(session["user_id"], cycle["id"])
        u = fetchone("SELECT full_name FROM users WHERE id=?", (session["user_id"],))
        r_info = fetchone("SELECT contribution_cents, currency FROM roscas WHERE id=?", (rosca_id,))
        amt = ""
        if r_info:
            sym = {"EUR":"€","GBP":"£","USD":"$","CAD":"C$","XAF":"Fr ","GHC":"₵","NGN":"₦","ZAR":"R"}
            amt = sym.get(r_info["currency"],"") + str(r_info["contribution_cents"]//100)
        _log_circle_activity(rosca_id, session["user_id"], "contribution_paid",
                             f"{u['full_name'] if u else 'A member'} contributed {amt}",
                             {"amount_cents": r_info["contribution_cents"] if r_info else 0})
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

# ── POOL PAGE ROUTES ─────────────────────────────────────────────────────────

@app.route("/pools")
@auth.login_required
def pools_page():
    user      = auth.get_current_user()
    my_pools  = pool.get_user_pools(user["id"])
    market    = pool.get_marketplace_pools(limit=6)
    purposes  = pool.POOL_PURPOSES
    schedules = pool.PAYMENT_SCHEDULES
    return render_template("pools.html", user=user, my_pools=my_pools,
                           market=market, purposes=purposes, schedules=schedules)

@app.route("/pools/<pool_id>")
@auth.login_required
def pool_detail(pool_id):
    user = auth.get_current_user()
    p    = pool.get_pool(pool_id)
    if not p: return redirect(url_for("pools_page"))
    members      = pool.get_pool_members(pool_id)
    disbursements= pool.get_disbursements(pool_id)
    admins       = pool.get_pool_admins(pool_id)
    is_member    = any(m["user_id"]==user["id"] and m["status"]=="active" for m in members)
    is_admin_m   = any(m["user_id"]==user["id"] and m["role"]=="admin" and m["status"]=="active" for m in members)
    my_status    = pool.get_member_contribution_status(pool_id, user["id"]) if is_member else None
    purposes     = pool.POOL_PURPOSES
    schedules    = pool.PAYMENT_SCHEDULES
    return render_template("pool_detail.html", user=user, pool=dict(p),
                           members=members, disbursements=disbursements,
                           admins=admins, is_member=is_member, is_admin=is_admin_m,
                           my_status=my_status, purposes=purposes, schedules=schedules)

@app.route("/pools/<pool_id>/manage")
@auth.login_required
def pool_manage(pool_id):
    user = auth.get_current_user()
    p    = pool.get_pool(pool_id)
    if not p: return redirect(url_for("pools_page"))
    # Must be admin
    admins = pool.get_pool_admins(pool_id)
    if not any(a["user_id"]==user["id"] for a in admins):
        return redirect(url_for("pool_detail", pool_id=pool_id))
    members      = pool.get_pool_members(pool_id)
    pending      = pool.get_pending_pool_members(pool_id)
    disbursements= pool.get_disbursements(pool_id)
    summary      = pool.get_pool_contribution_summary(pool_id)
    report       = pool.get_pool_report(pool_id)
    purposes     = pool.POOL_PURPOSES
    schedules    = pool.PAYMENT_SCHEDULES
    return render_template("pool_manage.html", user=user, pool=dict(p),
                           members=members, pending=pending, admins=admins,
                           disbursements=disbursements, summary=summary,
                           report=report, purposes=purposes, schedules=schedules)

# ── POOL API ──────────────────────────────────────────────────────────────────

@app.route("/api/pools/create", methods=["POST"])
@auth.login_required
def api_create_pool():
    d = request.json or {}
    try:
        annual = int(float(d.get("annual_amount", 600)) * 100)
        pid, fee = pool.create_pool(
            organiser_id=session["user_id"],
            name=d.get("name","").strip(),
            description=d.get("description",""),
            purpose=d.get("purpose","general"),
            annual_amount_cents=annual,
            duration_months=int(d.get("duration_months", 12)),
            payout_type=d.get("payout_type","single"),
            currency=d.get("currency","EUR"),
            ncs_min=int(d.get("ncs_min", 300)),
            is_public=bool(d.get("is_public", False)),
        )
        if fee > 0:
            w = _get_wallet(session["user_id"], "EUR")
            if w and wallet_balance(w["id"]) >= fee:
                post_transaction(w["id"], -fee, f"Pool creation fee: {d.get('name','')}", tx_type="fee")
        return jsonify({"ok": True, "pool_id": pid, "fee_cents": fee})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/join", methods=["POST"])
@auth.login_required
def api_join_pool(pool_id):
    d = request.json or {}
    try:
        pool.request_to_join_pool(pool_id, session["user_id"],
                                  d.get("payment_schedule", "monthly"))
        return jsonify({"ok": True, "status": "pending"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/approve/<user_id>", methods=["POST"])
@auth.login_required
def api_approve_pool_member(pool_id, user_id):
    try:
        pool.approve_pool_member(pool_id, user_id, session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/reject/<user_id>", methods=["POST"])
@auth.login_required
def api_reject_pool_member(pool_id, user_id):
    try:
        pool.reject_pool_member(pool_id, user_id, session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/remove/<user_id>", methods=["POST"])
@auth.login_required
def api_remove_pool_member(pool_id, user_id):
    d = request.json or {}
    try:
        pool.remove_pool_member(pool_id, user_id, session["user_id"], d.get("reason",""))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/promote/<user_id>", methods=["POST"])
@auth.login_required
def api_promote_pool_admin(pool_id, user_id):
    try:
        pool.promote_to_admin(pool_id, user_id, session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/schedule", methods=["POST"])
@auth.login_required
def api_update_pool_schedule(pool_id):
    d = request.json or {}
    try:
        pool.update_payment_schedule(pool_id, session["user_id"], d.get("schedule","monthly"))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/contribute", methods=["POST"])
@auth.login_required
def api_pool_contribute(pool_id):
    d = request.json or {}
    beneficiary_id = d.get("beneficiary_id", session["user_id"])
    months         = int(d.get("months", 1))
    if months not in [1, 3, 6, 12]:
        return jsonify({"error": "Months must be 1, 3, 6, or 12"}), 400
    try:
        cid = pool.pay_pool_contribution(pool_id, session["user_id"], beneficiary_id,
                                         months, note=d.get("note",""))
        return jsonify({"ok": True, "contribution_id": cid})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/disburse", methods=["POST"])
@auth.login_required
def api_request_disbursement(pool_id):
    d = request.json or {}
    try:
        did = pool.request_disbursement(
            pool_id, session["user_id"],
            int(float(d.get("amount", 0)) * 100),
            d.get("purpose_note",""),
            d.get("recipient_id")
        )
        return jsonify({"ok": True, "disbursement_id": did})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/disburse/<disbursement_id>/approve", methods=["POST"])
@auth.login_required
def api_approve_disbursement(pool_id, disbursement_id):
    try:
        pool.approve_disbursement(pool_id, disbursement_id, session["user_id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/disburse/<disbursement_id>/reject", methods=["POST"])
@auth.login_required
def api_reject_disbursement(pool_id, disbursement_id):
    d = request.json or {}
    try:
        pool.reject_disbursement(pool_id, disbursement_id, session["user_id"], d.get("note",""))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/pools/<pool_id>/report")
@auth.login_required
def api_pool_report(pool_id):
    p = pool.get_pool(pool_id)
    if not p: return jsonify({"error": "Not found"}), 404
    return jsonify({"report": pool.get_pool_report(pool_id)})

@app.route("/api/pools/<pool_id>/report/csv")
@auth.login_required
def api_pool_report_csv(pool_id):
    import io, csv as csv_mod
    p = pool.get_pool(pool_id)
    if not p: return jsonify({"error": "Not found"}), 404
    report = pool.get_pool_report(pool_id)
    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow(["Member","Role","Months Covered","Total Paid (€)","Received Help (€)","Helped Others (€)"])
    for m in report["members"]:
        writer.writerow([m["full_name"], m["role"].title(),
                         m["months_covered"], f"{m['total_paid_cents']/100:.2f}",
                         f"{m['received_help_cents']/100:.2f}",
                         f"{m['helped_others_cents']/100:.2f}"])
    output.seek(0)
    name = p["name"].replace(" ","-").lower()
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=pool-report-{name}.csv"})

# ── ADMIN FREEZE CONTROLS ─────────────────────────────────────────────────────

@app.route("/api/admin/freeze", methods=["POST"])
@admin_required
def api_admin_freeze():
    """
    Freeze deposits and/or withdrawals for a user.
    Authorized roles: CEO, CCO, CFO.
    Only CEO can freeze another admin.
    """
    d          = request.json or {}
    target_id  = d.get("user_id","").strip()
    freeze_dep = bool(d.get("freeze_deposits", False))
    freeze_wd  = bool(d.get("freeze_withdrawals", False))
    reason     = d.get("reason","").strip()

    if not target_id: return jsonify({"error": "user_id required"}), 400
    if not reason:    return jsonify({"error": "A reason is required"}), 400
    if not freeze_dep and not freeze_wd:
        return jsonify({"error": "Select at least one: deposits or withdrawals"}), 400

    # Check actor role
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in FREEZE_AUTHORIZED_ROLES:
        return jsonify({"error": "Insufficient privileges. Only CEO, CCO, and CFO can freeze accounts."}), 403

    # Check target exists and whether they are an admin
    target = fetchone("SELECT id, full_name, is_admin, admin_role FROM users WHERE id=?", (target_id,))
    if not target: return jsonify({"error": "User not found"}), 404

    if not _can_freeze(actor["admin_role"], bool(target["is_admin"])):
        return jsonify({"error": "Only the CEO can restrict another admin account."}), 403

    # Prevent self-freeze
    if target_id == session["user_id"]:
        return jsonify({"error": "You cannot freeze your own account"}), 400

    with get_db() as db:
        db.execute("""UPDATE users SET
                      freeze_deposits=?, freeze_withdrawals=?,
                      freeze_reason=?, frozen_by=?, frozen_at=datetime('now')
                      WHERE id=?""",
                   (1 if freeze_dep else 0, 1 if freeze_wd else 0,
                    reason, session["user_id"], target_id))
        # Audit log
        db.execute("""INSERT INTO freeze_log(id,target_user_id,admin_id,action,freeze_type,reason)
                      VALUES(?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), target_id, session["user_id"],
                    "freeze",
                    ("deposits+withdrawals" if freeze_dep and freeze_wd
                     else "deposits" if freeze_dep else "withdrawals"),
                    reason))

    # Notify the affected user
    frozen_what = []
    if freeze_dep: frozen_what.append("deposits")
    if freeze_wd:  frozen_what.append("withdrawals")
    push_notification(target_id,
                      "Account restriction applied",
                      f"Your {' and '.join(frozen_what)} have been restricted. "
                      f"Please contact support@sohana.app to resolve this.",
                      "danger")

    log_admin_action("user_freeze", "user", target_id,
                     new_data={"freeze_deposits": freeze_dep, "freeze_withdrawals": freeze_wd},
                     reason=reason)
    return jsonify({"ok": True, "target_name": target["full_name"]})


@app.route("/api/admin/unfreeze", methods=["POST"])
@admin_required
def api_admin_unfreeze():
    """Lift deposits and/or withdrawals freeze for a user."""
    d          = request.json or {}
    target_id  = d.get("user_id","").strip()
    unfreeze_dep = bool(d.get("unfreeze_deposits", False))
    unfreeze_wd  = bool(d.get("unfreeze_withdrawals", False))
    reason       = d.get("reason","").strip()

    if not target_id: return jsonify({"error": "user_id required"}), 400
    if not unfreeze_dep and not unfreeze_wd:
        return jsonify({"error": "Select at least one to unfreeze"}), 400

    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in FREEZE_AUTHORIZED_ROLES:
        return jsonify({"error": "Insufficient privileges"}), 403

    target = fetchone("SELECT id, full_name, is_admin FROM users WHERE id=?", (target_id,))
    if not target: return jsonify({"error": "User not found"}), 404
    if not _can_freeze(actor["admin_role"], bool(target["is_admin"])):
        return jsonify({"error": "Only the CEO can modify restrictions on admin accounts."}), 403

    with get_db() as db:
        if unfreeze_dep and unfreeze_wd:
            db.execute("""UPDATE users SET freeze_deposits=0, freeze_withdrawals=0,
                          freeze_reason=NULL, frozen_by=NULL, frozen_at=NULL WHERE id=?""", (target_id,))
        elif unfreeze_dep:
            db.execute("UPDATE users SET freeze_deposits=0 WHERE id=?", (target_id,))
        else:
            db.execute("UPDATE users SET freeze_withdrawals=0 WHERE id=?", (target_id,))

        # Update freeze_reason if both now clear
        current = fetchone("SELECT freeze_deposits, freeze_withdrawals FROM users WHERE id=?", (target_id,))
        if current and not current["freeze_deposits"] and not current["freeze_withdrawals"]:
            db.execute("UPDATE users SET freeze_reason=NULL, frozen_by=NULL, frozen_at=NULL WHERE id=?", (target_id,))

        db.execute("""INSERT INTO freeze_log(id,target_user_id,admin_id,action,freeze_type,reason)
                      VALUES(?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), target_id, session["user_id"],
                    "unfreeze",
                    ("deposits+withdrawals" if unfreeze_dep and unfreeze_wd
                     else "deposits" if unfreeze_dep else "withdrawals"),
                    reason or "Restriction lifted"))

    unfrozen_what = []
    if unfreeze_dep: unfrozen_what.append("deposits")
    if unfreeze_wd:  unfrozen_what.append("withdrawals")
    push_notification(target_id,
                      "Account restriction lifted ✓",
                      f"Your {' and '.join(unfrozen_what)} restriction has been removed.",
                      "success")

    return jsonify({"ok": True, "target_name": target["full_name"]})


@app.route("/api/admin/freeze-log")
@admin_required
def api_freeze_log():
    """Full audit log of all freeze/unfreeze actions."""
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in FREEZE_AUTHORIZED_ROLES:
        return jsonify({"error": "Insufficient privileges"}), 403
    logs = fetchall("""SELECT fl.*, u.full_name as target_name, a.full_name as admin_name, a.admin_role
                       FROM freeze_log fl
                       JOIN users u ON u.id=fl.target_user_id
                       JOIN users a ON a.id=fl.admin_id
                       ORDER BY fl.created_at DESC LIMIT 100""")
    return jsonify({"logs": [dict(l) for l in logs]})


@app.route("/admin/freeze")
@admin_required
def admin_freeze_panel():
    """Dedicated freeze management page for CEO/CCO/CFO."""
    user  = auth.get_current_user()
    actor_role = user["admin_role"] if user else ""
    if actor_role not in FREEZE_AUTHORIZED_ROLES:
        return redirect(url_for("admin_home"))

    # All users with their freeze status
    users = fetchall("""SELECT id, full_name, phone, email, hanatag, ncs_score, ncs_tier,
                               is_admin, admin_role, freeze_deposits, freeze_withdrawals,
                               freeze_reason, frozen_at
                        FROM users ORDER BY is_admin DESC, full_name""")

    # Freeze log
    logs = fetchall("""SELECT fl.*, u.full_name as target_name, a.full_name as admin_name, a.admin_role
                       FROM freeze_log fl
                       JOIN users u ON u.id=fl.target_user_id
                       JOIN users a ON a.id=fl.admin_id
                       ORDER BY fl.created_at DESC LIMIT 50""")

    # Stats
    frozen_dep_count = sum(1 for u in users if u["freeze_deposits"])
    frozen_wd_count  = sum(1 for u in users if u["freeze_withdrawals"])

    return render_template("admin_freeze.html", user=user, all_users=users,
                           logs=logs, actor_role=actor_role,
                           frozen_dep_count=frozen_dep_count,
                           frozen_wd_count=frozen_wd_count)


# ── CAMPAIGN PAGE ROUTES ─────────────────────────────────────────────────────

@app.route("/campaigns")
def campaigns_page():
    """Public browse page — no login required."""
    user     = auth.get_current_user() if "user_id" in session else None
    category = request.args.get("category","")
    search   = request.args.get("q","")
    campaigns_list = campaign.browse_campaigns(
        category=category or None,
        search=search or None,
        limit=24
    )
    categories = campaign.CAMPAIGN_CATEGORIES
    stats      = campaign.get_campaign_stats()
    return render_template("campaigns.html", user=user,
                           campaigns=campaigns_list, categories=categories,
                           stats=stats, active_category=category, search=search)


@app.route("/campaigns/<slug>")
def campaign_detail(slug):
    """Public campaign page — shareable, no login needed to view."""
    user = auth.get_current_user() if "user_id" in session else None
    c    = campaign.get_campaign(slug=slug)
    if not c: return redirect(url_for("campaigns_page"))
    donations  = campaign.get_donations(c["id"], limit=20)
    top_donors = campaign.get_top_donors(c["id"])
    is_creator = user and user["id"] == c["creator_id"]
    categories = campaign.CAMPAIGN_CATEGORIES
    pct = min(100, round(c["raised_cents"] / max(c["goal_cents"], 1) * 100))
    return render_template("campaign_detail.html", user=user, campaign=dict(c),
                           donations=donations, top_donors=top_donors,
                           is_creator=is_creator, categories=categories, pct=pct)


@app.route("/campaigns/<slug>/manage")
@auth.login_required
def campaign_manage(slug):
    """Creator-only management page."""
    user = auth.get_current_user()
    c    = campaign.get_campaign(slug=slug)
    if not c or c["creator_id"] != user["id"]:
        return redirect(url_for("campaign_detail", slug=slug))
    donations  = campaign.get_donations(c["id"], limit=100)
    top_donors = campaign.get_top_donors(c["id"])
    categories = campaign.CAMPAIGN_CATEGORIES
    pct        = min(100, round(c["raised_cents"] / max(c["goal_cents"], 1) * 100))
    available  = c["raised_cents"] - c["withdrawn_cents"]
    return render_template("campaign_manage.html", user=user, campaign=dict(c),
                           donations=donations, top_donors=top_donors,
                           categories=categories, pct=pct, available=available)


@app.route("/my-campaigns")
@auth.login_required
def my_campaigns():
    user = auth.get_current_user()
    my   = campaign.get_user_campaigns(user["id"])
    categories = campaign.CAMPAIGN_CATEGORIES
    return render_template("campaigns.html", user=user, campaigns=None,
                           my_campaigns=my, categories=categories,
                           stats=campaign.get_campaign_stats(),
                           active_category="", search="")


# ── CAMPAIGN API ──────────────────────────────────────────────────────────────

@app.route("/api/campaigns/create", methods=["POST"])
@auth.login_required
def api_create_campaign():
    d = request.json or {}
    try:
        goal = int(float(d.get("goal", 0)) * 100)
        cid, slug = campaign.create_campaign(
            creator_id=session["user_id"],
            title=d.get("title","").strip(),
            story=d.get("story","").strip(),
            category=d.get("category","personal"),
            goal_cents=goal,
            currency=d.get("currency","EUR"),
            deadline=d.get("deadline") or None,
            is_public=bool(d.get("is_public", True)),
            allow_anonymous=bool(d.get("allow_anonymous", True)),
        )
        return jsonify({"ok": True, "campaign_id": cid, "slug": slug})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/campaigns/<campaign_id>/donate", methods=["POST"])
@auth.login_required
def api_donate(campaign_id):
    d = request.json or {}
    cents = int(float(d.get("amount", 0)) * 100)
    try:
        did, net, fee = campaign.donate(
            campaign_id=campaign_id,
            amount_cents=cents,
            donor_id=session["user_id"],
            message=d.get("message",""),
            is_anonymous=bool(d.get("is_anonymous", False)),
        )
        return jsonify({"ok": True, "donation_id": did, "net_cents": net, "fee_cents": fee})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/campaigns/<campaign_id>/withdraw", methods=["POST"])
@auth.login_required
def api_campaign_withdraw(campaign_id):
    d = request.json or {}
    cents = int(float(d.get("amount", 0)) * 100)
    try:
        ref = campaign.withdraw_funds(campaign_id, session["user_id"], cents)
        return jsonify({"ok": True, "ref": ref})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/campaigns/<campaign_id>/update", methods=["POST"])
@auth.login_required
def api_update_campaign(campaign_id):
    d = request.json or {}
    try:
        campaign.update_campaign(campaign_id, session["user_id"],
                                 title=d.get("title"),
                                 story=d.get("story"),
                                 deadline=d.get("deadline"),
                                 is_public=d.get("is_public"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/campaigns/<campaign_id>/close", methods=["POST"])
@auth.login_required
def api_close_campaign(campaign_id):
    try:
        campaign.close_campaign(campaign_id, session["user_id"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── ADMIN CAMPAIGN ROUTES ─────────────────────────────────────────────────────

@app.route("/admin/campaigns")
@admin_required
def admin_campaigns_page():
    user  = auth.get_current_user()
    status_filter = request.args.get("status","")
    all_c = campaign.get_all_campaigns(status=status_filter or None)
    stats = campaign.get_campaign_stats()
    return render_template("admin_campaigns.html", user=user,
                           campaigns=all_c, stats=stats,
                           status_filter=status_filter,
                           categories=campaign.CAMPAIGN_CATEGORIES)


@app.route("/api/admin/campaigns/<campaign_id>/flag", methods=["POST"])
@admin_required
def api_admin_flag_campaign(campaign_id):
    d = request.json or {}
    try:
        campaign.admin_flag_campaign(campaign_id, session["user_id"], d.get("reason","Policy violation"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/admin/campaigns/<campaign_id>/restore", methods=["POST"])
@admin_required
def api_admin_restore_campaign(campaign_id):
    try:
        campaign.admin_restore_campaign(campaign_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400




# ── PLATFORM CONTROLS API ─────────────────────────────────────────────────────

@app.route("/api/admin/platform-controls", methods=["GET"])
@admin_required
def api_platform_controls_get():
    """Return current platform control state."""
    ctrl = _get_platform_controls()
    return jsonify(ctrl)


@app.route("/api/admin/platform-controls", methods=["POST"])
@admin_required
def api_platform_controls_set():
    """CEO-only: toggle a platform control flag."""
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] != "ceo":
        return jsonify({"error": "Only the CEO can modify platform-wide controls."}), 403

    d      = request.json or {}
    flag   = d.get("flag", "").strip()
    value  = int(bool(d.get("value", True)))
    reason = d.get("reason", "").strip()

    VALID_FLAGS = {"deposits_enabled","withdrawals_enabled","transfers_enabled",
                   "rosca_payouts_enabled","new_registrations_enabled","maintenance_mode"}
    if flag not in VALID_FLAGS:
        return jsonify({"error": f"Unknown control flag: {flag}"}), 400
    if not reason:
        return jsonify({"error": "A reason is required for every platform control change."}), 400

    ctrl = _get_platform_controls()
    prev = ctrl.get(flag)

    try:
        with get_db() as db:
            db.execute(f"UPDATE platform_controls SET {flag}=?, updated_by_admin_id=?, updated_at=datetime('now'), reason=? WHERE id=1",
                       (value, session["user_id"], reason))
        log_admin_action(
            action_type=f"platform_control_{'enabled' if value else 'disabled'}",
            entity_type="platform",
            entity_id=flag,
            previous_data={flag: prev},
            new_data={flag: value},
            reason=reason
        )
        return jsonify({"ok": True, "flag": flag, "value": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── USER ACTION API ───────────────────────────────────────────────────────────

@app.route("/api/admin/users/<user_id>/action", methods=["POST"])
@admin_required
def api_admin_user_action(user_id):
    """
    Unified user action endpoint.
    action: suspend | unsuspend | force_kyc | add_note | set_risk_level | view
    """
    d       = request.json or {}
    action  = d.get("action", "").strip()
    reason  = d.get("reason", "").strip()
    note    = d.get("note", "").strip()

    target = fetchone("SELECT id, full_name, is_admin, admin_role FROM users WHERE id=?", (user_id,))
    if not target:
        return jsonify({"error": "User not found"}), 404

    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor:
        return jsonify({"error": "Actor not found"}), 403

    # Only CEO can act on admins
    if target["is_admin"] and actor["admin_role"] != "ceo":
        return jsonify({"error": "Only the CEO can take actions on admin accounts."}), 403

    # ── SUSPEND / UNSUSPEND ──
    if action in ("suspend", "unsuspend"):
        if actor["admin_role"] not in {"ceo", "cco", "compliance"}:
            return jsonify({"error": "Only CEO, CCO, or Compliance can suspend accounts."}), 403
        if not reason:
            return jsonify({"error": "Reason required to suspend an account."}), 400
        new_val = 1 if action == "suspend" else 0
        prev = fetchone("SELECT is_suspended FROM users WHERE id=?", (user_id,))
        try:
            with get_db() as db:
                db.execute("UPDATE users SET is_suspended=? WHERE id=?", (new_val, user_id))
            push_notification(user_id,
                "Account suspended" if new_val else "Account reinstated",
                reason if new_val else "Your account has been reinstated. Contact support@sohana.app for details.",
                "danger" if new_val else "success")
            log_admin_action(f"user_{action}", "user", user_id,
                             previous_data={"is_suspended": not new_val},
                             new_data={"is_suspended": bool(new_val)}, reason=reason)
            return jsonify({"ok": True, "action": action, "user": target["full_name"]})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── FORCE KYC REVIEW ──
    elif action == "force_kyc":
        if actor["admin_role"] not in {"ceo", "cco", "cfo", "compliance", "fraud"}:
            return jsonify({"error": "Insufficient privileges."}), 403
        try:
            with get_db() as db:
                db.execute("UPDATE users SET kyc_status='pending' WHERE id=?", (user_id,))
            log_admin_action("user_force_kyc", "user", user_id,
                             reason=reason or "Manual KYC review requested by admin")
            return jsonify({"ok": True, "message": f"{target['full_name']} moved to KYC review queue."})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── ADD NOTE ──
    elif action == "add_note":
        if not note:
            return jsonify({"error": "Note text is required."}), 400
        _ensure_admin_notes()
        try:
            with get_db() as db:
                db.execute("INSERT INTO admin_notes(id,admin_id,entity_type,entity_id,note) VALUES(?,?,?,?,?)",
                           (str(uuid.uuid4()), session["user_id"], "user", user_id, note))
            log_admin_action("admin_note_added", "user", user_id, new_data={"note": note[:100]})
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── SET RISK LEVEL ──
    elif action == "set_risk_level":
        level = d.get("level", "").strip()
        if level not in {"low", "medium", "high", "very_high", "blocked"}:
            return jsonify({"error": "Invalid risk level"}), 400
        if actor["admin_role"] not in {"ceo", "fraud", "compliance"}:
            return jsonify({"error": "Insufficient privileges."}), 403
        prev = fetchone("SELECT risk_level FROM users WHERE id=?", (user_id,))
        try:
            with get_db() as db:
                db.execute("UPDATE users SET risk_level=? WHERE id=?", (level, user_id))
            log_admin_action("user_risk_level_changed", "user", user_id,
                             previous_data={"risk_level": prev["risk_level"] if prev else None},
                             new_data={"risk_level": level}, reason=reason)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    else:
        return jsonify({"error": f"Unknown action: {action}"}), 400


# ── ADMIN NOTES API ───────────────────────────────────────────────────────────

@app.route("/api/admin/notes/<entity_type>/<entity_id>", methods=["GET"])
@admin_required
def api_admin_notes_get(entity_type, entity_id):
    _ensure_admin_notes()
    notes = fetchall("""SELECT n.*, u.full_name as admin_name, u.admin_role
                        FROM admin_notes n JOIN users u ON u.id=n.admin_id
                        WHERE n.entity_type=? AND n.entity_id=?
                        ORDER BY n.created_at DESC LIMIT 50""",
                     (entity_type, entity_id))
    return jsonify({"notes": [dict(n) for n in notes]})


@app.route("/api/admin/notes/<entity_type>/<entity_id>", methods=["POST"])
@admin_required
def api_admin_notes_post(entity_type, entity_id):
    _ensure_admin_notes()
    note = (request.json or {}).get("note", "").strip()
    if not note:
        return jsonify({"error": "Note text is required."}), 400
    try:
        with get_db() as db:
            db.execute("INSERT INTO admin_notes(id,admin_id,entity_type,entity_id,note) VALUES(?,?,?,?,?)",
                       (str(uuid.uuid4()), session["user_id"], entity_type, entity_id, note))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── AUDIT LOG API ─────────────────────────────────────────────────────────────

@app.route("/admin/audit-log")
@admin_required
def admin_audit_log():
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in {"ceo", "cto", "compliance", "cco"}:
        return redirect(url_for("admin_home"))
    _ensure_audit_log()
    user = auth.get_current_user()
    logs = fetchall("""SELECT al.*, u.full_name as admin_name
                       FROM admin_audit_logs al LEFT JOIN users u ON u.id=al.admin_id
                       ORDER BY al.created_at DESC LIMIT 200""")
    return render_template("admin_audit_log.html", user=user, logs=logs)


@app.route("/api/admin/audit-log")
@admin_required
def api_admin_audit_log():
    _ensure_audit_log()
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in {"ceo", "cto", "cco", "compliance"}:
        return jsonify({"error": "Insufficient privileges"}), 403
    entity_type = request.args.get("entity_type")
    entity_id   = request.args.get("entity_id")
    limit       = min(int(request.args.get("limit", 100)), 500)
    if entity_type and entity_id:
        logs = fetchall("SELECT * FROM admin_audit_logs WHERE entity_type=? AND entity_id=? ORDER BY created_at DESC LIMIT ?",
                        (entity_type, entity_id, limit))
    else:
        logs = fetchall("SELECT * FROM admin_audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))
    return jsonify({"logs": [dict(l) for l in logs]})


# ── SYSTEM HEALTH API ─────────────────────────────────────────────────────────

@app.route("/admin/system-health")
@app.route("/api/admin/system-health")
@admin_required
def admin_system_health():
    import time
    results = {}

    # Database
    try:
        t = time.time()
        fetchone("SELECT 1")
        results["database"] = {"status": "healthy", "latency_ms": round((time.time()-t)*1000, 1)}
    except Exception as e:
        results["database"] = {"status": "degraded", "error": str(e)}

    # App itself
    results["api"] = {"status": "healthy", "version": "6.5"}

    # Platform controls
    ctrl = _get_platform_controls()
    results["platform_controls"] = {
        "status": "healthy" if not ctrl.get("maintenance_mode") else "maintenance",
        "deposits_enabled":      bool(ctrl.get("deposits_enabled",1)),
        "withdrawals_enabled":   bool(ctrl.get("withdrawals_enabled",1)),
        "transfers_enabled":     bool(ctrl.get("transfers_enabled",1)),
        "rosca_payouts_enabled": bool(ctrl.get("rosca_payouts_enabled",1)),
        "registrations_enabled": bool(ctrl.get("new_registrations_enabled",1)),
        "maintenance_mode":      bool(ctrl.get("maintenance_mode",0)),
    }

    # Key table row counts (quick DB health signal)
    results["data"] = {
        "users":        fetchone("SELECT COUNT(*) as c FROM users")["c"],
        "wallets":      fetchone("SELECT COUNT(*) as c FROM wallets")["c"],
        "transactions": fetchone("SELECT COUNT(*) as c FROM wallet_transactions")["c"],
        "open_alerts":  fetchone("SELECT COUNT(*) as c FROM fraud_alerts WHERE status='open'")[["c"]] if _table_exists("fraud_alerts") else 0,
    }

    overall = "healthy"
    if results["database"]["status"] != "healthy":
        overall = "degraded"
    if ctrl.get("maintenance_mode"):
        overall = "maintenance"

    results["overall"] = overall
    results["checked_at"] = __import__("datetime").datetime.utcnow().isoformat()

    if request.path == "/admin/system-health":
        user = auth.get_current_user()
        return render_template("admin_system_health.html", user=user, health=results)
    return jsonify(results)


# ── TRANSACTION FLAG/REVERSE API ──────────────────────────────────────────────

@app.route("/api/admin/transactions/<tx_id>/flag", methods=["POST"])
@admin_required
def api_admin_flag_transaction(tx_id):
    d      = request.json or {}
    reason = d.get("reason", "").strip()
    if not reason:
        return jsonify({"error": "Reason required to flag a transaction."}), 400
    tx = fetchone("SELECT * FROM wallet_transactions WHERE id=?", (tx_id,))
    if not tx:
        return jsonify({"error": "Transaction not found"}), 404
    try:
        with get_db() as db:
            db.execute("UPDATE wallet_transactions SET flagged_for_review=1, flag_reason=? WHERE id=?",
                       (reason, tx_id))
        log_admin_action("transaction_flagged", "transaction", tx_id, reason=reason)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/transactions/<tx_id>/reverse", methods=["POST"])
@admin_required
def api_admin_reverse_transaction(tx_id):
    actor = fetchone("SELECT admin_role FROM users WHERE id=?", (session["user_id"],))
    if not actor or actor["admin_role"] not in {"ceo", "cfo", "operations"}:
        return jsonify({"error": "Only CEO, CFO, or Operations can reverse transactions."}), 403

    d      = request.json or {}
    reason = d.get("reason", "").strip()
    if not reason:
        return jsonify({"error": "Reason required to reverse a transaction."}), 400

    tx = fetchone("SELECT * FROM wallet_transactions WHERE id=?", (tx_id,))
    if not tx:
        return jsonify({"error": "Transaction not found"}), 404

    # Only reverse pending or failed — never settled external transfers
    if tx["status"] not in ("pending", "failed"):
        return jsonify({"error": f"Cannot reverse a transaction with status '{tx['status']}'. Only pending or failed transactions are reversible."}), 400

    try:
        with get_db() as db:
            db.execute("UPDATE wallet_transactions SET status='reversed', reversed_by=?, reversed_at=datetime('now') WHERE id=?",
                       (session["user_id"], tx_id))
        log_admin_action("transaction_reversed", "transaction", tx_id,
                         previous_data={"status": tx["status"]},
                         new_data={"status": "reversed"}, reason=reason)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ── CIRCLE SOCIAL INFRASTRUCTURE ──────────────────────────────────────────────

def _ensure_circle_tables():
    """Create circle_activity, circle_messages, circle_announcements, circle_votes on first use."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS circle_activity (
                id TEXT PRIMARY KEY,
                rosca_id TEXT NOT NULL,
                actor_id TEXT,
                type TEXT NOT NULL,
                body TEXT,
                metadata TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ca_rosca ON circle_activity(rosca_id, created_at DESC)")

            db.execute("""CREATE TABLE IF NOT EXISTS circle_messages (
                id TEXT PRIMARY KEY,
                rosca_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                message TEXT NOT NULL,
                is_pinned INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_cm_rosca ON circle_messages(rosca_id, created_at DESC)")

            db.execute("""CREATE TABLE IF NOT EXISTS circle_announcements (
                id TEXT PRIMARY KEY,
                rosca_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                priority TEXT DEFAULT 'normal',
                is_pinned INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ann_rosca ON circle_announcements(rosca_id, created_at DESC)")

            db.execute("""CREATE TABLE IF NOT EXISTS circle_votes (
                id TEXT PRIMARY KEY,
                rosca_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                question TEXT NOT NULL,
                type TEXT DEFAULT 'simple_majority',
                status TEXT DEFAULT 'open',
                expires_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS circle_vote_responses (
                id TEXT PRIMARY KEY,
                vote_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
    except Exception as e:
        import sys; print(f"[_ensure_circle_tables] {e}", file=sys.stderr, flush=True)


def _log_circle_activity(rosca_id, actor_id, event_type, body, metadata=None):
    """Write one activity event to circle_activity."""
    _ensure_circle_tables()
    try:
        import json as _json
        with get_db() as db:
            db.execute("""INSERT INTO circle_activity(id,rosca_id,actor_id,type,body,metadata)
                          VALUES(?,?,?,?,?,?)""",
                       (str(uuid.uuid4()), rosca_id, actor_id, event_type, body,
                        _json.dumps(metadata) if metadata else None))
    except Exception as e:
        import sys; print(f"[_log_circle_activity] {e}", file=sys.stderr, flush=True)


# ── CIRCLE ACTIVITY API ────────────────────────────────────────────────────────

@app.route("/api/rosca/<rosca_id>/activity")
@auth.login_required
def api_circle_activity(rosca_id):
    _ensure_circle_tables()
    limit  = min(int(request.args.get("limit", 30)), 100)
    before = request.args.get("before")
    if before:
        rows = fetchall("""SELECT ca.*, u.full_name, u.hanatag
                           FROM circle_activity ca
                           LEFT JOIN users u ON u.id=ca.actor_id
                           WHERE ca.rosca_id=? AND ca.created_at < ?
                           ORDER BY ca.created_at DESC LIMIT ?""",
                        (rosca_id, before, limit))
    else:
        rows = fetchall("""SELECT ca.*, u.full_name, u.hanatag
                           FROM circle_activity ca
                           LEFT JOIN users u ON u.id=ca.actor_id
                           WHERE ca.rosca_id=?
                           ORDER BY ca.created_at DESC LIMIT ?""",
                        (rosca_id, limit))
    return jsonify({"activities": [dict(r) for r in rows]})


# ── CIRCLE CHAT API ────────────────────────────────────────────────────────────

@app.route("/api/rosca/<rosca_id>/messages")
@auth.login_required
def api_circle_messages(rosca_id):
    _ensure_circle_tables()
    limit  = min(int(request.args.get("limit", 40)), 100)
    before = request.args.get("before")
    if before:
        rows = fetchall("""SELECT cm.*, u.full_name, u.hanatag
                           FROM circle_messages cm JOIN users u ON u.id=cm.sender_id
                           WHERE cm.rosca_id=? AND cm.created_at < ?
                           ORDER BY cm.created_at DESC LIMIT ?""",
                        (rosca_id, before, limit))
    else:
        rows = fetchall("""SELECT cm.*, u.full_name, u.hanatag
                           FROM circle_messages cm JOIN users u ON u.id=cm.sender_id
                           WHERE cm.rosca_id=?
                           ORDER BY cm.created_at DESC LIMIT ?""",
                        (rosca_id, limit))
    return jsonify({"messages": [dict(r) for r in reversed(rows)]})


@app.route("/api/rosca/<rosca_id>/messages", methods=["POST"])
@auth.login_required
def api_circle_send_message(rosca_id):
    _ensure_circle_tables()
    message = (request.json or {}).get("message", "").strip()
    if not message or len(message) > 2000:
        return jsonify({"error": "Message must be 1–2000 characters."}), 400
    r = fetchone("SELECT id FROM roscas WHERE id=?", (rosca_id,))
    if not r: return jsonify({"error": "Circle not found"}), 404
    mid = str(uuid.uuid4())
    with get_db() as db:
        db.execute("INSERT INTO circle_messages(id,rosca_id,sender_id,message) VALUES(?,?,?,?)",
                   (mid, rosca_id, session["user_id"], message))
    user = fetchone("SELECT full_name FROM users WHERE id=?", (session["user_id"],))
    _log_circle_activity(rosca_id, session["user_id"], "message_sent",
                         f"{user['full_name']} sent a message in the circle chat")
    return jsonify({"ok": True, "id": mid})


# ── CIRCLE ANNOUNCEMENTS API ───────────────────────────────────────────────────

@app.route("/api/rosca/<rosca_id>/announcements")
@auth.login_required
def api_circle_announcements(rosca_id):
    _ensure_circle_tables()
    rows = fetchall("""SELECT ca.*, u.full_name, u.hanatag
                       FROM circle_announcements ca JOIN users u ON u.id=ca.author_id
                       WHERE ca.rosca_id=?
                       ORDER BY ca.is_pinned DESC, ca.created_at DESC LIMIT 20""",
                    (rosca_id,))
    return jsonify({"announcements": [dict(r) for r in rows]})


@app.route("/api/rosca/<rosca_id>/announcements", methods=["POST"])
@auth.login_required
def api_circle_post_announcement(rosca_id):
    _ensure_circle_tables()
    r = fetchone("SELECT organiser_id FROM roscas WHERE id=?", (rosca_id,))
    if not r or r["organiser_id"] != session["user_id"]:
        return jsonify({"error": "Only the organiser can post announcements."}), 403
    d        = request.json or {}
    title    = d.get("title", "").strip()
    body     = d.get("body", "").strip()
    priority = d.get("priority", "normal").strip()
    is_pinned = int(bool(d.get("is_pinned", False)))
    if not title or not body:
        return jsonify({"error": "Title and body are required."}), 400
    aid = str(uuid.uuid4())
    with get_db() as db:
        db.execute("""INSERT INTO circle_announcements(id,rosca_id,author_id,title,body,priority,is_pinned)
                      VALUES(?,?,?,?,?,?,?)""",
                   (aid, rosca_id, session["user_id"], title, body, priority, is_pinned))
    _log_circle_activity(rosca_id, session["user_id"], "announcement",
                         f"Organiser posted an announcement: {title}")
    return jsonify({"ok": True, "id": aid})


# ── CIRCLE VOTES API ───────────────────────────────────────────────────────────

@app.route("/api/rosca/<rosca_id>/votes")
@auth.login_required
def api_circle_votes(rosca_id):
    _ensure_circle_tables()
    votes = fetchall("""SELECT v.*, u.full_name as creator_name,
                               (SELECT COUNT(*) FROM circle_vote_responses r WHERE r.vote_id=v.id) as response_count,
                               (SELECT response FROM circle_vote_responses r WHERE r.vote_id=v.id AND r.user_id=?) as my_vote
                        FROM circle_votes v JOIN users u ON u.id=v.creator_id
                        WHERE v.rosca_id=?
                        ORDER BY v.created_at DESC""",
                     (session["user_id"], rosca_id))
    return jsonify({"votes": [dict(v) for v in votes]})


@app.route("/api/rosca/<rosca_id>/votes", methods=["POST"])
@auth.login_required
def api_circle_create_vote(rosca_id):
    _ensure_circle_tables()
    d         = request.json or {}
    question  = d.get("question", "").strip()
    vote_type = d.get("type", "simple_majority").strip()
    expires   = d.get("expires_at")
    if not question: return jsonify({"error": "Question required."}), 400
    vid = str(uuid.uuid4())
    with get_db() as db:
        db.execute("""INSERT INTO circle_votes(id,rosca_id,creator_id,question,type,expires_at)
                      VALUES(?,?,?,?,?,?)""",
                   (vid, rosca_id, session["user_id"], question, vote_type, expires))
    _log_circle_activity(rosca_id, session["user_id"], "vote_created",
                         f"A new vote was created: {question}")
    return jsonify({"ok": True, "id": vid})


@app.route("/api/rosca/votes/<vote_id>/respond", methods=["POST"])
@auth.login_required
def api_circle_vote_respond(vote_id):
    _ensure_circle_tables()
    response = (request.json or {}).get("response", "").strip()
    if response not in ("yes", "no", "abstain"):
        return jsonify({"error": "Response must be yes, no, or abstain."}), 400
    existing = fetchone("SELECT id FROM circle_vote_responses WHERE vote_id=? AND user_id=?",
                        (vote_id, session["user_id"]))
    with get_db() as db:
        if existing:
            db.execute("UPDATE circle_vote_responses SET response=? WHERE vote_id=? AND user_id=?",
                       (response, vote_id, session["user_id"]))
        else:
            db.execute("INSERT INTO circle_vote_responses(id,vote_id,user_id,response) VALUES(?,?,?,?)",
                       (str(uuid.uuid4()), vote_id, session["user_id"], response))
    return jsonify({"ok": True})


# ── CONTACT INQUIRIES ─────────────────────────────────────────────────────────

def _ensure_contact_table():
    """Create contact_inquiries table on first use."""
    try:
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS contact_inquiries (
                id           TEXT PRIMARY KEY,
                reference    TEXT UNIQUE NOT NULL,
                name         TEXT NOT NULL,
                email        TEXT NOT NULL,
                phone        TEXT,
                hanatag      TEXT,
                reason       TEXT NOT NULL DEFAULT 'other',
                context      TEXT,
                subject      TEXT NOT NULL,
                message      TEXT NOT NULL,
                priority     TEXT NOT NULL DEFAULT 'normal',
                status       TEXT NOT NULL DEFAULT 'new',
                admin_notes  TEXT,
                replied_at   TEXT,
                user_id      TEXT REFERENCES users(id),
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contact_status ON contact_inquiries(status, created_at DESC)")
    except Exception as e:
        import sys
        print(f"[_ensure_contact_table] {e}", file=sys.stderr, flush=True)


def _gen_contact_ref():
    import random, string
    year   = datetime.now().year
    suffix = ''.join(random.choices(string.digits, k=4))
    return f"SOH-CTT-{year}-{suffix}"


@app.route("/api/contact/submit", methods=["POST"])
def api_contact_submit():
    """Capture contact form submissions from the public contact page."""
    _ensure_contact_table()
    d        = request.json or {}
    name     = d.get("name", "").strip()
    email    = d.get("email", "").strip().lower()
    phone    = d.get("phone", "").strip()
    hanatag  = d.get("hanatag", "").strip()
    reason   = d.get("reason", "other").strip()
    context  = d.get("context", "").strip()
    subject  = d.get("subject", "").strip()
    message  = d.get("message", "").strip()
    priority = d.get("priority", "normal").strip()

    # Validation
    if not name or not email or not subject or not message:
        return jsonify({"error": "Name, email, subject, and message are required."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "Please enter a valid email address."}), 400
    if len(message) < 20:
        return jsonify({"error": "Please write a bit more so we can help you properly."}), 400

    valid_reasons   = ("support", "ncs", "kyc", "partnership", "press", "feedback", "other")
    valid_priority  = ("normal", "high", "urgent")
    if reason   not in valid_reasons:  reason   = "other"
    if priority not in valid_priority: priority = "normal"

    user_id = session.get("user_id")

    try:
        cid = str(uuid.uuid4())
        for _ in range(5):
            ref = _gen_contact_ref()
            if not fetchone("SELECT id FROM contact_inquiries WHERE reference=?", (ref,)):
                break
        with get_db() as db:
            db.execute(
                """INSERT INTO contact_inquiries
                   (id, reference, name, email, phone, hanatag, reason, context,
                    subject, message, priority, user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, ref, name, email, phone or None, hanatag or None,
                 reason, context or None, subject, message, priority, user_id)
            )
        return jsonify({"ok": True, "reference": ref})
    except Exception as e:
        import sys
        print(f"[contact_submit] {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Submission failed. Please email support@sohana.app directly."}), 500


# ── CONTACT ADMIN ─────────────────────────────────────────────────────────────

@app.route("/admin/contact")
@admin_required
def admin_contact():
    _ensure_contact_table()
    user = auth.get_current_user()
    inquiries = fetchall(
        "SELECT * FROM contact_inquiries ORDER BY created_at DESC LIMIT 200"
    )
    return render_template("admin_contact.html", user=user, inquiries=inquiries)


@app.route("/api/admin/contact/<inquiry_id>/status", methods=["POST"])
@admin_required
def api_admin_contact_status(inquiry_id):
    d      = request.json or {}
    status = d.get("status", "").strip()
    if status not in ("new", "open", "replied", "closed"):
        return jsonify({"error": "Invalid status"}), 400
    try:
        with get_db() as db:
            db.execute("UPDATE contact_inquiries SET status=? WHERE id=?", (status, inquiry_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/contact/<inquiry_id>/notes", methods=["POST"])
@admin_required
def api_admin_contact_notes(inquiry_id):
    notes = (request.json or {}).get("notes", "").strip()
    try:
        with get_db() as db:
            db.execute("UPDATE contact_inquiries SET admin_notes=? WHERE id=?", (notes, inquiry_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/contact/<inquiry_id>/reply", methods=["POST"])
@admin_required
def api_admin_contact_reply(inquiry_id):
    """Log that a reply was sent and mark inquiry as replied."""
    d       = request.json or {}
    subject = d.get("subject", "").strip()
    body    = d.get("body", "").strip()
    if not body:
        return jsonify({"error": "Reply body is required."}), 400
    try:
        now = datetime.now().isoformat()
        with get_db() as db:
            db.execute(
                "UPDATE contact_inquiries SET status='replied', replied_at=? WHERE id=?",
                (now, inquiry_id)
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/contact/export")
@admin_required
def admin_contact_export():
    _ensure_contact_table()
    import csv, io
    inquiries = fetchall("SELECT * FROM contact_inquiries ORDER BY created_at DESC")
    output    = io.StringIO()
    writer    = csv.writer(output)
    writer.writerow(["reference","name","email","phone","hanatag","reason","subject","priority","status","created_at"])
    for i in inquiries:
        writer.writerow([i["reference"],i["name"],i["email"],i.get("phone",""),
                         i.get("hanatag",""),i["reason"],i["subject"],
                         i["priority"],i["status"],i["created_at"]])
    output.seek(0)
    from flask import Response
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=contact_inquiries.csv"})


# ── PWA / TWA ROUTES ─────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    response = send_from_directory(
        os.path.join(app.root_path, 'static'),
        'sw.js'
    )
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/manifest.json')
def web_manifest():
    """Serve PWA manifest inline — no static file dependency."""
    import json as _json
    manifest = {
        "name": "SOHANA",
        "short_name": "SOHANA",
        "description": "Community savings for the African diaspora. Circles, Pools, Hanapay and the Njangi Credit Score.",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0E120F",
        "theme_color": "#9EE493",
        "orientation": "portrait-primary",
        "lang": "en",
        "dir": "ltr",
        "categories": ["finance"],
        "icons": [
            {"src": "/static/icons/icon-96.png",           "sizes": "96x96",   "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-192.png",          "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icons/icon-512.png",          "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "screenshots": [
            {"src": "/static/screenshots/dashboard.png", "sizes": "390x844", "type": "image/png", "form_factor": "narrow", "label": "SOHANA Dashboard"},
            {"src": "/static/screenshots/wallet.png",    "sizes": "390x844", "type": "image/png", "form_factor": "narrow", "label": "Multi-currency Wallet"},
            {"src": "/static/screenshots/circles.png",   "sizes": "390x844", "type": "image/png", "form_factor": "narrow", "label": "Njangi Circles"},
        ],
        "shortcuts": [
            {"name": "My Wallet",  "short_name": "Wallet",  "description": "Open your multi-currency wallet", "url": "/wallet",         "icons": [{"src": "/static/icons/icon-96.png", "sizes": "96x96"}]},
            {"name": "My Circles", "short_name": "Circles", "description": "View your Njangi circles",       "url": "/circles",        "icons": [{"src": "/static/icons/icon-96.png", "sizes": "96x96"}]},
            {"name": "Hanapay",    "short_name": "Hanapay", "description": "Send money by @handle",          "url": "/wallet#hanapay", "icons": [{"src": "/static/icons/icon-96.png", "sizes": "96x96"}]},
        ],
    }
    resp = Response(
        _json.dumps(manifest, indent=2),
        mimetype='application/manifest+json'
    )
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/.well-known/assetlinks.json')
def asset_links():
    """Serve Digital Asset Links inline — update SHA256 fingerprint after keystore generation."""
    import json as _json
    # UPDATE sha256_cert_fingerprints with your keystore SHA256 after running keytool
    links = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "app.sohana.twa",
                "sha256_cert_fingerprints": [
                    os.environ.get("TWA_SHA256_FINGERPRINT",
                                   "REPLACE_WITH_YOUR_SHA256_FINGERPRINT_AFTER_KEYGEN")
                ]
            }
        }
    ]
    resp = Response(
        _json.dumps(links, indent=2),
        mimetype='application/json'
    )
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/offline')
def offline_page():
    return render_template('offline.html'), 200


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC ROUTES — test email + TOTP without touching the full app flow
# All require CEO or CTO admin login
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/comms-test")
@admin_required
def comms_test_page():
    """Diagnostic dashboard to test SendGrid email and TOTP from the admin panel."""
    user = auth.get_current_user()
    role = session.get("admin_role", "")
    if role not in ("ceo", "cto"):
        return "Access restricted to CEO and CTO.", 403
    try:
        import comms
        status = comms.is_configured()
    except Exception as e:
        status = {"error": str(e)}
    return render_template("comms_test.html", user=user, status=status)


@app.route("/api/admin/comms/test-email", methods=["POST"])
@admin_required
def api_test_email():
    """Send a real test email via SendGrid to verify delivery."""
    role = session.get("admin_role", "")
    if role not in ("ceo", "cto"):
        return jsonify({"error": "CEO/CTO only"}), 403
    d        = request.json or {}
    to_email = d.get("email", "").strip()
    to_name  = d.get("name", "SOHANA Test")
    if not to_email:
        return jsonify({"error": "Email address required"}), 400
    try:
        import comms
        result = comms.send_email(
            to_email     = to_email,
            to_name      = to_name,
            template_key = "notification",
            template_data = {
                "subject_line":   "SOHANA — Email delivery test",
                "message_body":   "This is a test email sent from the SOHANA admin diagnostic panel. If you received this, SendGrid email delivery is working correctly.",
                "highlight_label": "Status",
                "highlight_value": "Delivered ✓",
                "cta_label":      "Go to platform",
                "cta_url":        "https://sohana.app",
            }
        )
        return jsonify({"ok": result, "to": to_email,
                        "message": "Email sent — check your inbox" if result else "Send failed — check Railway logs for SendGrid errors"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/comms/test-reset-email", methods=["POST"])
@admin_required
def api_test_reset_email():
    """Send a real password reset email to a user — verifies the reset_pw template."""
    role = session.get("admin_role", "")
    if role not in ("ceo", "cto"):
        return jsonify({"error": "CEO/CTO only"}), 403
    d     = request.json or {}
    phone = d.get("phone", "").strip()
    user  = fetchone("SELECT id, full_name, email FROM users WHERE phone=? OR email=?",
                     (phone, phone))
    if not user or not user["email"]:
        return jsonify({"error": "User not found or has no email address"}), 404
    try:
        import secrets as _sec, os
        token      = _sec.token_urlsafe(32)
        _base      = os.environ.get("APP_BASE_URL", "https://sohana.app").rstrip("/")
        reset_link = f"{_base}/reset-password/{token}"
        with get_db() as db:
            db.execute("UPDATE password_reset_tokens SET used=1 WHERE user_id=?", (user["id"],))
            db.execute(
                """INSERT INTO password_reset_tokens(id,user_id,token,expires_at)
                   VALUES(?,?,?,datetime('now','+1 hour'))""",
                (str(uuid.uuid4()), user["id"], token)
            )
        import comms
        result = comms.send_email(
            to_email=user["email"], to_name=user["full_name"],
            template_key="reset_pw",
            template_data={"reset_link": reset_link, "otp_ttl": "60 minutes"}
        )
        return jsonify({"ok": result, "to": user["email"],
                        "reset_link": reset_link,
                        "message": "Reset email sent — check inbox" if result else "Failed — check logs"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/comms/test-totp", methods=["POST"])
@admin_required
def api_test_totp():
    """Generate a TOTP secret for a user and return the QR code for scanning."""
    role = session.get("admin_role", "")
    if role not in ("ceo", "cto"):
        return jsonify({"error": "CEO/CTO only"}), 403
    d      = request.json or {}
    phone  = d.get("phone", "").strip()
    target = fetchone("SELECT id, full_name, phone FROM users WHERE phone=? OR email=?",
                      (phone, phone))
    if not target:
        return jsonify({"error": "User not found"}), 404
    try:
        import pyotp, qrcode, io, base64
        secret = pyotp.random_base32()
        label  = f"SOHANA:{target['phone'] or target['full_name']}"
        uri    = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name="SOHANA")
        img    = qrcode.make(uri)
        buf    = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        # Store the secret
        with get_db() as db:
            db.execute("UPDATE users SET totp_secret=?, totp_enabled=0 WHERE id=?",
                       (secret, target["id"]))
        return jsonify({"ok": True, "user": target["full_name"],
                        "secret": secret,
                        "qr": f"data:image/png;base64,{qr_b64}",
                        "note": "Ask the user to scan this QR in Google Authenticator. Then call /api/auth/totp/confirm to activate."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/auth-login.js')
def serve_auth_login_js():
    """
    Serve login-critical JS via Flask with no-cache headers.
    This ensures the latest version is always used — immune to browser/CDN caching.
    """
    from flask import Response as _Response
    js = """
var _pendingToken = null;

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
  var tabEl = document.getElementById('tab-' + tab);
  var panelEl = document.getElementById('panel-' + tab);
  if (tabEl) tabEl.classList.add('active');
  if (panelEl) panelEl.classList.add('active');
}

function doLogin() {
  if (typeof hideAlert === 'function') hideAlert('login');
  var id   = (document.getElementById('l-id') || {}).value || '';
  var pass = (document.getElementById('l-pass') || {}).value || '';
  id = id.trim();
  if (!id)   { if (typeof showAlert === 'function') showAlert('login', 'Please enter your email or phone number.'); return; }
  if (!pass) { if (typeof showAlert === 'function') showAlert('login', 'Please enter your password.'); return; }
  var btn = document.getElementById('btn-login');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; btn.textContent = 'Signing in…'; }

  fetch('/api/auth/login', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ phone: id, password: pass })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = 'Sign in'; }
    if (d.error) {
      if (typeof showAlert === 'function') showAlert('login', d.error);
      else alert(d.error);
      return;
    }
    if (d.requires_2fa) {
      _pendingToken = d.pending_token;
      var s = document.getElementById('totp-step');
      if (s) { s.style.display = 'block'; }
      var inp = document.getElementById('totp-input');
      if (inp) { inp.focus(); }
      return;
    }
    if (d.ok) {
      window.location.href = d.is_admin ? '/admin/home' : '/dashboard';
    }
  })
  .catch(function(err) {
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = 'Sign in'; }
    alert('Connection error — please check your internet and try again.');
  });
}

function submitTotp() {
  var inp  = document.getElementById('totp-input');
  var code = inp ? inp.value.replace(/[^0-9]/g, '') : '';
  if (code.length !== 6) { return; }
  fetch('/api/auth/login-step2', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ pending_token: _pendingToken, totp_code: code })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      window.location.href = d.is_admin ? '/admin/home' : '/dashboard';
    } else {
      if (inp) {
        inp.value = '';
        inp.style.borderColor = '#FF6A55';
        setTimeout(function() { inp.style.borderColor = 'rgba(158,228,147,.3)'; }, 1500);
      }
    }
  })
  .catch(function() {
    if (inp) { inp.value = ''; }
  });
}

document.addEventListener('DOMContentLoaded', function() {
  // Wire buttons directly — works even if onclick attrs are stripped
  var btnLogin = document.getElementById('btn-login');
  if (btnLogin) { btnLogin.addEventListener('click', doLogin); }

  document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter') return;
    var step = document.getElementById('totp-step');
    if (step && step.style.display !== 'none') { submitTotp(); return; }
    // Enter on sign-in tab
    var loginPanel = document.getElementById('panel-login');
    if (loginPanel && loginPanel.classList.contains('active')) { doLogin(); }
  });
});
"""
    resp = _Response(js, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp


# ── PUBLIC CONTENT PAGES ─────────────────────────────────────────────────────

PUBLIC_PAGES = {
    # Products
    "njangi":        ("njangi.html",        "Njangi Circles"),
    "pools-page":    ("pools-page.html",    "Contribution Pools"),
    "fundraising":   ("fundraising.html",   "Fundraising & Donations"),
    "hanapay":       ("hanapay.html",       "HanaPay"),
    "wallet-page":   ("wallet-page.html",   "Multicurrency Wallet"),
    "ncs-page":      ("ncs-page.html",      "NCS Credit Score"),
    # Resources
    "how-it-works":  ("how-it-works.html",  "How It Works"),
    "ncs-guide":     ("ncs-guide.html",     "NCS Score Guide"),
    "currencies":    ("currencies.html",    "Supported Currencies"),
    "security":      ("security.html",      "Security & Trust"),
    # Company
    "about":         ("about.html",         "About SOHANA"),
    "mission":       ("mission.html",       "Our Mission"),
    "careers":       ("careers.html",       "Careers"),
    "press":         ("press.html",         "Press"),
    "partnerships":  ("partnerships.html",  "Partnerships"),
    # Help
    "contact":       ("contact.html",       "Contact Us"),
    # Legal
    "privacy":       ("privacy.html",       "Privacy Policy"),
    "terms":         ("terms.html",         "Terms of Service"),
    "cookies":       ("cookies.html",       "Cookie Policy"),
    "complaints":    ("complaints.html",    "Complaints"),
    "accessibility": ("accessibility.html", "Accessibility"),
    "help":          ("help.html",          "Help Centre"),
}

# ── MARKET PAGES ─────────────────────────────────────────────────────────────

MARKET_PAGES = {
    "france":   ("market_france.html",   "Sohana in France"),
    "uk":       ("market_uk.html",       "Sohana in the UK"),
    "belgium":  ("market_belgium.html",  "Sohana in Belgium"),
    "canada":   ("market_canada.html",   "Sohana in Canada"),
    "cameroon": ("market_cameroon.html", "Sohana in Cameroon, Ghana & Nigeria"),
    "rwanda":   ("market_rwanda.html",   "Sohana in Rwanda, CI & Southern Africa"),
}

@app.route("/markets/<country>")
def market_page(country):
    if country in MARKET_PAGES:
        template, title = MARKET_PAGES[country]
        return render_template(template)
    return render_template("landing_new.html"), 404

@app.route("/<path:slug>")
def public_page(slug):
    """Serve any public content page by slug."""
    if slug in PUBLIC_PAGES:
        template, title = PUBLIC_PAGES[slug]
        return render_template(template)
    # Not a public page — return 404
    return render_template("landing_new.html"), 404


# ── SURVEY ARTICLE SEED (runs every startup, independent of _seed_all guard) ─

def _ensure_survey_article():
    """Insert the community survey blog article if it doesn't exist.
    Runs every startup so it works even on DBs where _seed_all already ran."""
    try:
        existing = fetchone("SELECT id FROM blog_posts WHERE id='blog-007'")
        if existing:
            return  # already seeded
        admin_row = fetchone("SELECT id FROM users WHERE admin_role='ceo'")
        author_id = admin_row["id"] if admin_row else None
        # If no admin yet, use any user as author
        if not author_id:
            any_user = fetchone("SELECT id FROM users LIMIT 1")
            author_id = any_user["id"] if any_user else "system"
        excerpt = ("We have been building SOHANA for you. Now we need to hear from you directly. "
                   "Our community survey is open in English and French \u2014 8 to 10 minutes, "
                   "completely anonymous.")
        body = """ + repr(SURVEY_BODY) + """
        with get_db() as db:
            # Ensure author_id column allows NULL for system posts
            try:
                db.execute("ALTER TABLE blog_posts ALTER COLUMN author_id DROP NOT NULL")
            except Exception:
                pass  # SQLite doesn't support this; handled by inserting a valid id above
            db.execute("""INSERT OR REPLACE INTO blog_posts
                          (id,title,slug,excerpt,body,category,author_id,is_published,published_at)
                          VALUES(?,?,?,?,?,?,?,1,datetime('now'))""",
                       ("blog-007",
                        "Help us build SOHANA: take our community survey",
                        "community-survey-2026",
                        excerpt, body, "news", author_id))
        import sys
        print("[SOHANA] Survey article blog-007 seeded.", file=sys.stderr, flush=True)
    except Exception as e:
        import sys, traceback
        print(f"[_ensure_survey_article] {e}", file=sys.stderr, flush=True)


# ── ADMIN PASSWORD SYNC (runs every startup to apply env var changes) ─────────

def _sync_admin_passwords():
    """Re-hash admin passwords from ADMIN_SEED_PASSWORD env var on every startup.
    This ensures changing the env var in Railway actually takes effect."""
    import os as _os
    pw = _os.environ.get("ADMIN_SEED_PASSWORD", "")
    if not pw:
        return
    try:
        admin_phones = [
            "+00000000001", "+00000000002", "+00000000003",
            "+00000000004", "+00000000005", "+00000000006",
            "+00000000007", "+00000000008", "+00000000009",
        ]
        new_hash = auth.hash_password(pw)
        with get_db() as db:
            for phone in admin_phones:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE phone=? AND is_admin=1",
                    (new_hash, phone)
                )
        import sys
        print("[SOHANA] Admin passwords synced from ADMIN_SEED_PASSWORD.", file=sys.stderr, flush=True)
    except Exception as e:
        import sys
        print(f"[_sync_admin_passwords] {e}", file=sys.stderr, flush=True)


def _seed_all():
    if fetchone("SELECT id FROM users WHERE phone='+33611000001'"): return

    # Regular users
    # Demo user seed — password read from DEMO_SEED_PASSWORD env var
    _demo_pw = os.environ.get("DEMO_SEED_PASSWORD", "")
    if not _demo_pw:
        import sys
        print("[SEED] DEMO_SEED_PASSWORD env var not set — skipping demo user seed.",
              file=sys.stderr, flush=True)
        _demo_pw = ""  # proceed but passwords will be blank hashes; users won't log in
    regular_users = [
        ("+33611000001","Maria Ngono",   _demo_pw,"FR",480,0,None),
        ("+33611000002","Samuel Eto",    _demo_pw,"CM",680,0,None),
        ("+25078100001","Alice Uwase",   _demo_pw,"RW",750,0,None),
        ("+44795000001","Kwame Asante",  _demo_pw,"GB",560,0,None),
        ("+33611000003","Fatou Diallo",  _demo_pw,"FR",390,0,None),
    ]
    # Admin seed accounts — password read from ADMIN_SEED_PASSWORD env var.
    # Set this in Railway environment variables. Never commit plaintext passwords.
    _admin_pw = os.environ.get("ADMIN_SEED_PASSWORD", "")
    if not _admin_pw:
        import sys
        print("[SEED] ADMIN_SEED_PASSWORD env var not set — skipping admin seed.",
              file=sys.stderr, flush=True)
        return
    admin_users = [
        ("+00000000001", "Kwame Mensah",   _admin_pw, "CM", 800, 1, "ceo"),
        ("+00000000002", "Kojo Agyeman",   _admin_pw, "GH", 800, 1, "cto"),
        ("+00000000003", "Akosua Mensah",  _admin_pw, "GH", 800, 1, "cco"),
        ("+00000000004", "Ama Boateng",    _admin_pw, "GH", 800, 1, "cfo"),
        ("+00000000005", "Kofi Adu",       _admin_pw, "GH", 800, 1, "fraud"),
        ("+00000000006", "Yaw Darko",      _admin_pw, "GH", 800, 1, "credit"),
        ("+00000000007", "Abena Frimpong", _admin_pw, "GH", 800, 1, "operations"),
        ("+00000000008", "Efua Mensah",    _admin_pw, "GH", 800, 1, "compliance"),
        ("+00000000009", "Kwesi Antwi",    _admin_pw, "GH", 800, 1, "business"),
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
    BLOG_POSTS = [
        ("blog-001","SOHANA wins TEF Entrepreneurship Award 2026","tef-award-2026",
         "SOHANA has been selected as a winning project at the Tony Elumelu Foundation Entrepreneurship Award for 2026, recognised for its innovative approach to digitising community savings.",
         "We are proud to announce that SOHANA has been selected among the winning projects at the Tony Elumelu Foundation Entrepreneurship Award (TEF Cohort 2026). This recognition validates our mission to digitalise rotating savings circles — Tontines, Njangis, Esusu and Chamas — and build financial identity for the African diaspora.\n\nThe TEF award recognises African entrepreneurs who are building scalable solutions to the continent's most pressing challenges. SOHANA's approach — using savings behaviour to build a proprietary credit score (NCS) for the unbanked — was cited as one of the most innovative fintech models in the cohort.\n\nWe are grateful to our beta users, our team, and everyone who believed in this from the beginning.",
         "news","admin_id"),
        ("blog-002","What is the Njangi Credit Score (NCS)?","what-is-ncs",
         "The NCS is a 300–850 behavioural credit score built from your savings circle participation — not from your bank history. Here is how it works and why it matters.",
         "Traditional credit scores measure whether you have borrowed money and paid it back. The NCS measures something different: whether you save reliably, contribute on time, and keep your commitments to your community.\n\nFor millions of African diaspora members — nurses in London, engineers in Paris, market traders in Douala — there is no traditional credit file. Banks cannot see their financial discipline. The NCS makes that discipline visible.\n\nYour NCS is calculated from five components: contribution consistency (35%), active circles (20%), tenure on SOHANA (15%), organiser reputation (15%), and cross-circle diversity (15%). Every on-time contribution moves the dial. Every completed cycle earns a milestone badge.\n\nAt 550, you unlock emergency credit. At 650, early payout loans. At 750, you reach Exemplary tier — the highest recognition on the platform.",
         "education","admin_id"),
        ("blog-003","How savings circles work in the digital age","savings-circles-digital",
         "The Tontine, Njangi, Esusu, Chama — these rotating savings traditions have sustained African communities for generations. SOHANA brings them online without losing what makes them work.",
         "A savings circle is one of humanity's oldest financial instruments. A group of people agree to contribute a fixed amount regularly. Each round, one member receives the full pot. The circle rotates until everyone has received once.\n\nWhat makes it work is not technology — it is trust. Community accountability. Social enforcement. SOHANA's role is not to replace that trust but to protect it.\n\nWith SOHANA, contributions are tracked automatically. Payouts release on schedule. Three admins must approve any change to the pot. The organiser has a dashboard showing exactly who has paid, who is late, and what the circle's reliability score looks like.\n\nThe ledger moves from a WhatsApp notebook to an auditable, transparent system. The trust stays exactly where it belongs: in the community.",
         "education","admin_id"),
        ("blog-004","Pan-African currency exchange: why gold, not the dollar","gold-standard-africa",
         "SOHANA Labs has launched the first intra-African currency exchange anchored to gold rather than the US Dollar. Here is the research behind it.",
         "The US Dollar became the world's reserve currency in 1944 — not because it represented real wealth, but by political agreement. Since then, African currencies have been priced against a floating instrument managed by institutions in which Africa has no meaningful vote.\n\nGold is different. Africa holds approximately 40% of the world's gold reserves. The DRC alone holds an estimated $24 trillion in mineral wealth. Ghana, South Africa, Mali, Burkina Faso and Sudan are among the world's largest gold producers.\n\nWhen the Ghanaian Cedi is priced against gold rather than the dollar, it is priced against something Ghana actually has.\n\nSohana Labs' gold-normalised currency explorer is a research tool — not a trading platform. But it represents a framework: what would African currencies look like if they were anchored to African wealth? Visit /currencies to explore.",
         "research","admin_id"),
        ("blog-005","Building for the African diaspora: a design philosophy","diaspora-design-philosophy",
         "Why SOHANA uses Njangi, Tontine, Hanatag — not generic fintech language. A note on building with cultural specificity.",
         "When we built SOHANA, we made a deliberate choice: we would use the real names for things.\n\nNot 'savings group' but Njangi. Not 'handle' but Hanatag. Not 'rotating fund' but Tontine, Esusu, Chama, Tanda — depending on who you are and where you are from.\n\nFintech platforms often strip cultural specificity in pursuit of universality. We believe the opposite: cultural specificity is the feature, not a limitation. A Cameroonian woman in Lyon does not need a platform that pretends not to know what a Njangi is. She needs a platform that knows exactly what it is and builds for it.\n\nThis philosophy extends to our design system — warm cream ink instead of cold white, community photography that reflects our actual users, copy that speaks directly without condescension.\n\nWe are building for the most financially sophisticated communities in the world. They deserve a product that knows them.",
         "culture","admin_id"),
        ("blog-007","Help us build SOHANA: take our community survey","community-survey-2026",
         "We have been building SOHANA for you. Now we need to hear from you directly. Our community survey is open in English and French — 8 to 10 minutes, completely anonymous, and your answers go directly to the team building the product.",
         "We have been building SOHANA for you — and now we need to hear from you directly.\n\nSOHANA was born from a simple conviction: that the financial traditions your community has practised for generations deserve a platform that takes them seriously. The Njangi, the Tontine, the Esusu, the Chama — these are not informal workarounds. They are sophisticated, trust-based financial systems. We are here to make them permanent, portable, and recognised.\n\nBut building the right platform requires more than good intentions. It requires understanding your real life, your real constraints, and your real expectations.\n\n**We are asking for 8–10 minutes of your time.**\n\n---\n\n## What we want to understand\n\nWe have two surveys — one for people already on the platform, and one for people still on the waitlist. Both are open now.\n\n**Your answers will directly shape:**\n- How the Njangi circle experience works\n- What currencies and payment methods we prioritise\n- How the NCS credit score is communicated and used\n- What trust signals matter most to you when using a new financial platform\n- How we handle notifications, reminders, and community updates\n\n---\n\n## Take the survey\n\nChoose the link that applies to you:\n\n**[→ Survey for Platform Members (English)](https://forms.gle/yzFcakdZYwrCfKYDA)**\n*For users who have already registered on SOHANA*\n\n**[→ Survey for Waitlist Members (English)](https://forms.gle/yzFcakdZYwrCfKYDA)**\n*For users on the waitlist who have not yet registered*\n\n---\n\n## Sondage en français\n\nNous construisons SOHANA pour vous — et maintenant nous avons besoin de vous entendre directement.\n\nSOHANA est né d'une conviction simple : que les traditions financières que votre communauté pratique depuis des générations méritent une plateforme qui les prend au sérieux. Le Njangi, la Tontine, l'Esusu, le Chama — ce ne sont pas des solutions informelles. Ce sont des systèmes financiers sophistiqués, basés sur la confiance. Nous sommes là pour les rendre permanents, portables et reconnus.\n\nMais construire la bonne plateforme nécessite plus que de bonnes intentions. Cela nécessite de comprendre votre vie réelle, vos contraintes réelles et vos attentes réelles.\n\n**Nous vous demandons 8 à 10 minutes de votre temps.**\n\nVos réponses influenceront directement :\n- Le fonctionnement de l'expérience des cercles Njangi\n- Les devises et méthodes de paiement que nous priorisons\n- Comment le score de crédit NCS est communiqué et utilisé\n- Quels signaux de confiance sont les plus importants pour vous\n- Comment nous gérons les notifications, les rappels et les mises à jour communautaires\n\n**[→ Sondage pour les membres de la plateforme (Français)](https://forms.gle/kP3auRVwuGvFzNFNA)**\n*Pour les utilisateurs déjà inscrits sur SOHANA*\n\n**[→ Sondage pour les membres de la liste d'attente (Français)](https://forms.gle/kP3auRVwuGvFzNFNA)**\n*Pour les utilisateurs sur la liste d'attente*\n\n---\n\n## Why your voice matters\n\nThis is a critical moment. We are approaching our Q3 2026 beta launch, finalising our regulatory applications with the ACPR (France) and the FCA (UK), and preparing to open the platform to our first active users.\n\nEvery response we receive shapes a product decision. There is no research team filtering your feedback. Your answers go directly to the people building the platform.\n\nThe survey is completely anonymous. No identifying information is collected unless you choose to share it at the end.\n\n**Thank you for being part of this from the beginning.**\n\n— *The SOHANA Team*",
         "news","admin_id"),
        ("blog-006","Beta launch: what to expect from SOHANA in Q3 2026","beta-launch-q3-2026",
         "SOHANA's beta launch is scheduled for Q3 2026. Here is what early users will have access to, what the waitlist gets, and what comes next.",
         "We are targeting Q3 2026 for our beta launch. Waitlist members will receive early access to the full platform: multicurrency wallet (EUR, GBP, USD, CAD, XAF, GHC, NGN, ZAR), savings circle creation and management, NCS score building, and HanaPay instant transfers.\n\nWaitlist members who join before launch also receive: priority Hanatag handles (shorter, cleaner usernames), founder-circle status, and free outbound transfers for the first 12 months.\n\nThe regulatory path is clear. We are in active dialogue with ACPR in France and FCA in the UK. Until authorisations are in place, the platform operates in a controlled testing environment with no real funds.\n\nIf you are not on the waitlist yet, join at sohana.app. If you are already on it — thank you. You are part of something historic.",
         "news","admin_id"),
    ]
    admin_uid = fetchone("SELECT id FROM users WHERE admin_role='ceo'")
    admin_id  = admin_uid["id"] if admin_uid else None
    with get_db() as db:
        for bid,title,slug,excerpt,body,cat,_ in BLOG_POSTS:
            db.execute("""INSERT OR IGNORE INTO blog_posts(id,title,slug,excerpt,body,category,author_id,is_published,published_at)
                          VALUES(?,?,?,?,?,?,?,1,datetime('now'))""",
                       (bid,title,slug,excerpt,body,cat,admin_id))
        # Force-insert new posts that were added after initial seed (INSERT OR REPLACE updates if exists)
        _new_posts = [(p[0],p[1],p[2],p[3],p[4],p[5]) for p in BLOG_POSTS if p[0] in ("blog-007",)]
        for bid,title,slug,excerpt,body,cat in _new_posts:
            db.execute("""INSERT OR REPLACE INTO blog_posts
                          (id,title,slug,excerpt,body,category,author_id,is_published,published_at)
                          VALUES(?,?,?,?,?,?,?,1,datetime('now'))""",
                       (bid,title,slug,excerpt,body,cat,admin_id))

# ══════════════════════════════════════════════════════════════════════════════
# STATUS PAGE — public status page, health checks, incident management (v7.3)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/service-status')
def service_status_page():
    components = [dict(r) for r in fetchall(
        "SELECT * FROM service_components ORDER BY display_order")]
    active_incidents = [dict(r) for r in fetchall(
        "SELECT * FROM status_incidents WHERE status != 'resolved' "
        "AND severity != 'maintenance' ORDER BY created_at DESC")]
    upcoming_maintenance = [dict(r) for r in fetchall(
        "SELECT * FROM status_incidents WHERE severity='maintenance' "
        "AND status='scheduled' ORDER BY scheduled_start ASC")]

    incident_updates = {}
    for inc in active_incidents + upcoming_maintenance:
        incident_updates[inc['id']] = [dict(r) for r in fetchall(
            "SELECT * FROM status_incident_updates WHERE incident_id=? ORDER BY created_at DESC",
            (inc['id'],)
        )]

    uptime = {
        c['id']: status_module.uptime_percentage(c['id'], days=90)
        for c in components
    }

    overall = status_module.overall_status()

    return render_template(
        'service_status.html',
        components=components,
        overall=overall,
        overall_label=status_module.STATUS_LABELS.get(overall, 'Operational'),
        status_labels=status_module.STATUS_LABELS,
        status_rank=status_module.STATUS_RANK,
        incidents=active_incidents,
        maintenance=upcoming_maintenance,
        incident_updates=incident_updates,
        uptime=uptime,
    )


@app.route('/api/status/healthcheck')
def api_status_healthcheck():
    """Lightweight JSON health endpoint for external monitors / load balancers."""
    db_up, db_ms = status_module.check_database()
    return jsonify({
        "status":      "ok" if db_up else "error",
        "database":    db_up,
        "response_ms": db_ms,
        "timestamp":   datetime.utcnow().isoformat()
    }), (200 if db_up else 503)


@app.route('/api/status/external-ping', methods=['POST'])
def api_status_external_ping():
    """
    Receives webhook pings from UptimeRobot (or similar) and logs external
    uptime checks. Configure the monitor's webhook payload to send JSON:
    {"alert_type": "up"|"down", "monitor": "web_app"}
    Validated via a shared-secret query param.
    """
    secret = request.args.get('secret')
    if secret != os.environ.get('STATUS_WEBHOOK_SECRET'):
        return jsonify({"error": "unauthorized"}), 401

    data       = request.get_json(silent=True) or {}
    alert_type = data.get('alert_type', 'up')
    monitor    = data.get('monitor', 'web_app')
    is_up      = alert_type == 'up'

    status_module.log_check(monitor, is_up, None, source='external')
    status_module._maybe_update_component_status(monitor, is_up)

    return jsonify({"received": True}), 200


@app.route('/api/status/subscribe', methods=['POST'])
def api_status_subscribe():
    data  = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({"error": "Valid email required"}), 400
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO status_subscribers (id, email, is_confirmed) VALUES (?, ?, 1)",
                (str(uuid.uuid4()), email)
            )
    except Exception:
        pass  # likely duplicate — treat as success
    return jsonify({"success": True}), 200


# ── ADMIN: STATUS PAGE MANAGEMENT ─────────────────────────────────────────────

@app.route('/admin/status')
@admin_required
def admin_status_page():
    components = [dict(r) for r in fetchall(
        "SELECT * FROM service_components ORDER BY display_order")]
    incidents  = [dict(r) for r in fetchall(
        "SELECT * FROM status_incidents ORDER BY created_at DESC LIMIT 50")]
    incident_updates = {}
    for inc in incidents:
        incident_updates[inc['id']] = [dict(r) for r in fetchall(
            "SELECT * FROM status_incident_updates WHERE incident_id=? ORDER BY created_at DESC",
            (inc['id'],)
        )]
    return render_template('admin_status.html', components=components, incidents=incidents,
                            incident_updates=incident_updates,
                            status_labels=status_module.STATUS_LABELS,
                            status_rank=status_module.STATUS_RANK)


@app.route('/api/admin/status/component/<component_id>', methods=['POST'])
@admin_required
def api_admin_update_component(component_id):
    data       = request.get_json(silent=True) or {}
    new_status = data.get('status')
    if new_status not in status_module.STATUS_RANK:
        return jsonify({"error": "Invalid status"}), 400
    with get_db() as db:
        db.execute(
            "UPDATE service_components SET status=?, updated_at=datetime('now') WHERE id=?",
            (new_status, component_id)
        )
    log_admin_action("status_component_update", "service_component", component_id,
                      new_data={"status": new_status})
    return jsonify({"success": True}), 200


@app.route('/api/admin/status/incident/create', methods=['POST'])
@admin_required
def api_admin_create_incident():
    data = request.get_json(silent=True) or {}
    incident_id = status_module.create_incident(
        title           = data.get('title', '').strip(),
        component_id    = data.get('component_id') or None,
        severity        = data.get('severity', 'minor'),
        status          = data.get('status', 'investigating'),
        message         = data.get('message', '').strip(),
        scheduled_start = data.get('scheduled_start') or None,
        scheduled_end   = data.get('scheduled_end') or None,
    )
    log_admin_action("status_incident_create", "status_incident", incident_id,
                      new_data=data)
    return jsonify({"success": True, "incident_id": incident_id}), 200


@app.route('/api/admin/status/incident/<incident_id>/update', methods=['POST'])
@admin_required
def api_admin_update_incident(incident_id):
    data = request.get_json(silent=True) or {}
    status_module.add_incident_update(
        incident_id,
        message    = data.get('message', '').strip(),
        new_status = data.get('status') or None,
    )
    log_admin_action("status_incident_update", "status_incident", incident_id,
                      new_data=data)
    return jsonify({"success": True}), 200




@app.route('/api/admin/rates/refresh', methods=['POST'])
@admin_required
def api_admin_refresh_rates():
    """Manual override — force an exchange-rate refresh now."""
    ok = refresh_exchange_rates()
    log_admin_action("exchange_rates_manual_refresh", "system", "rates",
                      new_data={"ok": ok, "meta": dict(EXCHANGE_RATES_META)})
    return jsonify({
        "ok":     ok,
        "rates":  EXCHANGE_RATES,
        "source": EXCHANGE_RATES_META.get("source"),
        "updated_at": EXCHANGE_RATES_META.get("updated_at"),
        "error":  EXCHANGE_RATES_META.get("error"),
    }), (200 if ok else 502)


# ── SCHEDULED HEALTH CHECKS ────────────────────────────────────────────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _status_scheduler = BackgroundScheduler(daemon=True)
    _status_scheduler.add_job(status_module.run_health_checks, 'interval',
                               minutes=2, id='status_health_checks',
                               replace_existing=True)
    # Exchange-rate refresh — every 60 minutes, with an immediate first run at startup
    _status_scheduler.add_job(refresh_exchange_rates, 'interval',
                               minutes=60, id='exchange_rate_refresh',
                               replace_existing=True, next_run_time=None)
    _status_scheduler.start()

    # Kick off an immediate first refresh in a background thread so app startup
    # is not blocked by network I/O (Frankfurter usually responds in ~300ms).
    import threading
    threading.Thread(target=refresh_exchange_rates, daemon=True).start()
except Exception as _e:
    import sys
    print(f"[status scheduler] failed to start: {_e}", file=sys.stderr, flush=True)



if __name__ == "__main__":
    app.run(debug=True, port=5000)
