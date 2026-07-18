# ─────────────────────────────────────────────────────────────────────────────
# FILE: requirements.txt  (full replacement)
# ─────────────────────────────────────────────────────────────────────────────
flask==3.1.0
gunicorn==21.2.0
sendgrid==6.11.0
twilio==9.3.5


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION: Add to the safe_migrations list in database.py → init_db()
# Add this block AFTER the existing migrations (before the closing bracket).
# ─────────────────────────────────────────────────────────────────────────────

"""CREATE TABLE IF NOT EXISTS otp_requests (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    code        TEXT NOT NULL,
    method      TEXT NOT NULL DEFAULT 'sms',
    purpose     TEXT NOT NULL DEFAULT '2fa',
    used        INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)""",
"CREATE INDEX IF NOT EXISTS idx_otp_user ON otp_requests(user_id, purpose, used, created_at DESC)",


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION: Add to users table — 2FA preference columns
# Add these alongside the other ALTER TABLE users migrations.
# ─────────────────────────────────────────────────────────────────────────────

"ALTER TABLE users ADD COLUMN twofa_enabled  INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE users ADD COLUMN twofa_method   TEXT NOT NULL DEFAULT 'sms'",


# ─────────────────────────────────────────────────────────────────────────────
# RAILWAY ENVIRONMENT VARIABLES TO ADD
# Settings → Service → Variables in Railway dashboard
# ─────────────────────────────────────────────────────────────────────────────

# -- SendGrid --
SENDGRID_API_KEY           = "SG.xxxxxxxxxxxxxxxxxxxx"   # from SendGrid → API Keys
SENDGRID_FROM_EMAIL        = "noreply@sohana.app"
SENDGRID_FROM_NAME         = "SOHANA"

# Fill these AFTER creating each dynamic template in SendGrid dashboard:
SENDGRID_TEMPLATE_2FA          = "d-xxxxxxxxxxxxxxxxxxxx"
SENDGRID_TEMPLATE_RESET_PW     = "d-xxxxxxxxxxxxxxxxxxxx"
SENDGRID_TEMPLATE_WELCOME      = "d-xxxxxxxxxxxxxxxxxxxx"
SENDGRID_TEMPLATE_KYC_UPDATE   = "d-xxxxxxxxxxxxxxxxxxxx"
SENDGRID_TEMPLATE_CONTRIBUTION = "d-xxxxxxxxxxxxxxxxxxxx"
SENDGRID_TEMPLATE_PAYOUT       = "d-xxxxxxxxxxxxxxxxxxxx"
SENDGRID_TEMPLATE_NOTIFICATION = "d-xxxxxxxxxxxxxxxxxxxx"

# -- Twilio --
TWILIO_ACCOUNT_SID   = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # from Twilio Console
TWILIO_AUTH_TOKEN    = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_FROM_NUMBER   = "+12015551234"   # your Twilio phone number in E.164 format


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION SNIPPET: Add to app.py (near top, after other imports)
# ─────────────────────────────────────────────────────────────────────────────

import comms


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION: New routes to add to app.py
# These handle OTP send + verify for both user and admin login flows.
# ─────────────────────────────────────────────────────────────────────────────

# POST /api/auth/send-otp
# Called after password is verified. Sends OTP before completing login.
@app.route("/api/auth/send-otp", methods=["POST"])
def api_send_otp():
    d       = request.json or {}
    user_id = d.get("user_id", "")
    method  = d.get("method", "sms")   # 'sms' or 'email'
    purpose = d.get("purpose", "2fa")

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    if method not in ("sms", "email"):
        return jsonify({"error": "method must be sms or email"}), 400

    ok = comms.send_otp(user_id, method=method, purpose=purpose)
    if not ok:
        return jsonify({"error": "Could not send code. Please try again."}), 500
    return jsonify({"ok": True, "method": method})


# POST /api/auth/verify-otp
# Verifies OTP and, if valid, completes the session.
@app.route("/api/auth/verify-otp", methods=["POST"])
def api_verify_otp():
    d       = request.json or {}
    user_id = d.get("user_id", "")
    code    = d.get("code", "").strip()
    purpose = d.get("purpose", "2fa")

    if not user_id or not code:
        return jsonify({"error": "user_id and code are required"}), 400

    if not comms.verify_otp(user_id, code, purpose):
        return jsonify({"error": "Invalid or expired code."}), 401

    # OTP valid — complete the session
    user = fetchone(
        "SELECT id, full_name, is_admin, admin_role FROM users WHERE id=?",
        (user_id,)
    )
    if not user:
        return jsonify({"error": "User not found"}), 404

    session["user_id"]   = user["id"]
    session["user_name"] = user["full_name"]
    if user["is_admin"]:
        session["is_admin"]   = True
        session["admin_role"] = user["admin_role"]

    return jsonify({"ok": True, "role": user.get("admin_role") or "user"})
