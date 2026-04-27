import uuid
from datetime import datetime, timedelta
from database import get_db, fetchone, fetchall, post_transaction, ROSCA_CREATION_FEES, push_notification
import ncs_engine

def create_rosca(organiser_id, name, description, contribution_cents,
                 max_members=12, frequency_days=30, ncs_min=300,
                 is_public=True, rosca_type="fixed_order", currency="EUR"):
    user = fetchone("SELECT ncs_tier FROM users WHERE id=?", (organiser_id,))
    tier = user["ncs_tier"] if user else "probation"
    creation_fee = ROSCA_CREATION_FEES.get(tier, 500)
    rid = str(uuid.uuid4())
    with get_db() as db:
        db.execute("""INSERT INTO roscas(id,name,description,organiser_id,rosca_type,
                      contribution_cents,currency,frequency_days,max_members,ncs_min_score,
                      is_public,total_cycles,creation_fee_cents)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, name, description, organiser_id, rosca_type,
                    contribution_cents, currency, frequency_days, max_members,
                    ncs_min, 1 if is_public else 0, max_members, creation_fee))
        db.execute("INSERT INTO rosca_members(id,rosca_id,user_id,slot,status) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), rid, organiser_id, 1, "active"))
    ncs_engine.apply_event(organiser_id, "contribution_on_time", ref_type="rosca_created", ref_id=rid)
    return rid, creation_fee

def get_rosca(rosca_id):
    return fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))

def get_marketplace(limit=20, search=None):
    sql = """SELECT r.*, u.full_name as organiser_name, COUNT(rm.id) as member_count
             FROM roscas r JOIN users u ON u.id=r.organiser_id
             LEFT JOIN rosca_members rm ON rm.rosca_id=r.id AND rm.status='active'
             WHERE r.is_public=1 AND r.status IN ('forming','active')"""
    params = []
    if search:
        sql += " AND (r.name LIKE ? OR r.description LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?"
    params.append(limit)
    return fetchall(sql, params)

def get_user_roscas(user_id):
    return fetchall("""SELECT r.*, rm.status as mem_status, rm.slot,
                              COUNT(rm2.id) as member_count
                       FROM rosca_members rm
                       JOIN roscas r ON r.id=rm.rosca_id
                       LEFT JOIN rosca_members rm2 ON rm2.rosca_id=r.id AND rm2.status='active'
                       WHERE rm.user_id=?
                       GROUP BY r.id ORDER BY r.created_at DESC""", (user_id,))

def join_rosca(rosca_id, user_id):
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r: raise ValueError("Circle not found")
    if r["status"] not in ("forming","active"): raise ValueError("This circle is not accepting members")
    user = fetchone("SELECT ncs_score FROM users WHERE id=?", (user_id,))
    if user["ncs_score"] < r["ncs_min_score"]:
        raise ValueError(f"Minimum NCS score of {r['ncs_min_score']} required")
    count = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE rosca_id=? AND status='active'", (rosca_id,))
    if count["c"] >= r["max_members"]: raise ValueError("This circle is full")
    if fetchone("SELECT id FROM rosca_members WHERE rosca_id=? AND user_id=?", (rosca_id, user_id)):
        raise ValueError("Already a member of this circle")
    with get_db() as db:
        db.execute("INSERT INTO rosca_members(id,rosca_id,user_id,slot,status) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), rosca_id, user_id, (count["c"] or 0)+1, "active"))
    organiser_id = r["organiser_id"]
    push_notification(organiser_id, "New member joined", f"Someone joined your circle '{r['name']}'.", "info", f"/organiser/{rosca_id}")

def get_or_create_active_cycle(rosca_id):
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    cycle = fetchone("SELECT * FROM cycles WHERE rosca_id=? AND status IN ('open','collecting') ORDER BY cycle_number DESC LIMIT 1", (rosca_id,))
    if cycle: return dict(cycle)
    cycle_num = (r["current_cycle"] or 0) + 1
    due = (datetime.utcnow() + timedelta(days=r["frequency_days"])).isoformat()
    members = fetchall("SELECT user_id, slot FROM rosca_members WHERE rosca_id=? AND status='active' ORDER BY slot", (rosca_id,))
    if not members: raise ValueError("No active members")
    recipient = members[(cycle_num - 1) % len(members)]["user_id"]
    cid = str(uuid.uuid4())
    with get_db() as db:
        db.execute("INSERT INTO cycles(id,rosca_id,cycle_number,recipient_id,pot_cents,status,due_at) VALUES(?,?,?,?,?,?,?)",
                   (cid, rosca_id, cycle_num, recipient, 0, "collecting", due))
        db.execute("UPDATE roscas SET current_cycle=?,updated_at=datetime('now') WHERE id=?", (cycle_num, rosca_id))
        for m in members:
            db.execute("INSERT OR IGNORE INTO contributions(id,cycle_id,rosca_id,user_id,amount_cents,status,due_at) VALUES(?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), cid, rosca_id, m["user_id"], r["contribution_cents"], "pending", due))
    return fetchone("SELECT * FROM cycles WHERE id=?", (cid,))

def pay_contribution(user_id, cycle_id):
    contrib = fetchone("SELECT * FROM contributions WHERE cycle_id=? AND user_id=? AND status='pending'", (cycle_id, user_id))
    if not contrib: raise ValueError("No pending contribution found")
    wallet = fetchone("SELECT id FROM wallets WHERE user_id=?", (user_id,))
    if not wallet: raise ValueError("Wallet not found")
    post_transaction(wallet["id"], -contrib["amount_cents"], "ROSCA contribution", "contribution", contrib["id"], tx_type="rosca_contribution")
    now = datetime.utcnow().isoformat()
    late_days = max(0, (datetime.fromisoformat(now[:10]) - datetime.fromisoformat(contrib["due_at"][:10])).days)
    status = "late" if late_days > 0 else "paid"
    with get_db() as db:
        db.execute("UPDATE contributions SET status=?,paid_at=?,late_days=? WHERE id=?", (status, now, late_days, contrib["id"]))
        # Pot grows only from actual contributions — no initial amount
        db.execute("UPDATE cycles SET pot_cents=pot_cents+? WHERE id=?", (contrib["amount_cents"], cycle_id))
    event_type = "contribution_on_time" if status == "paid" else "contribution_late"
    ncs_engine.apply_event(user_id, event_type, ref_type="contribution", ref_id=contrib["id"])
    _check_cycle_complete(cycle_id)

def _check_cycle_complete(cycle_id):
    pending = fetchone("SELECT COUNT(*) as c FROM contributions WHERE cycle_id=? AND status='pending'", (cycle_id,))
    if pending["c"] > 0: return
    cycle = fetchone("SELECT * FROM cycles WHERE id=?", (cycle_id,))
    if not cycle or cycle["status"] == "completed": return
    recipient_wallet = fetchone("SELECT id FROM wallets WHERE user_id=?", (cycle["recipient_id"],))
    if recipient_wallet:
        fee = int(cycle["pot_cents"] * 0.0125)
        net = cycle["pot_cents"] - fee
        post_transaction(recipient_wallet["id"], net, "ROSCA payout", "cycle", cycle_id, tx_type="rosca_payout")
        push_notification(cycle["recipient_id"], "You received your payout! 🎉", f"€{net/100:.2f} has been added to your wallet.", "success", "/wallet")
    with get_db() as db:
        db.execute("UPDATE cycles SET status='completed',completed_at=datetime('now') WHERE id=?", (cycle_id,))
    members = fetchall("SELECT user_id FROM rosca_members WHERE rosca_id=? AND status='active'", (cycle["rosca_id"],))
    for m in members:
        ncs_engine.apply_event(m["user_id"], "cycle_completed", ref_type="cycle", ref_id=cycle_id)

def get_cycle_status(rosca_id):
    cycle = fetchone("SELECT * FROM cycles WHERE rosca_id=? ORDER BY cycle_number DESC LIMIT 1", (rosca_id,))
    if not cycle: return None
    contribs = fetchall("SELECT * FROM contributions WHERE cycle_id=?", (cycle["id"],))
    member_ids = [c["user_id"] for c in contribs]
    users = {}
    if member_ids:
        placeholders = ",".join("?"*len(member_ids))
        for r in fetchall(f"SELECT id, full_name FROM users WHERE id IN ({placeholders})", member_ids):
            users[r["id"]] = dict(r)
    return {"cycle": dict(cycle),
            "contributions": [{**dict(c),"user_name":users.get(c["user_id"],{}).get("full_name","?")} for c in contribs]}

def get_rosca_members(rosca_id):
    return fetchall("""SELECT rm.*, u.full_name, u.ncs_score, u.ncs_tier, u.hanatag
                       FROM rosca_members rm JOIN users u ON u.id=rm.user_id
                       WHERE rm.rosca_id=? ORDER BY rm.slot""", (rosca_id,))

# ── MEMBERSHIP MANAGEMENT ─────────────────────────────────────────────────────

def request_to_join(rosca_id, user_id):
    """Request to join — creates a pending membership for organiser approval."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r: raise ValueError("Circle not found")
    if r["status"] not in ("forming", "active"): raise ValueError("Circle not accepting members")
    user = fetchone("SELECT ncs_score FROM users WHERE id=?", (user_id,))
    if user["ncs_score"] < r["ncs_min_score"]:
        raise ValueError(f"Minimum NCS score of {r['ncs_min_score']} required")
    count = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE rosca_id=? AND status IN ('active','pending')", (rosca_id,))
    if count["c"] >= r["max_members"]: raise ValueError("This circle is full")
    existing = fetchone("SELECT id, status FROM rosca_members WHERE rosca_id=? AND user_id=?", (rosca_id, user_id))
    if existing:
        if existing["status"] == "pending": raise ValueError("Your request is already pending approval")
        raise ValueError("Already a member of this circle")
    with get_db() as db:
        db.execute("INSERT INTO rosca_members(id,rosca_id,user_id,slot,status) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), rosca_id, user_id, None, "pending"))
    push_notification(r["organiser_id"], "New join request",
                      f"Someone wants to join '{r['name']}'. Review in your organiser panel.",
                      "info", f"/organiser/{rosca_id}")

def get_pending_members(rosca_id):
    return fetchall("""SELECT rm.id as membership_id, rm.user_id, rm.joined_at,
                              u.full_name, u.ncs_score, u.ncs_tier, u.hanatag, u.country,
                              (SELECT COUNT(*) FROM contributions WHERE user_id=u.id AND status IN ('paid','late')) as contrib_count,
                              (SELECT COUNT(*) FROM contributions WHERE user_id=u.id AND status='missed') as missed_count,
                              (SELECT COUNT(*) FROM rosca_members WHERE user_id=u.id AND status='active') as active_circles
                       FROM rosca_members rm JOIN users u ON u.id=rm.user_id
                       WHERE rm.rosca_id=? AND rm.status='pending'
                       ORDER BY rm.joined_at""", (rosca_id,))

def approve_member(rosca_id, user_id, organiser_id):
    """Approve a pending join request."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or r["organiser_id"] != organiser_id: raise ValueError("Unauthorised")
    membership = fetchone("SELECT * FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='pending'", (rosca_id, user_id))
    if not membership: raise ValueError("No pending request found")
    active_count = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE rosca_id=? AND status='active'", (rosca_id,))
    slot = (active_count["c"] or 0) + 1
    with get_db() as db:
        db.execute("UPDATE rosca_members SET status='active', slot=? WHERE rosca_id=? AND user_id=?",
                   (slot, rosca_id, user_id))
    push_notification(user_id, "Join request approved! ✓",
                      f"You have been approved to join '{r['name']}'.",
                      "success", f"/circles/{rosca_id}")

def reject_member(rosca_id, user_id, organiser_id):
    """Reject a pending join request."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or r["organiser_id"] != organiser_id: raise ValueError("Unauthorised")
    with get_db() as db:
        db.execute("DELETE FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='pending'",
                   (rosca_id, user_id))
    push_notification(user_id, "Join request declined",
                      f"Your request to join '{r['name']}' was not approved.",
                      "warning")

def remove_member(rosca_id, user_id, organiser_id, reason=""):
    """Remove an active member. Applies NCS penalty and notifies them."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or r["organiser_id"] != organiser_id: raise ValueError("Unauthorised")
    if user_id == organiser_id: raise ValueError("Cannot remove the organiser")
    membership = fetchone("SELECT * FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='active'", (rosca_id, user_id))
    if not membership: raise ValueError("Member not found")
    with get_db() as db:
        db.execute("UPDATE rosca_members SET status='left' WHERE rosca_id=? AND user_id=?", (rosca_id, user_id))
    # NCS penalty for being removed
    ncs_engine.apply_event(user_id, "dispute_raised", ref_type="rosca", ref_id=rosca_id,
                           metadata={"removed_by": organiser_id, "reason": reason})
    push_notification(user_id, "Removed from circle",
                      f"You have been removed from '{r['name']}'" + (f": {reason}" if reason else "."),
                      "danger", "/circles")

def add_member_direct(rosca_id, user_id, organiser_id):
    """Organiser directly adds a member (bypasses approval, NCS reward for trust)."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or r["organiser_id"] != organiser_id: raise ValueError("Unauthorised")
    count = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE rosca_id=? AND status='active'", (rosca_id,))
    if count["c"] >= r["max_members"]: raise ValueError("Circle is full")
    existing = fetchone("SELECT id FROM rosca_members WHERE rosca_id=? AND user_id=?", (rosca_id, user_id))
    if existing: raise ValueError("Already a member or pending")
    slot = (count["c"] or 0) + 1
    with get_db() as db:
        db.execute("INSERT INTO rosca_members(id,rosca_id,user_id,slot,status) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), rosca_id, user_id, slot, "active"))
    push_notification(user_id, "Added to circle ✓",
                      f"You have been added to '{r['name']}' by the organiser.",
                      "success", f"/circles/{rosca_id}")

# ── CIRCLE REPORT ─────────────────────────────────────────────────────────────

def get_circle_report(rosca_id):
    """Generate a comprehensive report of how a circle performed."""
    r    = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r: return None

    # All cycles
    cycles = fetchall("SELECT * FROM cycles WHERE rosca_id=? ORDER BY cycle_number", (rosca_id,))

    # All contributions with member names
    all_contribs = fetchall("""
        SELECT c.*, u.full_name, u.ncs_score, u.ncs_tier, u.hanatag
        FROM contributions c JOIN users u ON u.id=c.user_id
        WHERE c.rosca_id=? ORDER BY c.created_at
    """, (rosca_id,))

    # Per-member stats
    members = fetchall("""
        SELECT rm.*, u.full_name, u.ncs_score, u.ncs_tier, u.hanatag, u.country
        FROM rosca_members rm JOIN users u ON u.id=rm.user_id
        WHERE rm.rosca_id=?
        ORDER BY rm.slot
    """, (rosca_id,))

    member_stats = []
    for m in members:
        uid = m["user_id"]
        mc  = [c for c in all_contribs if c["user_id"] == uid]
        paid   = [c for c in mc if c["status"] == "paid"]
        late   = [c for c in mc if c["status"] == "late"]
        missed = [c for c in mc if c["status"] == "missed"]
        total_paid_cents = sum(c["amount_cents"] for c in mc if c["status"] in ("paid","late"))
        on_time_rate     = round(len(paid) / max(len(mc), 1) * 100, 1)
        avg_late_days    = round(sum(c["late_days"] for c in late) / max(len(late), 1), 1) if late else 0
        received_payout  = fetchone("SELECT pot_cents FROM cycles WHERE rosca_id=? AND recipient_id=? AND status='completed'", (rosca_id, uid))
        member_stats.append({
            "user_id":       uid,
            "full_name":     m["full_name"],
            "ncs_score":     m["ncs_score"],
            "ncs_tier":      m["ncs_tier"],
            "hanatag":       m["hanatag"],
            "country":       m["country"],
            "slot":          m["slot"],
            "status":        m["status"],
            "total_contributions": len(mc),
            "paid_on_time":  len(paid),
            "paid_late":     len(late),
            "missed":        len(missed),
            "total_paid_cents": total_paid_cents,
            "on_time_rate":  on_time_rate,
            "avg_late_days": avg_late_days,
            "payout_received_cents": received_payout["pot_cents"] if received_payout else 0,
        })

    # Sort by on-time rate for ranking
    ranked = sorted(member_stats, key=lambda x: (-x["on_time_rate"], x["missed"], x["avg_late_days"]))
    for i, m in enumerate(ranked): m["rank"] = i + 1

    # Circle-level aggregates
    total_expected   = sum(c["amount_cents"] for c in all_contribs)
    total_collected  = sum(c["amount_cents"] for c in all_contribs if c["status"] in ("paid","late"))
    total_missed_amt = sum(c["amount_cents"] for c in all_contribs if c["status"] == "missed")
    cycles_complete  = len([cy for cy in cycles if cy["status"] == "completed"])
    collection_rate  = round(total_collected / max(total_expected, 1) * 100, 1)

    return {
        "rosca":           dict(r),
        "cycles":          [dict(cy) for cy in cycles],
        "member_stats":    ranked,
        "total_members":   len(members),
        "cycles_total":    len(cycles),
        "cycles_complete": cycles_complete,
        "total_expected_cents":   total_expected,
        "total_collected_cents":  total_collected,
        "total_missed_cents":     total_missed_amt,
        "collection_rate":        collection_rate,
        "platform_fees_cents":    int(total_collected * 0.0125),
    }
