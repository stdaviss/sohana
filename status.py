"""
status.py — Health checks, status aggregation, and incident helpers for /service-status

Follows the same module style as wallet.py / pool.py — thin helper functions
called from app.py routes. All DB access goes through database.get_db /
fetchone / fetchall, matching the project-wide pattern.
"""
import time
import uuid
from datetime import datetime, timedelta

from database import get_db, fetchone, fetchall
import comms

# Status severity ranking — higher number = worse
STATUS_RANK = {
    'operational':    0,
    'maintenance':    1,
    'degraded':       2,
    'partial_outage': 3,
    'major_outage':   4,
}

STATUS_LABELS = {
    'operational':    'Operational',
    'degraded':       'Degraded Performance',
    'partial_outage': 'Partial Outage',
    'major_outage':   'Major Outage',
    'maintenance':    'Under Maintenance',
}


# ── HEALTH CHECKS ────────────────────────────────────────────────────────────

def check_database():
    """Returns (is_up: bool, response_ms: int|None)."""
    start = time.time()
    try:
        fetchone("SELECT 1")
        return True, int((time.time() - start) * 1000)
    except Exception:
        return False, None


def check_comms():
    """Checks SendGrid/Twilio config status — returns dict {sendgrid: bool, twilio: bool, ...}."""
    try:
        return comms.is_configured()
    except Exception:
        return {"sendgrid": False, "twilio": False}


def run_health_checks():
    """
    Runs all internal health checks and writes results to status_checks_log.
    Also updates service_components.status for any component whose checks
    failed, UNLESS that component is currently in 'maintenance' (manual
    override wins). Intended to be called every 2 minutes by APScheduler.
    """
    db_up, db_ms = check_database()

    log_check('web_app', True, 0)          # if this code is running, web_app is up
    log_check('auth', db_up, db_ms)        # auth depends on DB
    log_check('wallet', db_up, db_ms)      # wallet depends on DB
    log_check('circles_pools', db_up, db_ms)
    log_check('hanapay', db_up, db_ms)

    comms_cfg = check_comms()
    comms_up  = bool(comms_cfg.get('sendgrid', False) or comms_cfg.get('twilio', False))
    log_check('comms', comms_up, None)

    for component_id, is_up in [
        ('auth', db_up), ('wallet', db_up),
        ('circles_pools', db_up), ('hanapay', db_up),
        ('comms', comms_up), ('web_app', True),
    ]:
        _maybe_update_component_status(component_id, is_up)


def log_check(component_id, is_up, response_ms, source='internal'):
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO status_checks_log (id, component_id, is_up, response_ms, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), component_id, 1 if is_up else 0, response_ms, source)
            )
    except Exception:
        pass


def _maybe_update_component_status(component_id, is_up):
    row = fetchone("SELECT status FROM service_components WHERE id=?", (component_id,))
    if not row:
        return
    current = dict(row)['status']
    if current == 'maintenance':
        return  # manual override — do not auto-change
    new_status = 'operational' if is_up else 'major_outage'
    if new_status != current:
        try:
            with get_db() as db:
                db.execute(
                    "UPDATE service_components SET status=?, updated_at=datetime('now') WHERE id=?",
                    (new_status, component_id)
                )
        except Exception:
            pass


# ── AGGREGATION ──────────────────────────────────────────────────────────────

def overall_status():
    """Returns the worst status across all components, for the top banner."""
    rows = fetchall("SELECT status FROM service_components")
    if not rows:
        return 'operational'
    worst = max((dict(r)['status'] for r in rows),
                 key=lambda s: STATUS_RANK.get(s, 0))
    return worst


def uptime_percentage(component_id, days=90):
    """
    Returns {"percentage": float, "days": [{"date": "YYYY-MM-DD", "status": "up|down|partial"}]}
    for a component over the last N days.
    """
    since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    rows = fetchall(
        "SELECT is_up, checked_at FROM status_checks_log "
        "WHERE component_id=? AND checked_at >= ? ORDER BY checked_at ASC",
        (component_id, since)
    )
    if not rows:
        return {"percentage": 100.0, "days": []}

    total = len(rows)
    up    = sum(1 for r in rows if dict(r)['is_up'] == 1)
    percentage = round((up / total) * 100, 2) if total else 100.0

    day_status = {}
    for r in rows:
        r = dict(r)
        day = r['checked_at'][:10]
        day_status.setdefault(day, []).append(r['is_up'])

    days_list = []
    for day, vals in sorted(day_status.items()):
        if all(vals):
            day_state = "up"
        elif not any(vals):
            day_state = "down"
        else:
            day_state = "partial"
        days_list.append({"date": day, "status": day_state})

    return {"percentage": percentage, "days": days_list}


# ── INCIDENTS ────────────────────────────────────────────────────────────────

def create_incident(title, component_id, severity, status, message,
                     scheduled_start=None, scheduled_end=None):
    incident_id = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            "INSERT INTO status_incidents "
            "(id, title, component_id, severity, status, scheduled_start, scheduled_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (incident_id, title, component_id, severity, status, scheduled_start, scheduled_end)
        )
        db.execute(
            "INSERT INTO status_incident_updates (id, incident_id, message) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), incident_id, message)
        )
    notify_subscribers(f"New incident: {title}", message)
    return incident_id


def add_incident_update(incident_id, message, new_status=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO status_incident_updates (id, incident_id, message) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), incident_id, message)
        )
        if new_status:
            if new_status == 'resolved':
                db.execute(
                    "UPDATE status_incidents SET status=?, resolved_at=datetime('now') WHERE id=?",
                    (new_status, incident_id)
                )
            else:
                db.execute(
                    "UPDATE status_incidents SET status=? WHERE id=?",
                    (new_status, incident_id)
                )

    incident = fetchone("SELECT title FROM status_incidents WHERE id=?", (incident_id,))
    if incident:
        notify_subscribers(f"Update: {dict(incident)['title']}", message)


def notify_subscribers(subject, message):
    """Sends incident notification emails to all confirmed subscribers via comms.py."""
    try:
        subs = fetchall("SELECT email FROM status_subscribers WHERE is_confirmed=1")
        for s in subs:
            email = dict(s)['email']
            comms.send_email(
                to_email      = email,
                to_name       = "SOHANA Status Subscriber",
                template_key  = "notification",
                template_data = {
                    "subject_line": subject,
                    "message_body": message,
                    "cta_label":    "View status page",
                    "cta_url":      "https://sohana.app/service-status",
                }
            )
    except Exception:
        pass  # never let notification failures break incident creation
