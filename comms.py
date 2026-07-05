"""
comms.py — SOHANA unified communications layer
Wraps SendGrid (email) and Twilio (SMS).

Key behaviour:
  - If a SendGrid dynamic template ID is set, uses it.
  - If not, falls back to inline branded HTML — emails STILL send.
  - Either service degrades gracefully if not configured (logs warning, returns False).
"""

import os, random, string, uuid
from datetime import datetime, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────

SENDGRID_API_KEY      = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL   = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@sohana.app")
SENDGRID_FROM_NAME    = os.environ.get("SENDGRID_FROM_NAME",  "SOHANA")

TWILIO_ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN",  "")
TWILIO_FROM_NUMBER    = os.environ.get("TWILIO_FROM_NUMBER", "")

# Dynamic template IDs — optional. Falls back to inline HTML if missing.
TEMPLATES = {
    "2fa":          os.environ.get("SENDGRID_TEMPLATE_2FA",          ""),
    "notification": os.environ.get("SENDGRID_TEMPLATE_NOTIFICATION", ""),
    "reset_pw":     os.environ.get("SENDGRID_TEMPLATE_RESET_PW",     ""),
    "welcome":      os.environ.get("SENDGRID_TEMPLATE_WELCOME",      ""),
    "kyc_update":   os.environ.get("SENDGRID_TEMPLATE_KYC_UPDATE",   ""),
    "contribution": os.environ.get("SENDGRID_TEMPLATE_CONTRIBUTION", ""),
    "payout":       os.environ.get("SENDGRID_TEMPLATE_PAYOUT",       ""),
}

OTP_LENGTH  = 6
OTP_EXPIRY  = 10  # minutes

# ── INLINE HTML TEMPLATES (fallback when no SendGrid template ID is set) ──────

_BASE_STYLE = """
body{margin:0;padding:0;background:#f4f2ec;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif}
.wrap{max-width:540px;margin:32px auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e0ddd5}
.header{background:#0e120f;padding:28px 32px;text-align:center}
.logo{color:#9ee493;font-size:22px;font-weight:700;letter-spacing:-.02em}
.body{padding:32px}
.body p{color:#3d3d3d;font-size:15px;line-height:1.65;margin:0 0 16px}
.code-box{background:#0e120f;border-radius:10px;padding:22px;text-align:center;margin:24px 0}
.code{color:#9ee493;font-size:40px;font-weight:700;letter-spacing:.18em;font-family:'Courier New',monospace}
.highlight{background:#f7f6f2;border-left:3px solid #9ee493;padding:14px 18px;border-radius:4px;margin:20px 0}
.highlight .label{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.08em;font-weight:600}
.highlight .value{font-size:17px;color:#0e120f;font-weight:700;margin-top:4px}
.btn{display:inline-block;background:#9ee493;color:#0e120f;padding:13px 28px;border-radius:99px;font-weight:700;font-size:15px;text-decoration:none;margin:20px 0}
.warn{background:#fff8e6;border:1px solid #ffe49e;border-radius:8px;padding:12px 16px;font-size:13px;color:#7a5f00;margin-top:20px}
.footer{background:#f7f6f2;padding:20px 32px;text-align:center;font-size:12px;color:#aaa;line-height:1.6}
"""

def _build_html(title: str, body_html: str, unsubscribe_url: str = None) -> str:
    """
    Build a branded HTML email. If unsubscribe_url is provided, appends the
    GDPR-mandated marketing footer with the unsubscribe link and postal address.
    Only pass unsubscribe_url for MARKETING emails — transactional emails
    (password reset, OTP, receipts) legally do not need one and shouldn\'t
    show it (looks weird on a password reset).
    """
    marketing_footer = ""
    if unsubscribe_url:
        marketing_footer = f"""<br><br>
    <span style="color:#aaa;font-size:11px">
      You received this because you opted in to SOHANA updates.<br>
      <a href="{unsubscribe_url}" style="color:#9ee493;text-decoration:underline">Unsubscribe</a> · <a href="https://sohana.app/privacy" style="color:#9ee493">Privacy</a><br>
      SOHANA SAS · Paris, France
    </span>"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{_BASE_STYLE}</style>
</head><body>
<div class="wrap">
  <div class="header"><div class="logo">S · SOHANA</div></div>
  <div class="body">{body_html}</div>
  <div class="footer">
    SOHANA  ·  sohana.app<br>
    Questions? <a href="mailto:{SENDGRID_FROM_EMAIL}" style="color:#9ee493">{SENDGRID_FROM_EMAIL}</a><br>
    <span style="color:#ccc">&copy; {datetime.now().year} SOHANA</span>{marketing_footer}
  </div>
</div>
</body></html>"""


def _inline_html(template_key: str, data: dict, to_name: str) -> tuple[str, str]:
    """
    Returns (subject, html_body) for each template type.
    Used when no SendGrid dynamic template ID is configured.
    """
    name = to_name or data.get("name", "Member")

    if template_key == "reset_pw":
        link = data.get("reset_link", "#")
        ttl  = data.get("otp_ttl", "60 minutes")
        subj = "Reset your SOHANA password"
        body = f"""
<p>Hi {name},</p>
<p>We received a request to reset your SOHANA password. Click the button below to choose a new one.</p>
<a class="btn" href="{link}">Reset my password</a>
<div class="highlight">
  <div class="label">This link expires in</div>
  <div class="value">{ttl}</div>
</div>
<div class="warn">
  If you did not request a password reset, ignore this email. Your password will not change.
</div>"""
        return subj, _build_html(subj, body)

    if template_key == "2fa":
        code    = data.get("otp_code", "------")
        purpose = data.get("otp_purpose", "verification")
        ttl     = data.get("otp_ttl", "10 minutes")
        subj    = f"Your SOHANA {purpose} code: {code}"
        body    = f"""
<p>Hi {name},</p>
<p>Your SOHANA {purpose} code is:</p>
<div class="code-box"><div class="code">{code}</div></div>
<div class="highlight">
  <div class="label">Expires in</div>
  <div class="value">{ttl}</div>
</div>
<div class="warn">Never share this code. SOHANA will never ask for it by phone or chat.</div>"""
        return subj, _build_html(subj, body)

    if template_key == "welcome":
        hanatag = data.get("hanatag", "")
        subj    = "Welcome to SOHANA 🎉"
        body    = f"""
<p>Hi {name},</p>
<p>Welcome to SOHANA — your community savings platform built for the African diaspora.</p>
{f'<div class="highlight"><div class="label">Your Hanatag</div><div class="value">{hanatag}</div></div>' if hanatag else ''}
<p>You can now join or create a Njangi circle, send money with your @handle, and build your Njangi Credit Score.</p>
<a class="btn" href="https://sohana.app/dashboard">Go to my dashboard</a>"""
        return subj, _build_html(subj, body)

    if template_key == "kyc_update":
        status  = data.get("kyc_status", "updated")
        level   = data.get("kyc_level",  "")
        subj    = f"SOHANA — Your identity verification is {status}"
        body    = f"""
<p>Hi {name},</p>
<p>Your identity verification status has been updated.</p>
<div class="highlight">
  <div class="label">KYC Status</div>
  <div class="value">{status.title()}{(' — Level ' + str(level)) if level else ''}</div>
</div>
<p>If you have questions, contact our support team.</p>
<a class="btn" href="https://sohana.app/kyc">View my verification</a>"""
        return subj, _build_html(subj, body)

    if template_key == "contribution":
        circle  = data.get("circle_name", "your circle")
        amount  = data.get("amount",      "")
        due     = data.get("due_date",    "")
        subj    = f"Contribution reminder — {circle}"
        body    = f"""
<p>Hi {name},</p>
<p>Your contribution to <strong>{circle}</strong> is due soon.</p>
<div class="highlight">
  <div class="label">Amount due</div>
  <div class="value">{amount}</div>
</div>
{f'<p><strong>Due date:</strong> {due}</p>' if due else ''}
<a class="btn" href="https://sohana.app/circles">Pay now</a>"""
        return subj, _build_html(subj, body)

    if template_key == "payout":
        circle = data.get("circle_name", "your circle")
        amount = data.get("amount", "")
        subj   = f"🎉 Your SOHANA payout is ready — {circle}"
        body   = f"""
<p>Hi {name},</p>
<p>Great news — you're the next payout recipient in <strong>{circle}</strong>!</p>
<div class="highlight">
  <div class="label">Payout amount</div>
  <div class="value">{amount}</div>
</div>
<a class="btn" href="https://sohana.app/circles">View my circle</a>"""
        return subj, _build_html(subj, body)

    # Generic notification fallback
    subject_line = data.get("subject_line", "Notification from SOHANA")
    message_body = data.get("message_body", "")
    highlight_l  = data.get("highlight_label", "")
    highlight_v  = data.get("highlight_value", "")
    cta_url      = data.get("cta_url",   "")
    cta_label    = data.get("cta_label", "Open SOHANA")
    subj         = subject_line
    body         = f"""
<p>Hi {name},</p>
<p>{message_body}</p>
{f'<div class="highlight"><div class="label">{highlight_l}</div><div class="value">{highlight_v}</div></div>' if highlight_l and highlight_v else ''}
{f'<a class="btn" href="{cta_url}">{cta_label}</a>' if cta_url else ''}"""
    return subj, _build_html(subj, body)


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_email(to_email: str, to_name: str,
               template_key: str, template_data: dict) -> bool:
    """
    Send a transactional email.

    Prefers SendGrid dynamic templates (SENDGRID_TEMPLATE_<KEY>).
    Falls back to inline branded HTML when the template ID is not set.
    Returns True on success, False on failure (never raises).
    """
    if not SENDGRID_API_KEY:
        import sys
        print("[comms.send_email] SENDGRID_API_KEY not set — email not sent",
              file=sys.stderr, flush=True)
        return False

    # Inject base variables
    data = {
        "name":           to_name or "Member",
        "platform_name":  "SOHANA",
        "support_email":  SENDGRID_FROM_EMAIL,
        "year":           str(datetime.now().year),
    }
    data.update(template_data or {})

    template_id = TEMPLATES.get(template_key, "")

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Mail, From, To, Subject,
            HtmlContent, DynamicTemplateData, TemplateId
        )

        if template_id:
            # ── Use SendGrid dynamic template ──
            msg = Mail(
                from_email = From(SENDGRID_FROM_EMAIL, SENDGRID_FROM_NAME),
                to_emails  = To(to_email, to_name),
            )
            msg.template_id         = TemplateId(template_id)
            msg.dynamic_template_data = DynamicTemplateData(data)
        else:
            # ── Inline HTML fallback ──
            import sys
            print(f"[comms.send_email] No template ID for '{template_key}' — using inline HTML",
                  file=sys.stderr, flush=True)
            subject, html = _inline_html(template_key, data, to_name)
            # If this is a marketing email, inject the unsubscribe footer.
            # data must contain is_marketing=True and unsubscribe_url=... — the
            # broadcast sender is responsible for providing both.
            if data.get("is_marketing") and data.get("unsubscribe_url"):
                # Re-render the body inside a _build_html shell that has the
                # marketing footer. We regenerate rather than string-injecting
                # because _inline_html already wraps in _build_html.
                # Strategy: strip the outer envelope and rewrap.
                body_only = html
                # Extract just the inner body — the templates all use the same shell.
                import re as _re
                m = _re.search(r'<div class="body">(.*?)</div>\s*<div class="footer">', body_only, _re.DOTALL)
                if m:
                    inner = m.group(1)
                    html  = _build_html(subject, inner, unsubscribe_url=data["unsubscribe_url"])
            msg = Mail(
                from_email = From(SENDGRID_FROM_EMAIL, SENDGRID_FROM_NAME),
                to_emails  = To(to_email, to_name),
                subject    = Subject(subject),
                html_content = HtmlContent(html),
            )

        # Disable click + open tracking — prevents SendGrid rewriting links
        # through url####.sohana.app tracking domain (which has no SSL cert)
        from sendgrid.helpers.mail import TrackingSettings, ClickTracking, OpenTracking
        ts = TrackingSettings()
        ts.click_tracking = ClickTracking(enable=False, enable_text=False)
        ts.open_tracking  = OpenTracking(enable=False)
        msg.tracking_settings = ts

        sg   = SendGridAPIClient(SENDGRID_API_KEY)
        resp = sg.send(msg)
        ok   = resp.status_code in (200, 202)
        if not ok:
            import sys
            print(f"[comms.send_email] SendGrid returned {resp.status_code}: {resp.body}",
                  file=sys.stderr, flush=True)
        return ok

    except Exception as e:
        import sys
        print(f"[comms.send_email] Exception: {e}", file=sys.stderr, flush=True)
        return False


# ── SMS ───────────────────────────────────────────────────────────────────────

def send_sms(to_number: str, body: str) -> bool:
    """Send an SMS via Twilio. Returns True on success."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        import sys
        print("[comms.send_sms] Twilio not fully configured — SMS not sent",
              file=sys.stderr, flush=True)
        return False
    try:
        from twilio.rest import Client
        client  = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            body = body,
            from_= TWILIO_FROM_NUMBER,
            to   = to_number,
        )
        return message.sid is not None
    except Exception as e:
        import sys
        print(f"[comms.send_sms] Exception: {e}", file=sys.stderr, flush=True)
        return False


# ── OTP ───────────────────────────────────────────────────────────────────────

def _generate_otp() -> str:
    return "".join(random.SystemRandom().choices(string.digits, k=OTP_LENGTH))


def _store_otp(user_id: str, code: str, method: str, purpose: str) -> bool:
    """Persist OTP to the database, invalidating prior pending codes."""
    try:
        from database import get_db
        expires_at = (datetime.utcnow() + timedelta(minutes=OTP_EXPIRY)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with get_db() as db:
            db.execute(
                "UPDATE otp_requests SET used=1 WHERE user_id=? AND purpose=? AND used=0",
                (user_id, purpose)
            )
            db.execute(
                """INSERT INTO otp_requests(id,user_id,code,method,purpose,expires_at)
                   VALUES(?,?,?,?,?,?)""",
                (str(uuid.uuid4()), user_id, code, method, purpose, expires_at)
            )
        return True
    except Exception as e:
        import sys
        print(f"[comms._store_otp] {e}", file=sys.stderr, flush=True)
        return False


def verify_otp(user_id: str, code: str, purpose: str) -> bool:
    """Validate a submitted OTP. Marks it used on success."""
    try:
        from database import fetchone, get_db
        row = fetchone(
            """SELECT id FROM otp_requests
               WHERE user_id=? AND code=? AND purpose=?
               AND used=0 AND expires_at > datetime('now')""",
            (user_id, code, purpose)
        )
        if not row:
            return False
        with get_db() as db:
            db.execute("UPDATE otp_requests SET used=1 WHERE id=?", (row["id"],))
        return True
    except Exception as e:
        import sys
        print(f"[comms.verify_otp] {e}", file=sys.stderr, flush=True)
        return False


def send_otp(user_id: str, method: str, purpose: str,
             to_address: str = "", to_name: str = "") -> bool:
    """Generate, store, and deliver an OTP via SMS or email."""
    code = _generate_otp()
    if not _store_otp(user_id, code, method, purpose):
        return False

    purpose_labels = {
        "2fa":      "Two-factor authentication",
        "payment":  "Payment verification",
        "login":    "Sign-in verification",
        "register": "Registration",
    }
    label = purpose_labels.get(purpose, purpose.replace("_", " ").title())

    if method == "sms":
        return send_sms(
            to_number = to_address,
            body      = f"Your SOHANA {label} code: {code}. Valid for {OTP_EXPIRY} minutes. Never share this code."
        )
    else:
        return send_email(
            to_email      = to_address,
            to_name       = to_name,
            template_key  = "2fa",
            template_data = {
                "otp_code":    code,
                "otp_purpose": label,
                "otp_ttl":     f"{OTP_EXPIRY} minutes",
            }
        )


# ── HIGH-LEVEL NOTIFICATION HELPERS ──────────────────────────────────────────

def notify_kyc_update(to_email: str, to_name: str,
                      kyc_status: str, kyc_level: int = None) -> bool:
    return send_email(to_email, to_name, "kyc_update", {
        "kyc_status": kyc_status,
        "kyc_level":  str(kyc_level) if kyc_level else "",
    })


def notify_contribution_reminder(to_email: str, to_name: str,
                                  circle_name: str, amount: str,
                                  due_date: str = "") -> bool:
    return send_email(to_email, to_name, "contribution", {
        "circle_name": circle_name,
        "amount":      amount,
        "due_date":    due_date,
    })


def notify_payout(to_email: str, to_name: str,
                   circle_name: str, amount: str) -> bool:
    return send_email(to_email, to_name, "payout", {
        "circle_name": circle_name,
        "amount":      amount,
    })


def notify_welcome(to_email: str, to_name: str, hanatag: str = "") -> bool:
    return send_email(to_email, to_name, "welcome", {"hanatag": hanatag})


# ── STATUS CHECK ─────────────────────────────────────────────────────────────

def is_configured() -> dict:
    return {
        "sendgrid": bool(SENDGRID_API_KEY),
        "twilio":   bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER),
        "templates": {k: bool(v) for k, v in TEMPLATES.items()},
        "inline_fallback": "active — emails send even without template IDs",
        "from_email": SENDGRID_FROM_EMAIL,
    }
