import math, uuid, json
from database import get_db, fetchone, fetchall

NCS_MIN = 300; NCS_MAX = 850; NCS_RANGE = 550

WEIGHTS = {"reliability":0.35,"completion":0.25,"default_rec":0.20,"social":0.10,"wallet":0.10}

TIERS = [
    {"name":"probation", "label":"Probation", "min":300,"max":549,"color":"#D85A30","bg":"#FAECE7",
     "benefits":["Join small ROSCAs (up to 5 members)","Basic wallet access","Score history"]},
    {"name":"developing","label":"Developing","min":550,"max":649,"color":"#BA7517","bg":"#FAEEDA",
     "benefits":["Join most ROSCAs","Emergency liquidity loan","NCS coaching tips"]},
    {"name":"reliable",  "label":"Reliable",  "min":650,"max":749,"color":"#0F6E56","bg":"#E1F5EE",
     "benefits":["All ROSCAs","Early payout loan","Fee discount 0.5%","Priority support","Free ROSCA creation"]},
    {"name":"exemplary", "label":"Exemplary", "min":750,"max":850,"color":"#534AB7","bg":"#EEEDFE",
     "benefits":["Max loan amounts","Credit bureau reporting","Operator privileges","Beta access","Free ROSCA creation"]},
]

EVENT_DELTAS = {
    "contribution_on_time":+8,"cycle_completed":+12,"contribution_recovered":+5,
    "peer_endorsement":+3,"peer_endorsement_removed":-2,"dispute_resolved":+4,"wallet_deposit":+1,
    "contribution_late":-5,"contribution_missed":-18,"cycle_defaulted":-30,"dispute_raised":-8,
}

BADGE_DEFINITIONS = {
    "first_contribution":{"label":"First Step",    "icon":"🌱","desc":"Made your first contribution"},
    "streak_3":          {"label":"Hat Trick",     "icon":"🎯","desc":"3 on-time payments in a row"},
    "streak_5":          {"label":"On a Roll",     "icon":"🔥","desc":"5 on-time payments in a row"},
    "streak_10":         {"label":"Ironclad",      "icon":"⚡","desc":"10 on-time payments in a row"},
    "streak_25":         {"label":"Unstoppable",   "icon":"💎","desc":"25 consecutive on-time payments"},
    "cycle_1":           {"label":"Full Circle",   "icon":"⭕","desc":"Completed your first ROSCA cycle"},
    "cycle_5":           {"label":"Circle Elder",  "icon":"🏅","desc":"Completed 5 ROSCA cycles"},
    "cycle_10":          {"label":"Circle Master", "icon":"🏆","desc":"Completed 10 ROSCA cycles"},
    "endorser_3":        {"label":"Trusted Voice", "icon":"🤝","desc":"Gave 3+ peer endorsements"},
    "endorsed_5":        {"label":"Well Regarded", "icon":"⭐","desc":"Received 5+ endorsements"},
    "endorsed_20":       {"label":"Community Hero","icon":"🦸","desc":"Received 20+ endorsements"},
    "score_550":         {"label":"Developing",    "icon":"📈","desc":"Reached Developing tier"},
    "score_650":         {"label":"Reliable",      "icon":"💪","desc":"Reached Reliable tier"},
    "score_750":         {"label":"Exemplary",     "icon":"👑","desc":"Reached Exemplary tier"},
    "organiser":         {"label":"Circle Leader", "icon":"🎪","desc":"Organised your first circle"},
    "early_bird":        {"label":"Early Bird",    "icon":"🐦","desc":"Paid contribution 5+ days early"},
    "recovery":          {"label":"Comeback",      "icon":"🔄","desc":"Recovered from a missed payment"},
    "big_saver":         {"label":"Big Saver",     "icon":"💰","desc":"Rotated over €5,000 total"},
    "multi_circle":      {"label":"Circle Hopper", "icon":"🌐","desc":"Active in 3+ circles at once"},
}

def get_tier(score):
    for t in TIERS:
        if t["min"] <= score <= t["max"]: return t
    return TIERS[0]

def compute_components(user_id):
    contribs  = fetchall("SELECT status, late_days FROM contributions WHERE user_id=? ORDER BY created_at",(user_id,))
    membership= fetchall("""SELECT r.status AS rosca_status, rm.status AS mem_status
                            FROM rosca_members rm JOIN roscas r ON r.id=rm.rosca_id
                            WHERE rm.user_id=?""",(user_id,))
    endorsements = fetchone("SELECT COUNT(*) as cnt FROM endorsements WHERE to_id=?",(user_id,))
    wallet_txns  = fetchall("""SELECT amount_cents FROM wallet_transactions wt
                               JOIN wallets w ON w.id=wt.wallet_id
                               WHERE w.user_id=? AND wt.created_at>=datetime('now','-90 days')""",(user_id,))
    total_c = len(contribs)
    if total_c == 0: reliability = 0.5
    else:
        score = sum(1.0 if c["status"]=="paid" and c["late_days"]==0
                    else (1-math.sqrt(min(c["late_days"],30)/30)) if c["status"]=="paid"
                    else 0 for c in contribs)
        reliability = score / total_c
    total_r = len(membership)
    completion  = sum(1 for m in membership if m["rosca_status"]=="completed") / total_r if total_r else 0.5
    missed      = sum(1 for c in contribs if c["status"]=="missed")
    rosca_def   = sum(1 for m in membership if m["mem_status"]=="defaulted")
    default_rec = max(0.0, 1.0-(missed+rosca_def)*0.2) if (missed+rosca_def)>0 else 1.0
    ec = endorsements["cnt"] if endorsements else 0
    social      = min(1.0, math.log1p(ec)/math.log1p(20))
    if not wallet_txns: wallet = 0.5
    else:
        d = sum(1 for t in wallet_txns if t["amount_cents"]>0)
        w = sum(1 for t in wallet_txns if t["amount_cents"]<0)
        wallet = min(1.0, len(wallet_txns)/12)*0.5 + (d/(d+w) if d+w else 0.5)*0.5
    return {k:round(v,4) for k,v in [("reliability",reliability),("completion",completion),
             ("default_rec",default_rec),("social",social),("wallet",wallet)]}

def components_to_score(c):
    return int(round(max(NCS_MIN, min(NCS_MAX, NCS_MIN + sum(c[k]*WEIGHTS[k] for k in WEIGHTS)*NCS_RANGE))))

def recalculate(user_id):
    components = compute_components(user_id)
    new_score  = components_to_score(components)
    with get_db() as db:
        row = db.execute("SELECT ncs_score FROM users WHERE id=?",(user_id,)).fetchone()
        old_score = row["ncs_score"] if row else NCS_MIN
        delta = new_score - old_score
        db.execute("UPDATE users SET ncs_score=?,ncs_tier=?,updated_at=datetime('now') WHERE id=?",
                   (new_score, get_tier(new_score)["name"], user_id))
        db.execute("""INSERT INTO ncs_events(id,user_id,event_type,score_before,delta,score_after,ref_type,ref_id,metadata)
                      VALUES(?,?,'model_recalculation',?,?,?,'user',?,?)""",
                   (str(uuid.uuid4()), user_id, old_score, delta, new_score, user_id,
                    json.dumps({"components":components})))
    _check_badges(user_id, new_score, old_score)
    return new_score, components

def apply_event(user_id, event_type, ref_type=None, ref_id=None, metadata=None):
    if event_type not in EVENT_DELTAS: raise ValueError(f"Unknown event: {event_type}")
    with get_db() as db:
        row = db.execute("SELECT ncs_score FROM users WHERE id=?",(user_id,)).fetchone()
        score_before = row["ncs_score"] if row else NCS_MIN
        delta = EVENT_DELTAS[event_type]
        if event_type == "contribution_missed":
            count = db.execute("SELECT COUNT(*) as c FROM ncs_events WHERE user_id=? AND event_type='contribution_missed'",(user_id,)).fetchone()
            if count and count["c"] == 0: delta = delta // 2
        score_after = max(NCS_MIN, min(NCS_MAX, score_before + delta))
        db.execute("UPDATE users SET ncs_score=?,ncs_tier=?,updated_at=datetime('now') WHERE id=?",
                   (score_after, get_tier(score_after)["name"], user_id))
        db.execute("""INSERT INTO ncs_events(id,user_id,event_type,score_before,delta,score_after,ref_type,ref_id,metadata)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), user_id, event_type, score_before, delta, score_after,
                    ref_type, ref_id, json.dumps(metadata) if metadata else None))
    _check_badges(user_id, score_after, score_before)
    return score_before, score_after, delta

def _check_badges(user_id, new_score, old_score):
    earned = {r["badge_type"] for r in fetchall("SELECT badge_type FROM badges WHERE user_id=?",(user_id,))}
    to_award = []
    for score_thresh, badge in [(750,"score_750"),(650,"score_650"),(550,"score_550")]:
        if new_score >= score_thresh and badge not in earned: to_award.append(badge)
    contribs = fetchall("SELECT status FROM contributions WHERE user_id=? AND status='paid' ORDER BY created_at",(user_id,))
    if len(contribs) >= 1 and "first_contribution" not in earned: to_award.append("first_contribution")
    completed = fetchone("SELECT COUNT(*) as c FROM rosca_members rm JOIN roscas r ON r.id=rm.rosca_id WHERE rm.user_id=? AND r.status='completed'",(user_id,))
    if completed:
        for n, badge in [(1,"cycle_1"),(5,"cycle_5"),(10,"cycle_10")]:
            if completed["c"] >= n and badge not in earned: to_award.append(badge)
    consecutive = _count_consecutive_on_time(user_id)
    for n, badge in [(3,"streak_3"),(5,"streak_5"),(10,"streak_10"),(25,"streak_25")]:
        if consecutive >= n and badge not in earned: to_award.append(badge)
    ec = fetchone("SELECT COUNT(*) as c FROM endorsements WHERE to_id=?",(user_id,))
    if ec:
        for n, badge in [(5,"endorsed_5"),(20,"endorsed_20")]:
            if ec["c"] >= n and badge not in earned: to_award.append(badge)
    is_org = fetchone("SELECT COUNT(*) as c FROM roscas WHERE organiser_id=?",(user_id,))
    if is_org and is_org["c"] >= 1 and "organiser" not in earned: to_award.append("organiser")
    active_circles = fetchone("SELECT COUNT(*) as c FROM rosca_members WHERE user_id=? AND status='active'",(user_id,))
    if active_circles and active_circles["c"] >= 3 and "multi_circle" not in earned: to_award.append("multi_circle")
    total_rotated = fetchone("SELECT COALESCE(SUM(amount_cents),0) as s FROM contributions WHERE user_id=? AND status IN ('paid','late')",(user_id,))
    if total_rotated and total_rotated["s"] >= 500000 and "big_saver" not in earned: to_award.append("big_saver")
    with get_db() as db:
        for bt in to_award:
            if bt in BADGE_DEFINITIONS:
                try:
                    db.execute("INSERT OR IGNORE INTO badges(id,user_id,badge_type,label) VALUES(?,?,?,?)",
                               (str(uuid.uuid4()), user_id, bt, BADGE_DEFINITIONS[bt]["label"]))
                except Exception: pass

def _count_consecutive_on_time(user_id):
    rows = fetchall("SELECT status FROM contributions WHERE user_id=? ORDER BY created_at DESC LIMIT 30",(user_id,))
    count = 0
    for r in rows:
        if r["status"] == "paid": count += 1
        else: break
    return count

def get_score_history(user_id, days=180):
    rows = fetchall("SELECT DATE(recorded_at) as day, score_after FROM ncs_events WHERE user_id=? AND recorded_at>=datetime('now',?) ORDER BY recorded_at",(user_id,f"-{days} days"))
    seen, result = {}, []
    for r in rows: seen[r["day"]] = r["score_after"]
    for day, score in sorted(seen.items()): result.append({"day":day,"score":score})
    return result

def check_loan_eligibility(user_id, loan_type):
    thresholds = {"emergency":550,"early_payout":650,"rosca_backed":700}
    min_score = thresholds.get(loan_type, 999)
    row = fetchone("SELECT ncs_score, ncs_tier FROM users WHERE id=?",(user_id,))
    score = row["ncs_score"] if row else 300
    return {"eligible":score>=min_score,"score":score,"required":min_score,"gap":max(0,min_score-score)}

def get_component_breakdown(user_id):
    components = compute_components(user_id)
    return {k:{"value":v,"pct":round(v*100,1),"weight":WEIGHTS[k],"pts":round(v*WEIGHTS[k]*NCS_RANGE)} for k,v in components.items()}

def get_leaderboard(rosca_id):
    members = fetchall("""
        SELECT u.id, u.full_name, u.ncs_score, u.ncs_tier, rm.slot,
               COUNT(e.id) as endorsements,
               COUNT(CASE WHEN c.status IN ('paid','late') THEN 1 END) as paid_count,
               COUNT(CASE WHEN c.status='missed' THEN 1 END) as missed_count,
               COUNT(c.id) as total_contributions
        FROM rosca_members rm
        JOIN users u ON u.id=rm.user_id
        LEFT JOIN endorsements e ON e.to_id=u.id
        LEFT JOIN contributions c ON c.user_id=u.id AND c.rosca_id=rm.rosca_id
        WHERE rm.rosca_id=? AND rm.status='active'
        GROUP BY u.id ORDER BY u.ncs_score DESC
    """,(rosca_id,))
    result = []
    for i, m in enumerate(members):
        reliability = (m["paid_count"] / max(m["total_contributions"],1)) * 100
        trust_score = round((m["ncs_score"]/850*0.6 + m["endorsements"]/20*0.2 + reliability/100*0.2)*100)
        result.append({**dict(m), "rank": i+1, "reliability_pct": round(reliability,1), "trust_score": min(100,trust_score)})
    return result
