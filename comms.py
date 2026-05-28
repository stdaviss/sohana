# comms.py — Unified messaging layer for SOHANA
# Handles: Email (SendGrid dynamic templates) + SMS (Twilio)
# Drop this file next to app.py in the project root.

import os, random, string, uuid, sys
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — all sourced from Railway environment variables
# ─────────────────────────────────────────────────────────────────────────────

# SendGrid
SENDGRID_API_KEY    = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@sohana.app")
SENDGRID_FROM_NAME  = os.environ.get("SENDGRID_FROM_NAME", "SOHANA")

# SendGrid dynamic template IDs (fill each after creating in SG dashboard)
TEMPLATES = {
    "2fa":          os.environ.get("SENDGRID_TEMPLATE_2FA", ""),
    "reset_pw":     os.environ.get("SENDGRID_TEMPLATE_RESET_PW", ""),
    "welcome":      os.environ.get("SENDGRID_TEMPLATE_WELCOME", ""),
    "kyc_update":   os.environ.get("SENDGRID_TEMPLATE_KYC_UPDATE", ""),
    "contribution": os.environ.get("SENDGRID_TEMPLATE_CONTRIBUTION", ""),
    "payout":       os.environ.get("SENDGRID_TEMPLATE_PAYOUT", ""),
    "notification": os.environ.get("SENDGRID_TEMPLATE_NOTIFICATION", ""),
}

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")  # E.164, e.g. +12015551234

# OTP settings
OTP_LENGTH   = 6
OTP_TTL_MINS = 10

# ─────────────────────────────────────────────────────────────────────────────
# Internal client factories (lazy — only import libs when actually needed)
# ─────────────────────────────────────────────────────────────────────────────

def _sg_client():
    """Return a live SendGrid client, or None if API key is not configured."""
    if not SENDGRID_API_KEY:
        print("[comms] SENDGRID_API_KEY not set — email disabled", file=sys.stderr)
        return None
    try:
        from sendgrid import SendGridAPIClient
        return SendGridAPIClient(SENDGRID_API_KEY)
    except ImportError:
        print("[comms] sendgrid package not installed", file=sys.stderr)
        return None


def _twilio_client():
    """Return a live Twilio client, or None if credentials are not configured."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("[comms] Twilio credentials not set — SMS disabled", file=sys.stderr)
        return None
    try:
        from twilio.rest import Client
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except ImportError:
        print("[comms] twilio package not installed", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Core send functions
# ─────────────────────────────────────────────────────────────────────────────

def send_email(to_email: str, to_name: str, template_key: str,
               template_data: dict = None) -> bool:
    """
    Send a transactional email via SendGrid dynamic template.

    template_key: one of the keys in TEMPLATES dict above.
    template_data: dict of Handlebars variables to merge into the template.
    Returns True on success, False on any failure (never raises).

    All templates automatically receive these base variables:
      name, platform_name, support_email, year
    """
    sg = _sg_client()
    if not sg:
        return False

    template_id = TEMPLATES.get(template_key, "")
    if not template_id:
        print(f"[comms] No template ID for '{template_key}' — set SENDGRID_TEMPLATE_{template_key.upper()} in Railway",
              file=sys.stderr)
        return False

    try:
        from sendgrid.helpers.mail import Mail, To
        msg = Mail(
            from_email=(SENDGRID_FROM_EMAIL, SENDGRID_FROM_NAME),
            to_emails=To(to_email, to_name),
        )
        msg.template_id = template_id
        msg.dynamic_template_data = {
            # Base variables always available in every template
            "name":          to_name,
            "platform_name": "SOHANA",
            "support_email": "support@sohana.app",
            "year":          str(datetime.utcnow().year),
            # Caller overrides
            **(template_data or {}),
        }
        resp = sg.send(msg)
        success = resp.status_code in (200, 201, 202)
        if not success:
            print(f"[comms] SendGrid returned {resp.status_code}", file=sys.stderr)
        return success
    except Exception as e:
        print(f"[comms] SendGrid error: {e}", file=sys.stderr)
        return False


def send_sms(to_number: str, body: str) -> bool:
    """
    Send an SMS via Twilio.
    to_number must be E.164 format (+33612345678 etc.)
    Returns True on success, False on failure (never raises).
    """
    client = _twilio_client()
    if not client:
        return False
    if not TWILIO_FROM_NUMBER:
        print("[comms] TWILIO_FROM_NUMBER not set", file=sys.stderr)
        return False
    try:
        msg = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=to_number)
        return bool(msg.sid)
    except Exception as e:
        print(f"[comms] Twilio error: {e}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# OTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _generate_otp() -> str:
    """Cryptographically random 6-digit string."""
    return "".join(random.SystemRandom().choices(string.digits, k=OTP_LENGTH))


def _store_otp(user_id: str, code: str, method: str, purpose: str) -> str:
    """
    Persist OTP to the otp_requests table. Invalidates any existing
    pending OTPs for the same user+purpose before inserting.
    Returns the new otp_id.
    """
    from database import get_db
    otp_id  = str(uuid.uuid4())
    expires = (datetime.utcnow() + timedelta(minutes=OTP_TTL_MINS)).isoformat()
    with get_db() as db:
        # Invalidate prior codes for this purpose (prevents replay after resend)
        db.execute(
            "UPDATE otp_requests SET used=1 WHERE user_id=? AND purpose=? AND used=0",
            (user_id, purpose)
        )
        db.execute(
            """INSERT INTO otp_requests(id, user_id, code, method, purpose, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (otp_id, user_id, code, method, purpose, expires)
        )
    return otp_id


def verify_otp(user_id: str, code: str, purpose: str = "2fa") -> bool:
    """
    Validate a submitted OTP code. Returns True if:
      - A matching unused code exists for this user+purpose
      - The code has not expired

    Marks the code as used immediately on success (single-use).
    """
    from database import fetchone, get_db

    row = fetchone(
        """SELECT id, expires_at FROM otp_requests
           WHERE user_id=? AND code=? AND purpose=? AND used=0
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, code, purpose)
    )
    if not row:
        return False
    if datetime.utcnow().isoformat() > row["expires_at"]:
        return False  # expired
    with get_db() as db:
        db.execute("UPDATE otp_requests SET used=1 WHERE id=?", (row["id"],))
    return True


def send_otp(user_id: str, method: str, purpose: str = "2fa") -> bool:
    """
    High-level function: generate, store, and send an OTP to the user.

    method:  'sms' | 'email'
    purpose: '2fa' | 'reset_pw' | 'email_verify'

    Returns True if delivery succeeded.

    Usage in routes:
        ok = comms.send_otp(user_id, method='sms', purpose='2fa')
        if not ok:
            return jsonify({"error": "Could not send code"}), 500
    """
    from database import fetchone

    user = fetchone(
        "SELECT full_name, email, phone FROM users WHERE id=?", (user_id,)
    )
    if not user:
        return False

    code = _generate_otp()
    _store_otp(user_id, code, method, purpose)

    purpose_labels = {
        "2fa":          "sign-in verification",
        "reset_pw":     "password reset",
        "email_verify": "email verification",
    }
    label = purpose_labels.get(purpose, "verification")

    if method == "sms":
        body = (
            f"Your SOHANA {label} code is:\n\n"
            f"{code}\n\n"
            f"Valid for {OTP_TTL_MINS} minutes. Never share this code."
        )
        return send_sms(user["phone"], body)

    elif method == "email":
        if not user.get("email"):
            print(f"[comms] User {user_id} has no email address", file=sys.stderr)
            return False
        return send_email(
            to_email=user["email"],
            to_name=user["full_name"],
            template_key=purpose if purpose in TEMPLATES else "2fa",
            template_data={
                "otp_code":    code,
                "otp_purpose": label,
                "otp_ttl":     str(OTP_TTL_MINS),
            },
        )

    print(f"[comms] Unknown OTP method: {method}", file=sys.stderr)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Convenience notification senders
# ─────────────────────────────────────────────────────────────────────────────

def notify_kyc_update(user_email: str, user_name: str, status: str,
                      level: str, note: str = "") -> bool:
    """Send a KYC status update email."""
    return send_email(
        to_email=user_email,
        to_name=user_name,
        template_key="kyc_update",
        template_data={
            "kyc_status": status,        # 'approved' | 'rejected' | 'pending'
            "kyc_level":  level,         # 'id' | 'address' | 'funds'
            "kyc_note":   note or "",
        },
    )


def notify_contribution_reminder(user_email: str, user_name: str,
                                  circle_name: str, amount_display: str,
                                  due_date: str) -> bool:
    """Send a circle contribution reminder."""
    return send_email(
        to_email=user_email,
        to_name=user_name,
        template_key="contribution",
        template_data={
            "circle_name":    circle_name,
            "amount_display": amount_display,
            "due_date":       due_date,
        },
    )


def notify_payout(user_email: str, user_name: str,
                  circle_name: str, amount_display: str) -> bool:
    """Notify a user they are the next payout recipient."""
    return send_email(
        to_email=user_email,
        to_name=user_name,
        template_key="payout",
        template_data={
            "circle_name":    circle_name,
            "amount_display": amount_display,
        },
    )


def notify_welcome(user_email: str, user_name: str, hanatag: str) -> bool:
    """Send a welcome email after registration."""
    return send_email(
        to_email=user_email,
        to_name=user_name,
        template_key="welcome",
        template_data={
            "hanatag": hanatag or "",
        },
    )


def is_configured() -> dict:
    """
    Utility: returns dict showing what's configured.
    Useful for the admin system health panel.
    """
    return {
        "sendgrid": bool(SENDGRID_API_KEY),
        "twilio":   bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
        "templates": {k: bool(v) for k, v in TEMPLATES.items()},
    }
