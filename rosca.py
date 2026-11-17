import uuid
from datetime import datetime, timedelta
from database import get_db, fetchone, fetchall, post_transaction, ROSCA_CREATION_FEES, push_notification, CURRENCIES

ZERO_DECIMAL = {"RWF", "XAF", "NGN"}
import ncs_engine

def create_rosca(organiser_id, name, description, contribution_cents,
                 max_members=12, frequency_days=30, ncs_min=300,
                 is_public=True, rosca_type="fixed_order", currency="EUR"):
    currency = (currency or "EUR").upper()
    if currency not in CURRENCIES:
        raise ValueError("Unsupported currency")
    if contribution_cents <= 0:
        raise ValueError("Enter a contribution amount")
    user = fetchone("SELECT ncs_tier FROM users WHERE id=?", (organiser_id,))
    tier = user["ncs_tier"] if user else "probation"
    creation_fee = ROSCA_CREATION_FEES.get(tier, 500)
    rid = str(uuid.uuid4())
    # Charge the creation fee AND create the circle in one write-locked
    # transaction. If the organiser can't cover the fee, nothing is created —
    # a zero balance no longer gets a free circle.
    with get_db(immediate=True) as db:
        if creation_fee > 0:
            w = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency='EUR'",
                           (organiser_id,)).fetchone()
            if not w:
                w = db.execute("SELECT id FROM wallets WHERE user_id=? AND is_default=1",
                               (organiser_id,)).fetchone()
            if not w:
                raise ValueError("No EUR wallet available to charge the creation fee.")
            balrow = db.execute("SELECT balance_after FROM wallet_transactions "
                                "WHERE wallet_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                (w["id"],)).fetchone()
            bal = balrow["balance_after"] if balrow else 0
            if bal < creation_fee:
                raise ValueError(f"You need €{creation_fee/100:.2f} to create a circle. "
                                 f"Please top up your EUR wallet first.")
            post_transaction(w["id"], -creation_fee, f"Circle creation fee: {name}",
                             tx_type="fee", _db=db)
        db.execute("""INSERT INTO roscas(id,name,description,organiser_id,rosca_type,
                      contribution_cents,currency,frequency_days,max_members,ncs_min_score,
                      is_public,total_cycles,creation_fee_cents)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, name, description, organiser_id, rosca_type,
                    contribution_cents, currency, frequency_days, max_members,
                    ncs_min, 1 if is_public else 0, max_members, creation_fee))
        db.execute("INSERT INTO rosca_members(id,rosca_id,user_id,slot,status) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), rid, organiser_id, 1, "active"))
    # (Creating a circle no longer awards an "on-time contribution" NCS event:
    # nothing was contributed, and it let the score be farmed.)
    return rid, creation_fee

def get_rosca(rosca_id):
    return fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))

def get_marketplace(limit=20, search=None):
    # NB: roscas has its own (stale) member_count column; r.* would shadow an alias of the
    # same name, so the live count is selected as active_count and copied over below.
    sql = """SELECT r.*, u.full_name as organiser_name, COUNT(rm.id) as active_count
             FROM roscas r JOIN users u ON u.id=r.organiser_id
             LEFT JOIN rosca_members rm ON rm.rosca_id=r.id AND rm.status='active'
             WHERE r.is_public=1 AND r.status IN ('forming','active')"""
    params = []
    if search:
        sql += " AND (r.name LIKE ? OR r.description LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?"
    params.append(limit)
    return [_with_count(row) for row in fetchall(sql, params)]


def _with_count(row):
    d = dict(row)
    d["member_count"] = d.pop("active_count", 0) or 0
    return d

def get_user_roscas(user_id):
    return [_with_count(x) for x in fetchall("""SELECT r.*, rm.status as mem_status, rm.slot,
                              COUNT(rm2.id) as active_count
                       FROM rosca_members rm
                       JOIN roscas r ON r.id=rm.rosca_id
                       LEFT JOIN rosca_members rm2 ON rm2.rosca_id=r.id AND rm2.status='active'
                       WHERE rm.user_id=? AND rm.status IN ('active','pending')
                       GROUP BY r.id ORDER BY r.created_at DESC""", (user_id,))]

def join_rosca(rosca_id, user_id):
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r: raise ValueError("Circle not found")
    if r["status"] not in ("forming","active","completed"): raise ValueError("This circle is not accepting members")
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
    sync_open_cycle(rosca_id)
    organiser_id = r["organiser_id"]
    push_notification(organiser_id, "New member joined", f"Someone joined your circle '{r['name']}'.", "info", f"/organiser/{rosca_id}")

ROSCA_PAYOUT_FEE = 0.0125   # 1.25% platform fee on each payout
MAX_CO_ADMINS = 2


def is_circle_admin(rosca_id, user_id):
    """Organiser, or an active member the organiser made co-admin."""
    if not user_id:
        return False
    r = fetchone("SELECT organiser_id FROM roscas WHERE id=?", (rosca_id,))
    if not r:
        return False
    if r["organiser_id"] == user_id:
        return True
    try:
        return fetchone("""SELECT 1 FROM rosca_members WHERE rosca_id=? AND user_id=?
                           AND status='active' AND role='co_admin'""", (rosca_id, user_id)) is not None
    except Exception:
        return False


def set_co_admin(rosca_id, organiser_id, user_id, on=True):
    """Only the organiser shares (or withdraws) admin rights, up to MAX_CO_ADMINS."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or r["organiser_id"] != organiser_id:
        raise ValueError("Only the organiser can share admin rights")
    if user_id == organiser_id:
        raise ValueError("You are already the organiser")
    m = fetchone("SELECT role FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='active'",
                 (rosca_id, user_id))
    if not m:
        raise ValueError("Only active members can be co-admins")
    if on:
        n = fetchone("SELECT COUNT(*) AS c FROM rosca_members WHERE rosca_id=? AND role='co_admin' AND status='active'",
                     (rosca_id,))["c"]
        if m["role"] != "co_admin" and n >= MAX_CO_ADMINS:
            raise ValueError(f"A circle can have at most {MAX_CO_ADMINS} co-admins")
    with get_db() as db:
        db.execute("UPDATE rosca_members SET role=? WHERE rosca_id=? AND user_id=?",
                   ("co_admin" if on else "member", rosca_id, user_id))
    if on:
        push_notification(user_id, "You are now a co-admin",
                          f"You can now manage '{r['name']}': add members, start rounds and post announcements.",
                          "success", f"/circles/{rosca_id}")


def _round_start(r):
    try:
        return int(r["round_start_cycle"] or 1)
    except (IndexError, KeyError):
        return 1


def _round_no(r):
    try:
        return int(r["current_round"] or 1)
    except (IndexError, KeyError):
        return 1


def cycle_label(r, cycle_number):
    """'cycle 2' in the first round, 'round 2 · cycle 1' afterwards."""
    n = int(cycle_number) - _round_start(r) + 1
    return f"round {_round_no(r)} · cycle {n}" if _round_no(r) > 1 else f"cycle {n}"


def start_new_round(rosca_id, user_id):
    """After every member has been paid out, an admin starts the next round
    with whoever is an active member now (new joiners included, leavers gone).
    The circle keeps its history, its name and its NCS trail."""
    if not is_circle_admin(rosca_id, user_id):
        raise ValueError("Only the organiser or a co-admin can start a new round")
    with get_db(immediate=True) as db:
        r = db.execute("SELECT * FROM roscas WHERE id=?", (rosca_id,)).fetchone()
        if not r:
            raise ValueError("Circle not found")
        if r["status"] == "active":
            raise ValueError("This round is still running")
        if r["status"] not in ("completed", "forming"):
            raise ValueError("This circle can't be restarted")
        n = db.execute("SELECT COUNT(*) AS c FROM rosca_members WHERE rosca_id=? AND status='active'",
                       (rosca_id,)).fetchone()["c"]
        if n < 1:
            raise ValueError("Add members before starting a new round")
        last = db.execute("SELECT MAX(cycle_number) AS n FROM cycles WHERE rosca_id=?", (rosca_id,)).fetchone()["n"] or 0
        new_round = _round_no(r) + (1 if last else 0)
        db.execute("""UPDATE roscas SET status='active', current_round=?, round_start_cycle=?,
                      updated_at=datetime('now') WHERE id=?""", (new_round, last + 1, rosca_id))
    cycle = get_or_create_active_cycle(rosca_id)
    r = fetchone("SELECT name FROM roscas WHERE id=?", (rosca_id,))
    for m in fetchall("SELECT user_id FROM rosca_members WHERE rosca_id=? AND status='active'", (rosca_id,)):
        if m["user_id"] != user_id:
            push_notification(m["user_id"], f"Round {new_round} has started",
                              f"'{r['name']}' is running again. Your first contribution is due.",
                              "info", f"/circles/{rosca_id}")
    return new_round, cycle


def _money(cents, currency):
    sym = CURRENCIES.get(currency, {}).get("symbol", currency)
    if len(sym) > 1 and not sym.endswith(" "):
        sym += " "
    if currency in ZERO_DECIMAL:
        return f"{sym}{round(cents/100):,}".replace(",", " ")
    return f"{sym}{cents/100:,.2f}".replace(",", " ")


def _active_members(db, rosca_id):
    return db.execute("""SELECT user_id FROM rosca_members
                         WHERE rosca_id=? AND status='active'
                         ORDER BY COALESCE(slot, payout_position, 9999), joined_at""",
                      (rosca_id,)).fetchall()


def _sync_contributions(db, cycle, r):
    """Every active member owes the open cycle (late joiners included);
    pending rows of people who are no longer members are dropped so they
    can't block the payout. Must run inside a write transaction."""
    if not cycle or cycle["status"] not in ("open", "collecting"):
        return
    active = {m["user_id"] for m in _active_members(db, r["id"])}
    have = {row["user_id"]: row["status"] for row in db.execute(
        "SELECT user_id, status FROM contributions WHERE cycle_id=?", (cycle["id"],))}
    for uid in active - set(have):
        db.execute("""INSERT OR IGNORE INTO contributions
                      (id,cycle_id,rosca_id,user_id,amount_cents,status,due_at)
                      VALUES(?,?,?,?,?,'pending',?)""",
                   (str(uuid.uuid4()), cycle["id"], r["id"], uid,
                    r["contribution_cents"], cycle["due_at"]))
    for uid, st in have.items():
        if uid not in active and st == "pending":
            db.execute("DELETE FROM contributions WHERE cycle_id=? AND user_id=? AND status='pending'",
                       (cycle["id"], uid))


def _open_next_cycle(db, r):
    """Open the next cycle for the first active member who hasn't been paid out
    yet. Returns the new cycle row, or None when everyone has received."""
    members = _active_members(db, r["id"])
    if not members:
        raise ValueError("This circle has no active members yet.")
    paid_out = {row["recipient_id"] for row in db.execute(
        "SELECT recipient_id FROM cycles WHERE rosca_id=? AND status='completed' AND cycle_number>=?",
        (r["id"], _round_start(r)))}
    nxt = next((m["user_id"] for m in members if m["user_id"] not in paid_out), None)
    if nxt is None:
        return None
    last = db.execute("SELECT MAX(cycle_number) AS n FROM cycles WHERE rosca_id=?",
                      (r["id"],)).fetchone()["n"] or 0
    num = last + 1
    due = (datetime.utcnow() + timedelta(days=r["frequency_days"] or 30)).isoformat()
    cid = str(uuid.uuid4())
    db.execute("""INSERT INTO cycles(id,rosca_id,cycle_number,recipient_id,pot_cents,status,due_at,round_number)
                  VALUES(?,?,?,?,0,'collecting',?,?)""", (cid, r["id"], num, nxt, due, _round_no(r)))
    db.execute("UPDATE roscas SET current_cycle=?, total_cycles=?, updated_at=datetime('now') WHERE id=?",
               (num, len(paid_out) + len([m for m in members if m["user_id"] not in paid_out]), r["id"]))
    cycle = db.execute("SELECT * FROM cycles WHERE id=?", (cid,)).fetchone()
    _sync_contributions(db, cycle, r)
    return cycle


def get_or_create_active_cycle(rosca_id):
    """Return the open cycle, opening one if needed. Only for active circles."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r:
        raise ValueError("Circle not found")
    if r["status"] == "completed":
        raise ValueError("This round is complete. The organiser can start a new round.")
    if r["status"] != "active":
        raise ValueError("Payments open once the organiser starts the circle.")
    with get_db(immediate=True) as db:
        cycle = db.execute("""SELECT * FROM cycles WHERE rosca_id=? AND status IN ('open','collecting')
                              ORDER BY cycle_number DESC LIMIT 1""", (rosca_id,)).fetchone()
        if cycle is None:
            cycle = _open_next_cycle(db, r)
            if cycle is None:
                db.execute("UPDATE roscas SET status='completed', updated_at=datetime('now') WHERE id=?", (rosca_id,))
                raise ValueError("This round is complete. The organiser can start a new round.")
        else:
            _sync_contributions(db, cycle, r)
        cid = cycle["id"]
    return dict(fetchone("SELECT * FROM cycles WHERE id=?", (cid,)))


def sync_open_cycle(rosca_id):
    """Make sure every active member has a row in the open cycle (cheap no-op
    when nothing is missing). Call after anyone joins or is added."""
    cycle = fetchone("""SELECT * FROM cycles WHERE rosca_id=? AND status IN ('open','collecting')
                        ORDER BY cycle_number DESC LIMIT 1""", (rosca_id,))
    if not cycle:
        return
    missing = fetchone("""SELECT COUNT(*) AS c FROM rosca_members rm
                          WHERE rm.rosca_id=? AND rm.status='active'
                            AND NOT EXISTS (SELECT 1 FROM contributions c
                                            WHERE c.cycle_id=? AND c.user_id=rm.user_id)""",
                       (rosca_id, cycle["id"]))["c"]
    stale = fetchone("""SELECT COUNT(*) AS c FROM contributions c
                        WHERE c.cycle_id=? AND c.status='pending'
                          AND NOT EXISTS (SELECT 1 FROM rosca_members rm WHERE rm.rosca_id=?
                                          AND rm.user_id=c.user_id AND rm.status='active')""",
                     (cycle["id"], rosca_id))["c"]
    if not missing and not stale:
        return
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    with get_db(immediate=True) as db:
        cyc = db.execute("SELECT * FROM cycles WHERE id=?", (cycle["id"],)).fetchone()
        _sync_contributions(db, cyc, r)
    _check_cycle_complete(cycle["id"])


def pay_contribution(user_id, cycle_id):
    """Debit the member's wallet in the circle's currency and record the
    contribution, all in one write-locked transaction (no double payment,
    no partial state). Returns a summary dict."""
    with get_db(immediate=True) as db:
        contrib = db.execute("SELECT * FROM contributions WHERE cycle_id=? AND user_id=?",
                             (cycle_id, user_id)).fetchone()
        if contrib and contrib["status"] in ("paid", "late"):
            raise ValueError("You've already paid this cycle.")
        if not contrib or contrib["status"] != "pending":
            raise ValueError("No contribution is due from you in this cycle.")
        r = db.execute("SELECT * FROM roscas WHERE id=?", (contrib["rosca_id"],)).fetchone()
        cycle = db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if not cycle or cycle["status"] not in ("open", "collecting"):
            raise ValueError("This cycle is closed.")
        cur = (r["currency"] or "EUR") if r else "EUR"
        amt = contrib["amount_cents"]
        w = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency=?", (user_id, cur)).fetchone()
        if not w:
            raise ValueError(f"This circle collects in {cur}. Open a {cur} balance in Wallet, "
                             f"then add or convert {_money(amt, cur)}.")
        balrow = db.execute("SELECT balance_after FROM wallet_transactions WHERE wallet_id=? "
                            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (w["id"],)).fetchone()
        bal = balrow["balance_after"] if balrow else 0
        if bal < amt:
            raise ValueError(f"Not enough in your {cur} balance: you have {_money(bal, cur)}, "
                             f"the contribution is {_money(amt, cur)}. Add or convert funds first.")
        post_transaction(w["id"], -amt, f"Contribution: {r['name']} ({cycle_label(r, cycle['cycle_number'])})",
                         "contribution", contrib["id"], tx_type="rosca_contribution", _db=db)
        now = datetime.utcnow()
        try:
            late_days = max(0, (now.date() - datetime.fromisoformat(str(contrib["due_at"])[:10]).date()).days)
        except Exception:
            late_days = 0
        status = "late" if late_days > 0 else "paid"
        db.execute("UPDATE contributions SET status=?, paid_at=?, late_days=? WHERE id=? AND status='pending'",
                   (status, now.isoformat(), late_days, contrib["id"]))
        # pot grows only from money actually received
        db.execute("UPDATE cycles SET pot_cents=pot_cents+? WHERE id=?", (amt, cycle_id))
        rosca_id = r["id"]
    ncs_engine.apply_event(user_id, "contribution_on_time" if status == "paid" else "contribution_late",
                           ref_type="contribution", ref_id=contrib["id"])
    payout = _check_cycle_complete(cycle_id)
    return {"status": status, "amount_cents": amt, "currency": cur,
            "rosca_id": rosca_id, "payout": payout}


def _check_cycle_complete(cycle_id):
    """When nobody in the cycle still owes, pay the recipient (in the circle's
    currency, minus the platform fee), close the cycle and open the next one.
    Exactly once, inside one write-locked transaction. Returns payout info or None."""
    with get_db(immediate=True) as db:
        cycle = db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if not cycle or cycle["status"] == "completed":
            return None
        r = db.execute("SELECT * FROM roscas WHERE id=?", (cycle["rosca_id"],)).fetchone()
        _sync_contributions(db, cycle, r)
        pending = db.execute("SELECT COUNT(*) AS c FROM contributions WHERE cycle_id=? AND status='pending'",
                             (cycle_id,)).fetchone()["c"]
        if pending:
            return None
        pot = db.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM contributions "
                         "WHERE cycle_id=? AND status IN ('paid','late')", (cycle_id,)).fetchone()["s"]
        if pot <= 0:
            return None
        cur = r["currency"] or "EUR"
        fee = int(round(pot * ROSCA_PAYOUT_FEE))
        net = pot - fee
        rw = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency=?",
                        (cycle["recipient_id"], cur)).fetchone()
        if not rw:
            db.execute("INSERT INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,0)",
                       (str(uuid.uuid4()), cycle["recipient_id"], cur))
            rw = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency=?",
                            (cycle["recipient_id"], cur)).fetchone()
        post_transaction(rw["id"], net,
                         f"Payout: {r['name']} ({cycle_label(r, cycle['cycle_number'])}, fee {_money(fee, cur)})",
                         "cycle", cycle_id, tx_type="rosca_payout", _db=db)
        db.execute("UPDATE cycles SET status='completed', pot_cents=?, completed_at=datetime('now') WHERE id=?",
                   (pot, cycle_id))
        contributors = [row["user_id"] for row in db.execute(
            "SELECT user_id FROM contributions WHERE cycle_id=? AND status IN ('paid','late')", (cycle_id,))]
        nxt = _open_next_cycle(db, r) if r["status"] == "active" else None
        if nxt is None:
            db.execute("UPDATE roscas SET status='completed', updated_at=datetime('now') WHERE id=?", (r["id"],))
        info = {"recipient_id": cycle["recipient_id"], "net_cents": net, "fee_cents": fee,
                "currency": cur, "cycle_number": cycle["cycle_number"] - _round_start(r) + 1,
                "round": _round_no(r), "rosca_id": r["id"],
                "rosca_name": r["name"], "next_cycle": nxt["cycle_number"] if nxt else None,
                "round_complete": nxt is None}
    push_notification(info["recipient_id"], "Your circle payout arrived",
                      f"{_money(net, cur)} from {info['rosca_name']} is in your {cur} balance.",
                      "success", "/wallet")
    for uid in contributors:
        ncs_engine.apply_event(uid, "cycle_completed", ref_type="cycle", ref_id=cycle_id)
    return info

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
    if r["status"] not in ("forming", "active", "completed"): raise ValueError("Circle not accepting members")
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
    if not r or not is_circle_admin(rosca_id, organiser_id): raise ValueError("Unauthorised")
    membership = fetchone("SELECT * FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='pending'", (rosca_id, user_id))
    if not membership: raise ValueError("No pending request found")
    active_count = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE rosca_id=? AND status='active'", (rosca_id,))
    slot = (active_count["c"] or 0) + 1
    with get_db() as db:
        db.execute("UPDATE rosca_members SET status='active', slot=? WHERE rosca_id=? AND user_id=?",
                   (slot, rosca_id, user_id))
    sync_open_cycle(rosca_id)
    push_notification(user_id, "Join request approved! ✓",
                      f"You have been approved to join '{r['name']}'.",
                      "success", f"/circles/{rosca_id}")

def reject_member(rosca_id, user_id, organiser_id):
    """Reject a pending join request."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or not is_circle_admin(rosca_id, organiser_id): raise ValueError("Unauthorised")
    with get_db() as db:
        db.execute("DELETE FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='pending'",
                   (rosca_id, user_id))
    push_notification(user_id, "Join request declined",
                      f"Your request to join '{r['name']}' was not approved.",
                      "warning")

def remove_member(rosca_id, user_id, organiser_id, reason=""):
    """Remove an active member. Applies NCS penalty and notifies them."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or not is_circle_admin(rosca_id, organiser_id): raise ValueError("Unauthorised")
    if user_id == r["organiser_id"]: raise ValueError("Cannot remove the organiser")
    if user_id == organiser_id: raise ValueError("You can't remove yourself")
    if organiser_id != r["organiser_id"] and is_circle_admin(rosca_id, user_id):
        raise ValueError("Only the organiser can remove a co-admin")
    membership = fetchone("SELECT * FROM rosca_members WHERE rosca_id=? AND user_id=? AND status='active'", (rosca_id, user_id))
    if not membership: raise ValueError("Member not found")
    with get_db() as db:
        db.execute("UPDATE rosca_members SET status='left' WHERE rosca_id=? AND user_id=?", (rosca_id, user_id))
    sync_open_cycle(rosca_id)   # drop their unpaid row so the payout isn't blocked
    # NCS penalty for being removed
    ncs_engine.apply_event(user_id, "dispute_raised", ref_type="rosca", ref_id=rosca_id,
                           metadata={"removed_by": organiser_id, "reason": reason})
    push_notification(user_id, "Removed from circle",
                      f"You have been removed from '{r['name']}'" + (f": {reason}" if reason else "."),
                      "danger", "/circles")

def add_member_direct(rosca_id, user_id, organiser_id):
    """Organiser directly adds a member (bypasses approval, NCS reward for trust)."""
    r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
    if not r or not is_circle_admin(rosca_id, organiser_id): raise ValueError("Unauthorised")
    count = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE rosca_id=? AND status='active'", (rosca_id,))
    if count["c"] >= r["max_members"]: raise ValueError("Circle is full")
    existing = fetchone("SELECT id FROM rosca_members WHERE rosca_id=? AND user_id=?", (rosca_id, user_id))
    if existing: raise ValueError("Already a member or pending")
    slot = (count["c"] or 0) + 1
    with get_db() as db:
        db.execute("INSERT INTO rosca_members(id,rosca_id,user_id,slot,status) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), rosca_id, user_id, slot, "active"))
    sync_open_cycle(rosca_id)
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
