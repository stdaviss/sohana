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
