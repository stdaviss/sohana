import sqlite3, os, uuid, json
from contextlib import contextmanager

DB_PATH = os.environ.get("DATABASE_PATH", "sohana.db")

# Supported currencies with display info
CURRENCIES = {
    "EUR": {"symbol": "€",  "name": "Euro",              "flag": "🇪🇺"},
    "GBP": {"symbol": "£",  "name": "British Pound",     "flag": "🇬🇧"},
    "USD": {"symbol": "$",  "name": "US Dollar",         "flag": "🇺🇸"},
    "CAD": {"symbol": "C$", "name": "Canadian Dollar",   "flag": "🇨🇦"},
    "XAF": {"symbol": "Fr", "name": "CFA Franc",         "flag": "🌍"},
    "GHC": {"symbol": "₵",  "name": "Ghanaian Cedi",    "flag": "🇬🇭"},
    "NGN": {"symbol": "₦",  "name": "Nigerian Naira",   "flag": "🇳🇬"},
    "ZAR": {"symbol": "R",  "name": "South African Rand","flag": "🇿🇦"},
}

# Demo exchange rates vs EUR (these update from an API in production)
EXCHANGE_RATES = {
    "EUR": 1.0000, "GBP": 0.8560, "USD": 1.0820, "CAD": 1.4710,
    "XAF": 655.96, "GHC": 16.42,  "NGN": 1780.50, "ZAR": 20.15,
}

CONVERSION_FEE_RATE = 0.007  # 0.7%

# Withdrawal fees by method
WITHDRAWAL_FEES = {
    "bank_eu": 0.010, "bank_uk": 0.015, "bank_us": 0.020,
    "bank_swift": 0.035, "mobile_money": 0.015, "sohana_user": 0.000,
}
WITHDRAWAL_FEE_MIN = 50  # minimum 50 cents

# ROSCA creation fees by tier
ROSCA_CREATION_FEES = {
    "probation": 500, "developing": 300, "reliable": 100, "exemplary": 0,
}

# ── Account transaction limits (Standard tier) ────────────────────────────────
LIMITS = {
    "standard": {
        "deposit_daily_cents":    1_000_000,   # €10,000
        "withdraw_daily_cents":     300_000,   # €3,000
        "withdraw_monthly_cents": 1_000_000,   # €10,000
        "pay_fee_threshold_cents":  500_000,   # €5,000 — Pay fee above this
        "pay_fee_rate":               0.020,   # 2% on Pay amounts > €5,000
    }
}

def fmt(cents, currency="EUR"):
    """Format cents as x xxx xxx.xx with thousand-space separators."""
    sym = CURRENCIES.get(currency, {}).get("symbol", "")
    value = abs(cents) / 100
    formatted = f"{value:,.2f}".replace(",", " ")  # non-breaking space
    return f"{sym}{formatted}"

def get_period_total(wallet_id, tx_type, direction, period="day"):
    """Sum transactions of a given type/direction within today or this month."""
    since = "datetime('now', 'start of day')" if period == "day"             else "datetime('now', 'start of month')"
    clause = "amount_cents < 0" if direction == "out" else "amount_cents > 0"
    row = fetchone(
        f"""SELECT COALESCE(SUM(ABS(amount_cents)), 0) AS total
            FROM wallet_transactions
            WHERE wallet_id=? AND tx_type=? AND {clause}
              AND created_at >= {since}""",
        (wallet_id, tx_type)
    )
    return row["total"] if row else 0

# Admin role definitions
ADMIN_ROLES = {
    "ceo":        {"label": "CEO",                 "dashboard": "executive",   "color": "#7c3aed", "icon": "👑"},
    "cto":        {"label": "CTO",                 "dashboard": "engineering", "color": "#0891b2", "icon": "⚙️"},
    "cco":        {"label": "CCO",                 "dashboard": "compliance",  "color": "#16a34a", "icon": "🛡️"},
    "cfo":        {"label": "CFO",                 "dashboard": "executive",   "color": "#0369a1", "icon": "💼"},
    "operations": {"label": "Operations Officer",  "dashboard": "operations",  "color": "#2563eb", "icon": "📊"},
    "compliance": {"label": "Compliance Manager",  "dashboard": "compliance",  "color": "#16a34a", "icon": "📋"},
    "fraud":      {"label": "Fraud Analyst",       "dashboard": "fraud",       "color": "#dc2626", "icon": "🔍"},
    "credit":     {"label": "Credit Officer",      "dashboard": "credit",      "color": "#d97706", "icon": "💳"},
    "business":   {"label": "Business Manager",    "dashboard": "business",    "color": "#7c3aed", "icon": "💼"},
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    phone         TEXT UNIQUE NOT NULL,
    email         TEXT UNIQUE,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    country       TEXT NOT NULL DEFAULT 'RW',
    hanatag       TEXT UNIQUE,
    bio           TEXT,
    language      TEXT NOT NULL DEFAULT 'en',
    base_currency TEXT NOT NULL DEFAULT 'EUR',
    ncs_score     INTEGER NOT NULL DEFAULT 300,
    ncs_tier      TEXT NOT NULL DEFAULT 'probation',
    kyc_level     TEXT NOT NULL DEFAULT 'phone',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    admin_role    TEXT,
    notif_email   INTEGER NOT NULL DEFAULT 1,
    notif_push    INTEGER NOT NULL DEFAULT 1,
    notif_sms          INTEGER NOT NULL DEFAULT 0,
    freeze_deposits    INTEGER NOT NULL DEFAULT 0,
    freeze_withdrawals INTEGER NOT NULL DEFAULT 0,
    freeze_reason      TEXT,
    frozen_by          TEXT,
    frozen_at          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wallets (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    currency   TEXT NOT NULL DEFAULT 'EUR',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, currency)
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id             TEXT PRIMARY KEY,
    wallet_id      TEXT NOT NULL REFERENCES wallets(id),
    amount_cents   INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    description    TEXT NOT NULL,
    ref_type       TEXT,
    ref_id         TEXT,
    tx_type        TEXT NOT NULL DEFAULT 'other',
    currency       TEXT NOT NULL DEFAULT 'EUR',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS currency_conversions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    from_wallet_id  TEXT NOT NULL REFERENCES wallets(id),
    to_wallet_id    TEXT NOT NULL REFERENCES wallets(id),
    from_currency   TEXT NOT NULL,
    to_currency     TEXT NOT NULL,
    from_amount     INTEGER NOT NULL,
    to_amount       INTEGER NOT NULL,
    rate            REAL NOT NULL,
    fee_cents       INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    method_type TEXT NOT NULL,
    label       TEXT NOT NULL,
    details     TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS roscas (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    description        TEXT,
    organiser_id       TEXT NOT NULL REFERENCES users(id),
    rosca_type         TEXT NOT NULL DEFAULT 'fixed_order',
    status             TEXT NOT NULL DEFAULT 'forming',
    contribution_cents INTEGER NOT NULL,
    currency           TEXT NOT NULL DEFAULT 'EUR',
    frequency_days     INTEGER NOT NULL DEFAULT 30,
    max_members        INTEGER NOT NULL DEFAULT 12,
    ncs_min_score      INTEGER NOT NULL DEFAULT 300,
    is_public          INTEGER NOT NULL DEFAULT 1,
    current_cycle      INTEGER NOT NULL DEFAULT 0,
    total_cycles       INTEGER NOT NULL DEFAULT 0,
    creation_fee_cents INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rosca_members (
    id        TEXT PRIMARY KEY,
    rosca_id  TEXT NOT NULL REFERENCES roscas(id),
    user_id   TEXT NOT NULL REFERENCES users(id),
    slot      INTEGER,
    status    TEXT NOT NULL DEFAULT 'active',
    joined_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(rosca_id, user_id)
);

CREATE TABLE IF NOT EXISTS cycles (
    id           TEXT PRIMARY KEY,
    rosca_id     TEXT NOT NULL REFERENCES roscas(id),
    cycle_number INTEGER NOT NULL,
    recipient_id TEXT REFERENCES users(id),
    pot_cents    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open',
    due_at       TEXT NOT NULL,
    completed_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(rosca_id, cycle_number)
);

CREATE TABLE IF NOT EXISTS contributions (
    id             TEXT PRIMARY KEY,
    cycle_id       TEXT NOT NULL REFERENCES cycles(id),
    rosca_id       TEXT NOT NULL,
    user_id        TEXT NOT NULL REFERENCES users(id),
    amount_cents   INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    due_at         TEXT NOT NULL,
    paid_at        TEXT,
    late_days      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(cycle_id, user_id)
);

CREATE TABLE IF NOT EXISTS ncs_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    event_type  TEXT NOT NULL,
    score_before INTEGER NOT NULL,
    delta       INTEGER NOT NULL,
    score_after INTEGER NOT NULL,
    ref_type    TEXT,
    ref_id      TEXT,
    metadata    TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS badges (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    badge_type  TEXT NOT NULL,
    label       TEXT NOT NULL,
    earned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, badge_type)
);

CREATE TABLE IF NOT EXISTS endorsements (
    id         TEXT PRIMARY KEY,
    from_id    TEXT NOT NULL REFERENCES users(id),
    to_id      TEXT NOT NULL REFERENCES users(id),
    rosca_id   TEXT REFERENCES roscas(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(from_id, to_id, rosca_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    notif_type TEXT NOT NULL DEFAULT 'info',
    link       TEXT,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blog_posts (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    slug         TEXT UNIQUE NOT NULL,
    excerpt      TEXT NOT NULL,
    body         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'news',
    author_id    TEXT REFERENCES users(id),
    is_published INTEGER NOT NULL DEFAULT 1,
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hanatag_payments (
    id             TEXT PRIMARY KEY,
    sender_id      TEXT NOT NULL REFERENCES users(id),
    recipient_id   TEXT NOT NULL REFERENCES users(id),
    amount_cents   INTEGER NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'EUR',
    note           TEXT,
    status         TEXT NOT NULL DEFAULT 'completed',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fraud_alerts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT REFERENCES users(id),
    alert_type  TEXT NOT NULL,
    risk_level  TEXT NOT NULL DEFAULT 'medium',
    risk_score  INTEGER NOT NULL DEFAULT 50,
    description TEXT NOT NULL,
    amount_cents INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx        ON wallet_transactions(wallet_id, created_at DESC);

CREATE TABLE IF NOT EXISTS freeze_log (
    id              TEXT PRIMARY KEY,
    target_user_id  TEXT NOT NULL REFERENCES users(id),
    admin_id        TEXT NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL,
    freeze_type     TEXT NOT NULL,
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_freeze_log ON freeze_log(target_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contrib_user     ON contributions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ncs_events_user  ON ncs_events(user_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_members_rosca    ON rosca_members(rosca_id);
CREATE INDEX IF NOT EXISTS idx_members_user     ON rosca_members(user_id);
CREATE INDEX IF NOT EXISTS idx_notifs_user      ON notifications(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS pools (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    description          TEXT,
    purpose              TEXT NOT NULL DEFAULT 'general',
    organiser_id         TEXT NOT NULL REFERENCES users(id),
    status               TEXT NOT NULL DEFAULT 'forming',
    currency             TEXT NOT NULL DEFAULT 'EUR',
    annual_amount_cents  INTEGER NOT NULL,
    monthly_amount_cents INTEGER NOT NULL,
    payout_type          TEXT NOT NULL DEFAULT 'single',
    payout_recipient_id  TEXT REFERENCES users(id),
    duration_months      INTEGER NOT NULL DEFAULT 12,
    start_date           TEXT,
    end_date             TEXT,
    ncs_min_score        INTEGER NOT NULL DEFAULT 300,
    is_public            INTEGER NOT NULL DEFAULT 0,
    creation_fee_cents   INTEGER NOT NULL DEFAULT 0,
    total_balance_cents  INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pool_members (
    id              TEXT PRIMARY KEY,
    pool_id         TEXT NOT NULL REFERENCES pools(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT NOT NULL DEFAULT 'pending',
    payment_schedule TEXT NOT NULL DEFAULT 'monthly',
    joined_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pool_id, user_id)
);

CREATE TABLE IF NOT EXISTS pool_contributions (
    id              TEXT PRIMARY KEY,
    pool_id         TEXT NOT NULL REFERENCES pools(id),
    payer_id        TEXT NOT NULL REFERENCES users(id),
    beneficiary_id  TEXT NOT NULL REFERENCES users(id),
    amount_cents    INTEGER NOT NULL,
    period_covered  TEXT NOT NULL,
    months_covered  INTEGER NOT NULL DEFAULT 1,
    paid_on_behalf  INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pool_disbursements (
    id              TEXT PRIMARY KEY,
    pool_id         TEXT NOT NULL REFERENCES pools(id),
    amount_cents    INTEGER NOT NULL,
    recipient_id    TEXT REFERENCES users(id),
    purpose_note    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    requested_by    TEXT NOT NULL REFERENCES users(id),
    approved_by_1   TEXT REFERENCES users(id),
    approved_by_2   TEXT REFERENCES users(id),
    approved_by_3   TEXT REFERENCES users(id),
    rejected_by     TEXT REFERENCES users(id),
    rejection_note  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at     TEXT,
    executed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pool_members_pool   ON pool_members(pool_id, status);
CREATE INDEX IF NOT EXISTS idx_pool_members_user   ON pool_members(user_id);
CREATE INDEX IF NOT EXISTS idx_pool_contribs_pool  ON pool_contributions(pool_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pool_disbursements  ON pool_disbursements(pool_id, status);

CREATE INDEX IF NOT EXISTS idx_hanatag_payments ON hanatag_payments(sender_id, created_at DESC);
"""

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()

    # ── Safe column migrations ─────────────────────────────────────────────────
    # Each ALTER TABLE is wrapped individually — if the column already exists
    # SQLite raises an error which we silently swallow. This is the correct
    # pattern for evolving an existing SQLite schema without data loss.
    safe_migrations = [
        # Freeze controls (v4.9)
        "ALTER TABLE users ADD COLUMN freeze_deposits    INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN freeze_withdrawals INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN freeze_reason      TEXT",
        "ALTER TABLE users ADD COLUMN frozen_by          TEXT",
        "ALTER TABLE users ADD COLUMN frozen_at          TEXT",
        # Pool support (v4.8)
        "ALTER TABLE users ADD COLUMN base_currency TEXT NOT NULL DEFAULT 'EUR'",
        "ALTER TABLE users ADD COLUMN hanatag       TEXT",
        "ALTER TABLE users ADD COLUMN bio           TEXT",
        "ALTER TABLE users ADD COLUMN language      TEXT NOT NULL DEFAULT 'en'",
        "ALTER TABLE users ADD COLUMN notif_email   INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN notif_push    INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN notif_sms     INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN is_admin      INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN admin_role    TEXT",
        "ALTER TABLE users ADD COLUMN kyc_level     TEXT NOT NULL DEFAULT 'phone'",
        # Wallet tx_type (v4.x)
        "ALTER TABLE wallet_transactions ADD COLUMN tx_type TEXT NOT NULL DEFAULT 'other'",
        # ROSCA creation fee (v4.x)
        "ALTER TABLE roscas ADD COLUMN creation_fee_cents INTEGER NOT NULL DEFAULT 0",
        # Rosca member join pending (v4.7)
        "ALTER TABLE rosca_members ADD COLUMN joined_at TEXT",
        # Pool tables
        """CREATE TABLE IF NOT EXISTS pools (
            id                   TEXT PRIMARY KEY,
            name                 TEXT NOT NULL,
            description          TEXT,
            purpose              TEXT NOT NULL DEFAULT 'general',
            organiser_id         TEXT NOT NULL REFERENCES users(id),
            status               TEXT NOT NULL DEFAULT 'forming',
            currency             TEXT NOT NULL DEFAULT 'EUR',
            annual_amount_cents  INTEGER NOT NULL DEFAULT 0,
            monthly_amount_cents INTEGER NOT NULL DEFAULT 0,
            payout_type          TEXT NOT NULL DEFAULT 'single',
            payout_recipient_id  TEXT REFERENCES users(id),
            duration_months      INTEGER NOT NULL DEFAULT 12,
            start_date           TEXT,
            end_date             TEXT,
            ncs_min_score        INTEGER NOT NULL DEFAULT 300,
            is_public            INTEGER NOT NULL DEFAULT 0,
            creation_fee_cents   INTEGER NOT NULL DEFAULT 0,
            total_balance_cents  INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS pool_members (
            id               TEXT PRIMARY KEY,
            pool_id          TEXT NOT NULL REFERENCES pools(id),
            user_id          TEXT NOT NULL REFERENCES users(id),
            role             TEXT NOT NULL DEFAULT 'member',
            status           TEXT NOT NULL DEFAULT 'pending',
            payment_schedule TEXT NOT NULL DEFAULT 'monthly',
            joined_at        TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS pool_contributions (
            id             TEXT PRIMARY KEY,
            pool_id        TEXT NOT NULL REFERENCES pools(id),
            payer_id       TEXT NOT NULL REFERENCES users(id),
            beneficiary_id TEXT NOT NULL REFERENCES users(id),
            amount_cents   INTEGER NOT NULL,
            period_covered TEXT NOT NULL,
            months_covered INTEGER NOT NULL DEFAULT 1,
            paid_on_behalf INTEGER NOT NULL DEFAULT 0,
            note           TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS pool_disbursements (
            id             TEXT PRIMARY KEY,
            pool_id        TEXT NOT NULL REFERENCES pools(id),
            amount_cents   INTEGER NOT NULL,
            recipient_id   TEXT REFERENCES users(id),
            purpose_note   TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            requested_by   TEXT NOT NULL REFERENCES users(id),
            approved_by_1  TEXT REFERENCES users(id),
            approved_by_2  TEXT REFERENCES users(id),
            approved_by_3  TEXT REFERENCES users(id),
            rejected_by    TEXT REFERENCES users(id),
            rejection_note TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            approved_at    TEXT,
            executed_at    TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS freeze_log (
            id              TEXT PRIMARY KEY,
            target_user_id  TEXT NOT NULL REFERENCES users(id),
            admin_id        TEXT NOT NULL REFERENCES users(id),
            action          TEXT NOT NULL,
            freeze_type     TEXT NOT NULL,
            reason          TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS waitlist (
            id         TEXT PRIMARY KEY,
            email      TEXT UNIQUE NOT NULL,
            name       TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        # Indexes
        "CREATE INDEX IF NOT EXISTS idx_pool_members_pool  ON pool_members(pool_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_pool_members_user  ON pool_members(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_pool_contribs_pool ON pool_contributions(pool_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pool_disbursements ON pool_disbursements(pool_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_freeze_log         ON freeze_log(target_user_id, created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_hanatag ON users(hanatag) WHERE hanatag IS NOT NULL",
    ]

    for sql in safe_migrations:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(sql)
            conn.commit()
            conn.close()
        except Exception:
            pass  # Column already exists or table already exists — safe to ignore


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def fetchone(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchone()

def fetchall(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchall()

def wallet_balance(wallet_id):
    row = fetchone(
        "SELECT balance_after FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC LIMIT 1",
        (wallet_id,)
    )
    return row["balance_after"] if row else 0

def get_user_wallets(user_id):
    """Return all open wallets for a user with balances."""
    wallets = fetchall("SELECT * FROM wallets WHERE user_id=? ORDER BY is_default DESC, created_at", (user_id,))
    result = []
    for w in wallets:
        bal = wallet_balance(w["id"])
        cur = w["currency"]
        info = CURRENCIES.get(cur, {"symbol": cur, "name": cur, "flag": ""})
        result.append({**dict(w), "balance": bal, "balance_display": bal/100,
                       "symbol": info["symbol"], "currency_name": info["name"], "flag": info["flag"]})
    return result

def get_default_wallet(user_id):
    w = fetchone("SELECT * FROM wallets WHERE user_id=? AND is_default=1", (user_id,))
    if not w:
        w = fetchone("SELECT * FROM wallets WHERE user_id=? ORDER BY created_at LIMIT 1", (user_id,))
    return w

def post_transaction(wallet_id, amount_cents, description, ref_type=None, ref_id=None, tx_type="other", _db=None):
    def _do(db):
        row = db.execute(
            "SELECT balance_after, currency FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC LIMIT 1",
            (wallet_id,)
        ).fetchone()
        cur_info = db.execute("SELECT currency FROM wallets WHERE id=?", (wallet_id,)).fetchone()
        currency = cur_info["currency"] if cur_info else "EUR"
        current = row["balance_after"] if row else 0
        new_bal = current + amount_cents
        if new_bal < 0:
            raise ValueError(f"Insufficient balance: {CURRENCIES.get(currency,{}).get('symbol','')}{current/100:.2f} available")
        db.execute(
            "INSERT INTO wallet_transactions(id,wallet_id,amount_cents,balance_after,description,ref_type,ref_id,tx_type,currency) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), wallet_id, amount_cents, new_bal, description, ref_type, ref_id, tx_type, currency)
        )
        return new_bal
    if _db is not None:
        return _do(_db)
    with get_db() as db:
        return _do(db)

def convert_currency(user_id, from_currency, to_currency, from_amount_cents):
    """Convert between user wallets. Returns (to_amount, fee_cents)."""
    if from_currency == to_currency:
        raise ValueError("Same currency")
    from_rate = EXCHANGE_RATES.get(from_currency, 1.0)
    to_rate   = EXCHANGE_RATES.get(to_currency, 1.0)
    eur_amount = from_amount_cents / from_rate
    to_amount  = int(eur_amount * to_rate)
    fee_cents  = max(50, int(from_amount_cents * CONVERSION_FEE_RATE))
    to_amount_after_fee = to_amount  # fee deducted from source
    from_wallet = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (user_id, from_currency))
    to_wallet   = fetchone("SELECT id FROM wallets WHERE user_id=? AND currency=?", (user_id, to_currency))
    if not from_wallet: raise ValueError(f"No {from_currency} wallet")
    if not to_wallet:   raise ValueError(f"No {to_currency} wallet. Open it first.")
    total_deduct = from_amount_cents + fee_cents
    with get_db() as db:
        post_transaction(from_wallet["id"], -total_deduct,
                         f"Convert {from_currency}→{to_currency} (fee: {fee_cents/100:.2f})",
                         tx_type="conversion", _db=db)
        post_transaction(to_wallet["id"], to_amount_after_fee,
                         f"Converted from {from_currency}",
                         tx_type="conversion", _db=db)
        db.execute(
            "INSERT INTO currency_conversions(id,user_id,from_wallet_id,to_wallet_id,from_currency,to_currency,from_amount,to_amount,rate,fee_cents) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id, from_wallet["id"], to_wallet["id"],
             from_currency, to_currency, from_amount_cents, to_amount_after_fee,
             to_rate/from_rate, fee_cents)
        )
    return to_amount_after_fee, fee_cents

def push_notification(user_id, title, body, notif_type="info", link=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO notifications(id,user_id,title,body,notif_type,link) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id, title, body, notif_type, link)
        )

def calc_withdrawal_fee(amount_cents, method):
    rate = WITHDRAWAL_FEES.get(method, 0.02)
    return max(WITHDRAWAL_FEE_MIN, int(amount_cents * rate))

def generate_hanatag(full_name):
    import re, random, string
    base = re.sub(r"[^a-z0-9]", "", full_name.lower().replace(" ", ""))[:12]
    suffix = "".join(random.choices(string.digits, k=4))
    return f"@{base}{suffix}"
