"""
pool.py — Contribution Pool Engine
Handles long-lived collective savings pools (funeral funds, wedding pools,
association treasuries, joint project funds) with 3-admin co-authorisation
on all disbursements.
"""
import uuid
from datetime import datetime, date
from calendar import monthrange
from database import (get_db, fetchone, fetchall, post_transaction,
                      push_notification, ROSCA_CREATION_FEES)
import ncs_engine

# ── PURPOSE CATEGORIES ────────────────────────────────────────────────────────
POOL_PURPOSES = {
    "funeral":    {"label": "Funeral & Bereavement",  "icon": "🕊"},
    "wedding":    {"label": "Wedding",                 "icon": "💍"},
    "birth":      {"label": "Birth & Newborn",         "icon": "👶"},
    "project":    {"label": "Joint Project",           "icon": "🏗"},
    "education":  {"label": "Education Fund",          "icon": "🎓"},
    "health":     {"label": "Medical Fund",            "icon": "🏥"},
    "business":   {"label": "Business Fund",           "icon": "💼"},
    "general":    {"label": "General Association",     "icon": "🤝"},
}

# ── PAYMENT SCHEDULES ─────────────────────────────────────────────────────────
PAYMENT_SCHEDULES = {
    "monthly":   {"label": "Monthly",           "months": 1},
    "quarterly": {"label": "Every 3 months",    "months": 3},
    "biannual":  {"label": "Every 6 months",    "months": 6},
    "annual":    {"label": "Annually (full year)", "months": 12},
}

# ── CREATE ────────────────────────────────────────────────────────────────────

def create_pool(organiser_id, name, description, purpose,
                annual_amount_cents, duration_months=12,
                payout_type="single", currency="EUR",
                ncs_min=300, is_public=False):
    """
    Create a contribution pool. The organiser becomes the first admin.
    Returns (pool_id, creation_fee_cents).
    """
    user     = fetchone("SELECT ncs_tier FROM users WHERE id=?", (organiser_id,))
    tier     = user["ncs_tier"] if user else "probation"
    fee      = ROSCA_CREATION_FEES.get(tier, 500)
    monthly  = annual_amount_cents // 12
    pid      = str(uuid.uuid4())

    with get_db() as db:
        db.execute("""INSERT INTO pools(id,name,description,purpose,organiser_id,
                      currency,annual_amount_cents,monthly_amount_cents,
                      payout_type,duration_months,ncs_min_score,is_public,creation_fee_cents)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (pid, name, description, purpose, organiser_id,
                    currency, annual_amount_cents, monthly,
                    payout_type, duration_months, ncs_min,
                    1 if is_public else 0, fee))
        # Organiser is first admin automatically
        db.execute("""INSERT INTO pool_members(id,pool_id,user_id,role,status,payment_schedule)
                      VALUES(?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), pid, organiser_id, "admin", "active", "monthly"))
    ncs_engine.apply_event(organiser_id, "contribution_on_time",
                           ref_type="pool_created", ref_id=pid)
    return pid, fee


def get_pool(pool_id):
    return fetchone("""SELECT p.*, u.full_name as organiser_name
                       FROM pools p JOIN users u ON u.id=p.organiser_id
                       WHERE p.id=?""", (pool_id,))


def get_user_pools(user_id):
    return fetchall("""SELECT p.*, pm.role, pm.status as mem_status, pm.payment_schedule,
                              COUNT(pm2.id) as member_count
                       FROM pool_members pm
                       JOIN pools p ON p.id=pm.pool_id
                       LEFT JOIN pool_members pm2 ON pm2.pool_id=p.id AND pm2.status='active'
                       WHERE pm.user_id=?
                       GROUP BY p.id ORDER BY p.created_at DESC""", (user_id,))


def get_marketplace_pools(search=None, limit=20):
    sql = """SELECT p.*, u.full_name as organiser_name, COUNT(pm.id) as member_count
             FROM pools p JOIN users u ON u.id=p.organiser_id
             LEFT JOIN pool_members pm ON pm.pool_id=p.id AND pm.status='active'
             WHERE p.is_public=1 AND p.status IN ('forming','active')"""
    params = []
    if search:
        sql += " AND (p.name LIKE ? OR p.description LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " GROUP BY p.id ORDER BY p.created_at DESC LIMIT ?"
    params.append(limit)
    return fetchall(sql, params)


# ── MEMBERSHIP ────────────────────────────────────────────────────────────────

def request_to_join_pool(pool_id, user_id, payment_schedule="monthly"):
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))
    if not p: raise ValueError("Pool not found")
    if p["status"] not in ("forming", "active"): raise ValueError("Pool not accepting members")
    user = fetchone("SELECT ncs_score FROM users WHERE id=?", (user_id,))
    if user["ncs_score"] < p["ncs_min_score"]:
        raise ValueError(f"Minimum NCS score of {p['ncs_min_score']} required")
    existing = fetchone("SELECT id, status FROM pool_members WHERE pool_id=? AND user_id=?", (pool_id, user_id))
    if existing:
        if existing["status"] == "pending": raise ValueError("Join request already pending")
        raise ValueError("Already a member")
    if payment_schedule not in PAYMENT_SCHEDULES:
        payment_schedule = "monthly"
    with get_db() as db:
        db.execute("""INSERT INTO pool_members(id,pool_id,user_id,role,status,payment_schedule)
                      VALUES(?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), pool_id, user_id, "member", "pending", payment_schedule))
    admins = get_pool_admins(pool_id)
    for admin in admins:
        push_notification(admin["user_id"], "New member request",
                          f"Someone wants to join '{p['name']}'.",
                          "info", f"/pools/{pool_id}/manage")


def approve_pool_member(pool_id, user_id, admin_id):
    _require_admin(pool_id, admin_id)
    m = fetchone("SELECT * FROM pool_members WHERE pool_id=? AND user_id=? AND status='pending'", (pool_id, user_id))
    if not m: raise ValueError("No pending request found")
    with get_db() as db:
        db.execute("UPDATE pool_members SET status='active' WHERE pool_id=? AND user_id=?", (pool_id, user_id))
    push_notification(user_id, "Pool membership approved ✓",
                      f"You are now a member of '{fetchone('SELECT name FROM pools WHERE id=?',(pool_id,))['name']}'.",
                      "success", f"/pools/{pool_id}")


def reject_pool_member(pool_id, user_id, admin_id):
    _require_admin(pool_id, admin_id)
    with get_db() as db:
        db.execute("DELETE FROM pool_members WHERE pool_id=? AND user_id=? AND status='pending'", (pool_id, user_id))
    push_notification(user_id, "Pool request declined",
                      f"Your request to join a pool was not approved.", "warning")


def remove_pool_member(pool_id, user_id, admin_id, reason=""):
    _require_admin(pool_id, admin_id)
    if user_id == fetchone("SELECT organiser_id FROM pools WHERE id=?", (pool_id,))["organiser_id"]:
        raise ValueError("Cannot remove the pool organiser")
    with get_db() as db:
        db.execute("UPDATE pool_members SET status='left' WHERE pool_id=? AND user_id=?", (pool_id, user_id))
    ncs_engine.apply_event(user_id, "dispute_raised", ref_type="pool", ref_id=pool_id,
                           metadata={"reason": reason})
    push_notification(user_id, "Removed from pool",
                      f"You have been removed from a contribution pool" + (f": {reason}" if reason else "."),
                      "danger")


def promote_to_admin(pool_id, user_id, requesting_admin_id):
    """
    Promote a member to admin. Max 3 admins per pool.
    """
    _require_admin(pool_id, requesting_admin_id)
    current_admins = get_pool_admins(pool_id)
    if len(current_admins) >= 3:
        raise ValueError("Maximum of 3 admins allowed per pool")
    m = fetchone("SELECT * FROM pool_members WHERE pool_id=? AND user_id=? AND status='active'", (pool_id, user_id))
    if not m: raise ValueError("Member not found or not active")
    with get_db() as db:
        db.execute("UPDATE pool_members SET role='admin' WHERE pool_id=? AND user_id=?", (pool_id, user_id))
    push_notification(user_id, "You are now a pool admin 👑",
                      f"You have been made an admin. You can now co-approve disbursements.",
                      "success", f"/pools/{pool_id}/manage")


def get_pool_admins(pool_id):
    return fetchall("""SELECT pm.*, u.full_name, u.hanatag
                       FROM pool_members pm JOIN users u ON u.id=pm.user_id
                       WHERE pm.pool_id=? AND pm.role='admin' AND pm.status='active'""", (pool_id,))


def get_pool_members(pool_id):
    return fetchall("""SELECT pm.*, u.full_name, u.ncs_score, u.ncs_tier, u.hanatag, u.country
                       FROM pool_members pm JOIN users u ON u.id=pm.user_id
                       WHERE pm.pool_id=? ORDER BY pm.role DESC, pm.joined_at""", (pool_id,))


def get_pending_pool_members(pool_id):
    return fetchall("""SELECT pm.*, u.full_name, u.ncs_score, u.ncs_tier, u.hanatag,
                              (SELECT COUNT(*) FROM pool_contributions WHERE payer_id=u.id) as total_contribs
                       FROM pool_members pm JOIN users u ON u.id=pm.user_id
                       WHERE pm.pool_id=? AND pm.status='pending'
                       ORDER BY pm.joined_at""", (pool_id,))


def update_payment_schedule(pool_id, user_id, new_schedule):
    if new_schedule not in PAYMENT_SCHEDULES:
        raise ValueError("Invalid payment schedule")
    with get_db() as db:
        db.execute("UPDATE pool_members SET payment_schedule=? WHERE pool_id=? AND user_id=?",
                   (new_schedule, pool_id, user_id))


# ── CONTRIBUTIONS ─────────────────────────────────────────────────────────────

def pay_pool_contribution(pool_id, payer_id, beneficiary_id, months,
                          note="", period_label=None):
    """
    Pay contribution for a member (can be yourself or someone else).
    months: 1, 3, 6, or 12
    Returns the contribution record id.
    """
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))
    if not p: raise ValueError("Pool not found")

    # Verify payer is active member
    payer_mem = fetchone("SELECT * FROM pool_members WHERE pool_id=? AND user_id=? AND status='active'", (pool_id, payer_id))
    if not payer_mem: raise ValueError("You must be an active member to contribute")

    # Verify beneficiary is a member (active or pending)
    ben_mem = fetchone("SELECT * FROM pool_members WHERE pool_id=? AND user_id=?", (pool_id, beneficiary_id))
    if not ben_mem: raise ValueError("Beneficiary is not a member of this pool")

    amount_cents = p["monthly_amount_cents"] * months
    paid_on_behalf = 1 if payer_id != beneficiary_id else 0

    # Debit payer's wallet
    wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (payer_id, p["currency"]))
    if not wallet: raise ValueError("No wallet found for this currency")

    if period_label is None:
        now = date.today()
        period_label = f"{now.year}-{now.month:02d}"

    contrib_id = str(uuid.uuid4())
    with get_db() as db:
        post_transaction(wallet["id"], -amount_cents,
                         f"Pool contribution — {p['name']}" + (f" (for {_get_name(beneficiary_id)})" if paid_on_behalf else ""),
                         ref_type="pool_contribution", ref_id=contrib_id,
                         tx_type="rosca_contribution", _db=db)
        db.execute("""INSERT INTO pool_contributions(id,pool_id,payer_id,beneficiary_id,
                      amount_cents,period_covered,months_covered,paid_on_behalf,note)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (contrib_id, pool_id, payer_id, beneficiary_id,
                    amount_cents, period_label, months, paid_on_behalf, note))
        db.execute("UPDATE pools SET total_balance_cents=total_balance_cents+?, updated_at=datetime('now') WHERE id=?",
                   (amount_cents, pool_id))

    # NCS events
    if paid_on_behalf:
        # Paying for another member is a social trust action — bonus NCS
        ncs_engine.apply_event(payer_id, "peer_endorsement",
                               ref_type="pool_paid_for_member", ref_id=contrib_id,
                               metadata={"beneficiary": beneficiary_id, "months": months})
        beneficiary_name = _get_name(beneficiary_id)
        payer_name = _get_name(payer_id)
        push_notification(beneficiary_id,
                          f"Contribution paid for you 🤝",
                          f"{payer_name} paid {months} month{'s' if months>1 else ''} of your contribution.",
                          "success", f"/pools/{pool_id}")
    else:
        ncs_engine.apply_event(payer_id, "contribution_on_time",
                               ref_type="pool_contribution", ref_id=contrib_id)

    return contrib_id


def get_member_contribution_status(pool_id, user_id):
    """
    How many months has this member covered, how many are due?
    """
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))
    mem = fetchone("SELECT * FROM pool_members WHERE pool_id=? AND user_id=?", (pool_id, user_id))
    if not mem: return None

    # All contributions where this person is beneficiary
    paid_months = fetchone("""SELECT COALESCE(SUM(months_covered),0) as m
                              FROM pool_contributions WHERE pool_id=? AND beneficiary_id=?""",
                           (pool_id, user_id))["m"]
    # Months since joining
    joined = datetime.fromisoformat(mem["joined_at"][:10])
    today  = datetime.utcnow()
    months_since = max(1, (today.year - joined.year)*12 + (today.month - joined.month))

    return {
        "months_paid":   paid_months,
        "months_due":    max(0, months_since - paid_months),
        "months_since":  months_since,
        "monthly_cents": p["monthly_amount_cents"],
        "arrears_cents": max(0, (months_since - paid_months) * p["monthly_amount_cents"]),
        "payment_schedule": mem["payment_schedule"],
    }


def get_pool_contribution_summary(pool_id):
    """
    Per-member contribution totals for the pool manage page.
    """
    members = get_pool_members(pool_id)
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))
    result = []
    for m in members:
        uid = m["user_id"]
        # Total paid as beneficiary
        paid = fetchone("SELECT COALESCE(SUM(months_covered),0) as m, COALESCE(SUM(amount_cents),0) as a FROM pool_contributions WHERE pool_id=? AND beneficiary_id=?", (pool_id, uid))
        # Total paid by others for them
        by_others = fetchone("SELECT COALESCE(SUM(amount_cents),0) as a FROM pool_contributions WHERE pool_id=? AND beneficiary_id=? AND paid_on_behalf=1", (pool_id, uid))
        # Total they paid for others
        for_others = fetchone("SELECT COALESCE(SUM(amount_cents),0) as a FROM pool_contributions WHERE pool_id=? AND payer_id=? AND paid_on_behalf=1", (pool_id, uid))
        result.append({
            **dict(m),
            "months_covered":    paid["m"],
            "total_paid_cents":  paid["a"],
            "received_help_cents": by_others["a"],
            "helped_others_cents": for_others["a"],
        })
    return result


# ── DISBURSEMENTS (3-admin co-authorisation) ───────────────────────────────────

def request_disbursement(pool_id, requester_id, amount_cents, purpose_note, recipient_id=None):
    """
    An admin requests a fund disbursement.
    Other admins must approve before funds are released.
    """
    _require_admin(pool_id, requester_id)
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))
    if amount_cents > p["total_balance_cents"]:
        raise ValueError(f"Insufficient pool balance. Available: €{p['total_balance_cents']/100:.2f}")
    if amount_cents <= 0:
        raise ValueError("Amount must be positive")

    did = str(uuid.uuid4())
    with get_db() as db:
        db.execute("""INSERT INTO pool_disbursements(id,pool_id,amount_cents,recipient_id,
                      purpose_note,status,requested_by,approved_by_1)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (did, pool_id, amount_cents, recipient_id,
                    purpose_note, "pending", requester_id, requester_id))

    # Notify all other admins
    admins = get_pool_admins(pool_id)
    for admin in admins:
        if admin["user_id"] != requester_id:
            push_notification(admin["user_id"],
                              "Disbursement approval required",
                              f"€{amount_cents/100:.2f} requested from '{p['name']}': {purpose_note}",
                              "warning", f"/pools/{pool_id}/manage#disbursements")
    return did


def approve_disbursement(pool_id, disbursement_id, admin_id):
    """
    An admin approves a pending disbursement.
    When 3 unique admins have approved, funds are released automatically.
    The requester counts as the first approver automatically.
    """
    _require_admin(pool_id, admin_id)
    d = fetchone("SELECT * FROM pool_disbursements WHERE id=? AND pool_id=?", (disbursement_id, pool_id))
    if not d: raise ValueError("Disbursement not found")
    if d["status"] != "pending": raise ValueError(f"Disbursement is already {d['status']}")
    if d["rejected_by"]: raise ValueError("This disbursement was rejected")

    # Check already approved by this admin
    already = [d["approved_by_1"], d["approved_by_2"], d["approved_by_3"]]
    if admin_id in already: raise ValueError("You have already approved this disbursement")

    # Assign to next available slot
    if not d["approved_by_2"]:
        slot = "approved_by_2"
    elif not d["approved_by_3"]:
        slot = "approved_by_3"
    else:
        # All 3 slots filled — execute
        _execute_disbursement(pool_id, disbursement_id)
        return

    with get_db() as db:
        db.execute(f"UPDATE pool_disbursements SET {slot}=? WHERE id=?", (admin_id, disbursement_id))

    # Check if we now have 3 approvals
    updated = fetchone("SELECT * FROM pool_disbursements WHERE id=?", (disbursement_id,))
    approvals = [x for x in [updated["approved_by_1"], updated["approved_by_2"], updated["approved_by_3"]] if x]
    unique_approvals = len(set(approvals))

    if unique_approvals >= 3:
        _execute_disbursement(pool_id, disbursement_id)
    else:
        # Notify remaining admins
        p = fetchone("SELECT name FROM pools WHERE id=?", (pool_id,))
        admins = get_pool_admins(pool_id)
        for admin in admins:
            if admin["user_id"] not in approvals:
                push_notification(admin["user_id"],
                                  f"Disbursement needs your approval ({unique_approvals}/3)",
                                  f"€{d['amount_cents']/100:.2f} from '{p['name']}': {d['purpose_note']}",
                                  "warning", f"/pools/{pool_id}/manage#disbursements")


def reject_disbursement(pool_id, disbursement_id, admin_id, note=""):
    """Any admin can reject a disbursement."""
    _require_admin(pool_id, admin_id)
    d = fetchone("SELECT * FROM pool_disbursements WHERE id=?", (disbursement_id,))
    if not d or d["status"] != "pending": raise ValueError("Not a pending disbursement")
    with get_db() as db:
        db.execute("UPDATE pool_disbursements SET status='rejected', rejected_by=?, rejection_note=? WHERE id=?",
                   (admin_id, note, disbursement_id))
    # Notify requester
    push_notification(d["requested_by"],
                      "Disbursement rejected",
                      f"Your disbursement request of €{d['amount_cents']/100:.2f} was rejected" + (f": {note}" if note else "."),
                      "danger")


def _execute_disbursement(pool_id, disbursement_id):
    """Internal: release funds when all approvals are in."""
    d = fetchone("SELECT * FROM pool_disbursements WHERE id=?", (disbursement_id,))
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))

    recipient_id = d["recipient_id"] or p["organiser_id"]
    wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (recipient_id, p["currency"]))

    with get_db() as db:
        if wallet:
            post_transaction(wallet["id"], d["amount_cents"],
                             f"Pool disbursement — {p['name']}: {d['purpose_note']}",
                             ref_type="pool_disbursement", ref_id=disbursement_id,
                             tx_type="rosca_payout", _db=db)
        db.execute("""UPDATE pool_disbursements SET status='executed', approved_at=datetime('now'),
                      executed_at=datetime('now') WHERE id=?""", (disbursement_id,))
        db.execute("UPDATE pools SET total_balance_cents=total_balance_cents-? WHERE id=?",
                   (d["amount_cents"], pool_id))

    push_notification(recipient_id,
                      "Pool funds disbursed ✓",
                      f"€{d['amount_cents']/100:.2f} has been sent to your wallet from '{p['name']}'.",
                      "success", "/wallet")


def get_disbursements(pool_id):
    return fetchall("""SELECT pd.*,
                              u1.full_name as requester_name,
                              u2.full_name as approver1_name,
                              u3.full_name as approver2_name,
                              u4.full_name as approver3_name,
                              u5.full_name as recipient_name
                       FROM pool_disbursements pd
                       LEFT JOIN users u1 ON u1.id=pd.requested_by
                       LEFT JOIN users u2 ON u2.id=pd.approved_by_1
                       LEFT JOIN users u3 ON u3.id=pd.approved_by_2
                       LEFT JOIN users u4 ON u4.id=pd.approved_by_3
                       LEFT JOIN users u5 ON u5.id=pd.recipient_id
                       WHERE pd.pool_id=? ORDER BY pd.created_at DESC""", (pool_id,))


# ── POOL REPORT ───────────────────────────────────────────────────────────────

def get_pool_report(pool_id):
    p = fetchone("SELECT * FROM pools WHERE id=?", (pool_id,))
    if not p: return None
    members = get_pool_contribution_summary(pool_id)
    disbursements = get_disbursements(pool_id)
    all_contribs = fetchall("""SELECT pc.*, u.full_name as payer_name, u2.full_name as beneficiary_name
                               FROM pool_contributions pc
                               JOIN users u ON u.id=pc.payer_id
                               JOIN users u2 ON u2.id=pc.beneficiary_id
                               WHERE pc.pool_id=? ORDER BY pc.created_at DESC""", (pool_id,))
    total_disbursed = sum(d["amount_cents"] for d in disbursements if d["status"] == "executed")
    total_collected = sum(c["amount_cents"] for c in all_contribs)
    on_behalf_cnt   = sum(1 for c in all_contribs if c["paid_on_behalf"])
    return {
        "pool":            dict(p),
        "members":         members,
        "disbursements":   [dict(d) for d in disbursements],
        "all_contribs":    [dict(c) for c in all_contribs],
        "total_collected": total_collected,
        "total_disbursed": total_disbursed,
        "current_balance": p["total_balance_cents"],
        "on_behalf_count": on_behalf_cnt,
    }


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _require_admin(pool_id, user_id):
    m = fetchone("SELECT role FROM pool_members WHERE pool_id=? AND user_id=? AND status='active'", (pool_id, user_id))
    if not m or m["role"] != "admin":
        raise ValueError("Admin privileges required")

def _get_name(user_id):
    row = fetchone("SELECT full_name FROM users WHERE id=?", (user_id,))
    return row["full_name"] if row else "Unknown"
