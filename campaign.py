"""
campaign.py — Fundraising & Donations Engine

Handles public fundraising campaigns: personal, community, emergency,
charity, and memorial. Donors can give from their SOHANA wallet or
anonymously. Creator withdraws raised funds (minus platform fee) to
their wallet at any time. Platform takes 2.5% of each donation.

NCS impact:
  - Donating: +2 NCS (charitable action)
  - Completing a campaign (reached goal): +8 NCS + badge
  - Sharing/creating a campaign: +1 NCS on first campaign
"""
import uuid, re
from datetime import datetime
from database import (get_db, fetchone, fetchall, post_transaction,
                      push_notification, wallet_balance)
import ncs_engine

# ── CATEGORY DEFINITIONS ──────────────────────────────────────────────────────
CAMPAIGN_CATEGORIES = {
    "personal":    {"label": "Personal",          "emoji": "🙏",  "desc": "Medical bills, school fees, personal hardship"},
    "emergency":   {"label": "Emergency",          "emoji": "🚨",  "desc": "Urgent crisis relief, disaster response"},
    "memorial":    {"label": "Memorial & Funeral", "emoji": "🕊",  "desc": "Funeral costs, bereavement support"},
    "community":   {"label": "Community Project",  "emoji": "🏗",  "desc": "Village development, infrastructure, renovation"},
    "education":   {"label": "Education",          "emoji": "🎓",  "desc": "School fees, scholarships, study support"},
    "charity":     {"label": "Charity & NGO",      "emoji": "💚",  "desc": "Registered charities and non-profits"},
    "business":    {"label": "Business Startup",   "emoji": "🚀",  "desc": "Launch or grow a small business"},
    "celebration": {"label": "Celebration",        "emoji": "🎉",  "desc": "Wedding gift, baby shower, milestone"},
}

PLATFORM_FEE_RATE = 0.025   # 2.5% on each donation
MIN_DONATION_CENTS = 100    # €1.00 minimum


# ── CREATE & MANAGE ───────────────────────────────────────────────────────────

def _make_slug(title, campaign_id):
    base = re.sub(r'[^a-z0-9]+', '-', title.lower().strip())[:50].strip('-')
    short = campaign_id[:6]
    return f"{base}-{short}"


def create_campaign(creator_id, title, story, category, goal_cents,
                    currency="EUR", deadline=None, is_public=True,
                    allow_anonymous=True):
    if not title.strip(): raise ValueError("Title is required")
    if not story.strip(): raise ValueError("Story is required")
    if goal_cents < 1000: raise ValueError("Goal must be at least €10")
    if category not in CAMPAIGN_CATEGORIES:
        category = "personal"

    cid   = str(uuid.uuid4())
    slug  = _make_slug(title, cid)
    emoji = CAMPAIGN_CATEGORIES[category]["emoji"]

    with get_db() as db:
        db.execute("""INSERT INTO campaigns(id,creator_id,title,slug,category,story,
                      cover_emoji,goal_cents,currency,is_public,allow_anonymous,deadline)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (cid, creator_id, title, slug, category, story, emoji,
                    goal_cents, currency, 1 if is_public else 0,
                    1 if allow_anonymous else 0, deadline))
    ncs_engine.apply_event(creator_id, "wallet_deposit",
                           ref_type="campaign_created", ref_id=cid)
    return cid, slug


def get_campaign(campaign_id=None, slug=None):
    if campaign_id:
        return fetchone("""SELECT c.*, u.full_name as creator_name, u.hanatag as creator_hanatag,
                                  u.ncs_score as creator_ncs, u.ncs_tier as creator_tier
                           FROM campaigns c JOIN users u ON u.id=c.creator_id
                           WHERE c.id=?""", (campaign_id,))
    if slug:
        return fetchone("""SELECT c.*, u.full_name as creator_name, u.hanatag as creator_hanatag,
                                  u.ncs_score as creator_ncs, u.ncs_tier as creator_tier
                           FROM campaigns c JOIN users u ON u.id=c.creator_id
                           WHERE c.slug=?""", (slug,))
    return None


def browse_campaigns(category=None, search=None, limit=20, offset=0):
    sql = """SELECT c.*, u.full_name as creator_name
             FROM campaigns c JOIN users u ON u.id=c.creator_id
             WHERE c.is_public=1 AND c.status='active'"""
    params = []
    if category and category in CAMPAIGN_CATEGORIES:
        sql += " AND c.category=?"
        params.append(category)
    if search:
        sql += " AND (c.title LIKE ? OR c.story LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY c.raised_cents DESC, c.created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return fetchall(sql, params)


def get_user_campaigns(user_id):
    return fetchall("""SELECT c.*, COUNT(cd.id) as donation_count
                       FROM campaigns c
                       LEFT JOIN campaign_donations cd ON cd.campaign_id=c.id
                       WHERE c.creator_id=?
                       GROUP BY c.id ORDER BY c.created_at DESC""", (user_id,))


def update_campaign(campaign_id, creator_id, **kwargs):
    c = fetchone("SELECT * FROM campaigns WHERE id=? AND creator_id=?", (campaign_id, creator_id))
    if not c: raise ValueError("Campaign not found or unauthorised")
    if c["status"] == "completed": raise ValueError("Cannot edit a completed campaign")

    allowed = {"title", "story", "deadline", "is_public", "allow_anonymous"}
    fields, vals = [], []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields: return
    vals.append(campaign_id)
    with get_db() as db:
        db.execute(f"UPDATE campaigns SET {','.join(fields)}, updated_at=datetime('now') WHERE id=?", vals)


def close_campaign(campaign_id, creator_id):
    c = fetchone("SELECT * FROM campaigns WHERE id=? AND creator_id=?", (campaign_id, creator_id))
    if not c: raise ValueError("Campaign not found or unauthorised")
    with get_db() as db:
        db.execute("UPDATE campaigns SET status='closed', updated_at=datetime('now') WHERE id=?", (campaign_id,))


# ── DONATIONS ────────────────────────────────────────────────────────────────

def donate(campaign_id, amount_cents, donor_id=None, message="", is_anonymous=False, donor_name_override=""):
    c = fetchone("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    if not c: raise ValueError("Campaign not found")
    if c["status"] not in ("active",): raise ValueError("This campaign is not currently accepting donations")
    if amount_cents < MIN_DONATION_CENTS:
        raise ValueError(f"Minimum donation is €{MIN_DONATION_CENTS/100:.2f}")

    # Platform fee
    fee = int(amount_cents * PLATFORM_FEE_RATE)
    net = amount_cents - fee

    # Resolve donor display name BEFORE opening any DB connection
    if is_anonymous or not donor_id:
        display_name = "Anonymous"
    elif donor_name_override:
        display_name = donor_name_override
    else:
        row = fetchone("SELECT full_name FROM users WHERE id=?", (donor_id,))
        display_name = row["full_name"] if row else "Anonymous"

    # Resolve donor wallet BEFORE opening the main transaction
    donor_wallet_id = None
    if donor_id:
        wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?",
                          (donor_id, c["currency"]))
        if not wallet:
            wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency='EUR'", (donor_id,))
        if not wallet: raise ValueError("No wallet found to donate from")
        donor_wallet_id = wallet["id"]

    did = str(uuid.uuid4())
    goal_reached = False
    creator_id   = c["creator_id"]

    # ── SINGLE ATOMIC TRANSACTION ─────────────────────────────────────────────
    # Everything inside one connection: wallet debit, donation record,
    # campaign balance update, goal check. If anything fails, all rolls back.
    # This prevents the "database locked" + partial-credit bug.
    with get_db() as db:
        # 1. Debit donor wallet
        if donor_wallet_id:
            post_transaction(donor_wallet_id, -amount_cents,
                             f"Donation to '{c['title']}'",
                             ref_type="campaign_donation", ref_id=did,
                             tx_type="pay_out", _db=db)

        # 2. Record the donation
        db.execute("""INSERT INTO campaign_donations(id,campaign_id,donor_id,donor_name,
                      amount_cents,message,is_anonymous,platform_fee)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (did, campaign_id, donor_id, display_name,
                    amount_cents, message,
                    1 if (is_anonymous or not donor_id) else 0, fee))

        # 3. Credit campaign balance and increment donor count
        db.execute("""UPDATE campaigns
                      SET raised_cents=raised_cents+?,
                          donor_count=donor_count+1,
                          updated_at=datetime('now')
                      WHERE id=?""", (net, campaign_id))

        # 4. Check if goal is now reached (read updated value inside same tx)
        updated = db.execute(
            "SELECT raised_cents, goal_cents FROM campaigns WHERE id=?",
            (campaign_id,)
        ).fetchone()
        if updated and updated["raised_cents"] >= updated["goal_cents"]:
            db.execute(
                "UPDATE campaigns SET status='completed', updated_at=datetime('now') WHERE id=?",
                (campaign_id,)
            )
            goal_reached = True
    # ── END ATOMIC TRANSACTION ────────────────────────────────────────────────

    # Post-commit side effects (notifications and NCS) run AFTER the
    # transaction closes, so they open fresh connections without conflict.
    if donor_id:
        try:
            ncs_engine.apply_event(donor_id, "peer_endorsement",
                                   ref_type="campaign_donation", ref_id=did,
                                   metadata={"campaign_id": campaign_id, "amount": amount_cents})
        except Exception:
            pass  # NCS failure must never roll back a completed donation

    if goal_reached:
        try:
            push_notification(creator_id,
                              "🎉 Goal reached!",
                              f"Your campaign '{c['title']}' has reached its goal!",
                              "success", f"/campaigns/{c['slug']}/manage")
            ncs_engine.apply_event(creator_id, "cycle_completed",
                                   ref_type="campaign_completed", ref_id=campaign_id)
        except Exception:
            pass

    # Always notify creator of the donation
    try:
        sym = "€"
        push_notification(creator_id,
                          f"New donation received! {sym}{net/100:.2f}",
                          f"{display_name} donated {sym}{amount_cents/100:.2f}" +
                          (f': "{message}"' if message else ""),
                          "success", f"/campaigns/{c['slug']}/manage")
    except Exception:
        pass

    return did, net, fee


def get_donations(campaign_id, limit=50):
    return fetchall("""SELECT * FROM campaign_donations
                       WHERE campaign_id=?
                       ORDER BY created_at DESC LIMIT ?""", (campaign_id, limit))


def get_top_donors(campaign_id, limit=5):
    return fetchall("""SELECT donor_name, SUM(amount_cents) as total, COUNT(*) as count,
                              is_anonymous, MAX(created_at) as last_at
                       FROM campaign_donations
                       WHERE campaign_id=? AND is_anonymous=0
                       GROUP BY donor_id, donor_name
                       ORDER BY total DESC LIMIT ?""", (campaign_id, limit))


# ── WITHDRAWALS ───────────────────────────────────────────────────────────────

def withdraw_funds(campaign_id, creator_id, amount_cents):
    """
    Creator withdraws raised funds to their SOHANA wallet.
    Can withdraw at any time — partial or full withdrawal.
    Platform fee has already been deducted from raised_cents at donation time.
    """
    c = fetchone("SELECT * FROM campaigns WHERE id=? AND creator_id=?", (campaign_id, creator_id))
    if not c: raise ValueError("Campaign not found or unauthorised")

    available = c["raised_cents"] - c["withdrawn_cents"]
    if amount_cents <= 0: raise ValueError("Amount must be positive")
    if amount_cents > available:
        raise ValueError(f"Only €{available/100:.2f} is available to withdraw")

    wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?",
                      (creator_id, c["currency"]))
    if not wallet:
        wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency='EUR'", (creator_id,))
    if not wallet: raise ValueError("No wallet found")

    ref = str(uuid.uuid4())
    post_transaction(wallet["id"], amount_cents,
                     f"Campaign withdrawal: {c['title']}",
                     ref_type="campaign_withdrawal", ref_id=ref,
                     tx_type="rosca_payout")

    with get_db() as db:
        db.execute("UPDATE campaigns SET withdrawn_cents=withdrawn_cents+?, updated_at=datetime('now') WHERE id=?",
                   (amount_cents, campaign_id))
    return ref


# ── ADMIN ─────────────────────────────────────────────────────────────────────

def get_all_campaigns(status=None, limit=50):
    sql = """SELECT c.*, u.full_name as creator_name
             FROM campaigns c JOIN users u ON u.id=c.creator_id"""
    params = []
    if status:
        sql += " WHERE c.status=?"
        params.append(status)
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    params.append(limit)
    return fetchall(sql, params)


def admin_flag_campaign(campaign_id, admin_id, reason):
    with get_db() as db:
        db.execute("UPDATE campaigns SET status='flagged', updated_at=datetime('now') WHERE id=?",
                   (campaign_id,))
    c = fetchone("SELECT creator_id, title FROM campaigns WHERE id=?", (campaign_id,))
    if c:
        push_notification(c["creator_id"],
                          "Campaign under review",
                          f"Your campaign '{c['title']}' has been flagged for review: {reason}",
                          "warning")


def admin_restore_campaign(campaign_id):
    with get_db() as db:
        db.execute("UPDATE campaigns SET status='active', updated_at=datetime('now') WHERE id=?",
                   (campaign_id,))


def get_campaign_stats():
    total  = fetchone("SELECT COUNT(*) as c FROM campaigns")["c"]
    active = fetchone("SELECT COUNT(*) as c FROM campaigns WHERE status='active'")["c"]
    raised = fetchone("SELECT COALESCE(SUM(raised_cents),0) as s FROM campaigns")["s"]
    fees   = fetchone("SELECT COALESCE(SUM(platform_fee),0) as s FROM campaign_donations")["s"]
    donors = fetchone("SELECT COUNT(*) as c FROM campaign_donations")["c"]
    return {"total": total, "active": active, "raised": raised, "fees": fees, "donors": donors}
