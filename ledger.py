"""
SOHANA double-entry ledger.

Every money movement is a *transaction* made of *legs*. Legs land either on a
member wallet (rows in `wallet_transactions`, which the app has always used) or
on one of SOHANA's own system accounts (rows in `ledger_entries`). The legs of
a transaction must add up to zero in every currency, so the whole ledger (the
trial balance) always sums to zero.

Sign convention
---------------
Member wallets and the holding accounts (circle pots, campaign funds, held
withdrawals, suspense, fee revenue...) carry positive balances: money SOHANA
holds for someone. The cash accounts (cash_momo, cash_bank, cash_card) are the
mirror image: a deposit of 10,000 posts -10,000 on cash_momo and +10,000 on the
member's wallet. So "cash really held at MoMo" = -balance(cash_momo), and that
figure is what the daily reconciliation compares with the provider statement.

Tracing
-------
Every franc that enters a member wallet or holding account is a *funding lot*
that remembers where it came from (its root origin: a MoMo, bank or card
deposit, simulated money, or the opening balance). When money moves, lots are
consumed oldest-first at the source and child lots are created at the
destination, so any amount can be traced back to its deposit through any number
of hops.

Tamper evidence
---------------
Each transaction stores a SHA-256 hash of its own content plus the previous
transaction's hash. Editing any past row breaks the chain, which the daily
checks detect.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta

from database import get_db, fetchone, fetchall, CURRENCIES, EXCHANGE_RATES

# ── Accounts ──────────────────────────────────────────────────────────────────

SYSTEM_ACCOUNTS = {
    # code              (label,                                  group)
    "cash_momo":        ("Cash held at MoMo / Airtel Money",      "cash"),
    "cash_bank":        ("Cash held at the bank",                 "cash"),
    "cash_card":        ("Card processor (to be settled)",        "cash"),
    "suspense":         ("Unmatched deposits (suspense)",         "holding"),
    "circle_pots":      ("Circle pots held",                      "holding"),
    "campaign_funds":   ("Campaign funds held",                   "holding"),
    "pool_funds":       ("Pool funds held",                       "holding"),
    "withdrawals_held": ("Withdrawals in progress",               "holding"),
    "fee_revenue":      ("SOHANA fee revenue",                    "revenue"),
    "fx_position":      ("Currency conversion position",          "treasury"),
    "rounding":         ("Rounding",                              "treasury"),
    "chargeback_loss":  ("Card chargebacks not recovered",        "treasury"),
    "adjustments":      ("Manual adjustments (two-person)",       "equity"),
    "simulated_funding":("Simulated money (demo and tests)",      "equity"),
    "opening":          ("Opening balances before the ledger",    "equity"),
}
CASH_ACCOUNTS = {"cash_momo", "cash_bank", "cash_card"}
# Accounts that create money "from outside" when debited: a debit here starts new lots.
ORIGIN_ACCOUNTS = CASH_ACCOUNTS | {"simulated_funding", "opening", "adjustments", "chargeback_loss"}

CHANNELS = {"momo": "cash_momo", "bank": "cash_bank", "card": "cash_card"}

# Currencies with no coins in use: every amount must be a whole unit (multiple of 100 "cents").
ZERO_DECIMAL = {"RWF", "XAF", "XOF", "UGX", "BIF"}

CARD_HOLD_DAYS = 14          # card-funded money cannot be withdrawn for this long
GENESIS = "0" * 64


class LedgerError(ValueError):
    """A transaction was refused; nothing was written."""


def unit(currency):
    return 100 if (currency or "").upper() in ZERO_DECIMAL else 1


def whole(cents, currency):
    """Round an amount to the smallest unit actually payable in `currency`."""
    u = unit(currency)
    return int(round(cents / u)) * u if u > 1 else int(cents)


def floor_whole(cents, currency):
    u = unit(currency)
    return (int(cents) // u) * u if u > 1 else int(cents)


def is_whole(cents, currency):
    return int(cents) % unit(currency) == 0


def fmt_money(cents, currency):
    cur = (currency or "EUR").upper()
    sym = CURRENCIES.get(cur, {}).get("symbol", cur)
    if len(sym) > 1:
        sym += " "
    dp = 0 if cur in ZERO_DECIMAL else 2
    return f"{'-' if cents < 0 else ''}{sym}{abs(cents) / 100:,.{dp}f}".replace(",", " ")


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS ledger_txns (
        id              TEXT PRIMARY KEY,
        seq             INTEGER NOT NULL UNIQUE,
        kind            TEXT NOT NULL,
        memo            TEXT,
        ref_type        TEXT,
        ref_id          TEXT,
        idempotency_key TEXT UNIQUE,
        created_by      TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        prev_hash       TEXT NOT NULL,
        hash            TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ltx_kind ON ledger_txns(kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ltx_ref ON ledger_txns(ref_type, ref_id)",
    """CREATE TABLE IF NOT EXISTS ledger_entries (
        id            TEXT PRIMARY KEY,
        txn_id        TEXT NOT NULL REFERENCES ledger_txns(id),
        account_code  TEXT NOT NULL,
        currency      TEXT NOT NULL,
        sub_ref_type  TEXT,
        sub_ref_id    TEXT,
        amount_cents  INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_le_acct ON ledger_entries(account_code, currency)",
    "CREATE INDEX IF NOT EXISTS idx_le_sub ON ledger_entries(account_code, sub_ref_id)",
    "CREATE INDEX IF NOT EXISTS idx_le_txn ON ledger_entries(txn_id)",
    """CREATE TABLE IF NOT EXISTS funding_lots (
        id               TEXT PRIMARY KEY,
        txn_id           TEXT NOT NULL,
        holder_kind      TEXT NOT NULL,     -- 'wallet' or a system account code
        holder_id        TEXT,              -- wallet id, or the sub-ref (cycle, campaign, withdrawal...)
        currency         TEXT NOT NULL,
        amount_cents     INTEGER NOT NULL,
        remaining_cents  INTEGER NOT NULL,
        parent_lot_id    TEXT,
        root_lot_id      TEXT,
        root_kind        TEXT NOT NULL,     -- cash_momo | cash_bank | cash_card | simulated_funding | opening | adjustments
        root_ref         TEXT,              -- external payment reference of the original deposit
        root_created_at  TEXT NOT NULL,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lots_holder ON funding_lots(holder_kind, holder_id, currency, remaining_cents)",
    "CREATE INDEX IF NOT EXISTS idx_lots_parent ON funding_lots(parent_lot_id)",
    "CREATE INDEX IF NOT EXISTS idx_lots_txn ON funding_lots(txn_id)",
    """CREATE TABLE IF NOT EXISTS withdrawals (
        id             TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        wallet_id      TEXT NOT NULL,
        currency       TEXT NOT NULL,
        amount_cents   INTEGER NOT NULL,
        fee_cents      INTEGER NOT NULL,
        method         TEXT NOT NULL,
        channel        TEXT NOT NULL,          -- momo | bank
        destination    TEXT,
        status         TEXT NOT NULL DEFAULT 'requested',  -- requested|approved|paid|failed|rejected
        approvals_needed INTEGER NOT NULL DEFAULT 1,
        approved_by    TEXT,
        approved_by_2  TEXT,
        decided_at     TEXT,
        paid_ref       TEXT,
        failure_reason TEXT,
        request_txn_id TEXT,
        final_txn_id   TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wd_status ON withdrawals(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_wd_user ON withdrawals(user_id, created_at)",
    """CREATE TABLE IF NOT EXISTS external_payments (
        id           TEXT PRIMARY KEY,
        channel      TEXT NOT NULL,            -- momo | bank | card
        external_ref TEXT NOT NULL,
        direction    TEXT NOT NULL,            -- in | out | reversal
        currency     TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        payer        TEXT,
        reference    TEXT,                     -- member reference typed by the payer (bank)
        status       TEXT NOT NULL,            -- credited | suspense | matched | refunded | reversed | duplicate
        user_id      TEXT,
        wallet_id    TEXT,
        txn_id       TEXT,
        note         TEXT,
        created_by   TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(channel, external_ref, direction)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ext_status ON external_payments(status, created_at)",
    """CREATE TABLE IF NOT EXISTS statement_balances (
        id           TEXT PRIMARY KEY,
        account_code TEXT NOT NULL,
        currency     TEXT NOT NULL,
        balance_cents INTEGER NOT NULL,
        as_of        TEXT NOT NULL,
        entered_by   TEXT,
        note         TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS ledger_adjustment_requests (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        currency     TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        reason       TEXT NOT NULL,
        proposed_by  TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'proposed',
        decided_by   TEXT,
        decided_at   TEXT,
        txn_id       TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS ledger_check_runs (
        id         TEXT PRIMARY KEY,
        run_at     TEXT NOT NULL DEFAULT (datetime('now')),
        passed     INTEGER NOT NULL,
        results    TEXT NOT NULL,
        run_by     TEXT
    )""",
]

MIGRATIONS = [
    "ALTER TABLE wallet_transactions ADD COLUMN txn_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_wt_txn ON wallet_transactions(txn_id)",
    "ALTER TABLE users ADD COLUMN deposit_ref TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_depref ON users(deposit_ref)",
    "ALTER TABLE cycles ADD COLUMN opens_at TEXT",
    "ALTER TABLE contributions ADD COLUMN paid_by TEXT",
    # pools: a fresh database got the web-app schema first; make sure the money columns exist
    "ALTER TABLE pools ADD COLUMN organiser_id TEXT",
    "ALTER TABLE pools ADD COLUMN purpose TEXT NOT NULL DEFAULT 'general'",
    "ALTER TABLE pools ADD COLUMN annual_amount_cents INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pools ADD COLUMN monthly_amount_cents INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pools ADD COLUMN payout_type TEXT NOT NULL DEFAULT 'single'",
    "ALTER TABLE pools ADD COLUMN payout_recipient_id TEXT",
    "ALTER TABLE pools ADD COLUMN duration_months INTEGER NOT NULL DEFAULT 12",
    "ALTER TABLE pools ADD COLUMN ncs_min_score INTEGER NOT NULL DEFAULT 300",
    "ALTER TABLE pools ADD COLUMN creation_fee_cents INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pools ADD COLUMN total_balance_cents INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pools ADD COLUMN updated_at TEXT",
    # Ghana's currency code is GHS (GHC was retired in 2007)
    "UPDATE wallets SET currency='GHS' WHERE currency='GHC'",
    "UPDATE wallet_transactions SET currency='GHS' WHERE currency='GHC'",
    "UPDATE roscas SET currency='GHS' WHERE currency='GHC'",
    "UPDATE campaigns SET currency='GHS' WHERE currency='GHC'",
    "UPDATE pools SET currency='GHS' WHERE currency='GHC'",
]


def deposit_ref_for(user_id):
    """The short reference a member writes on a bank transfer so it is matched to them."""
    row = fetchone("SELECT deposit_ref FROM users WHERE id=?", (user_id,))
    if row and row["deposit_ref"]:
        return row["deposit_ref"]
    import random
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(20):
        ref = "SOH" + "".join(random.choice(alphabet) for _ in range(6))
        try:
            with get_db() as db:
                db.execute("UPDATE users SET deposit_ref=? WHERE id=? AND deposit_ref IS NULL", (ref, user_id))
            row = fetchone("SELECT deposit_ref FROM users WHERE id=?", (user_id,))
            if row and row["deposit_ref"]:
                return row["deposit_ref"]
        except Exception:
            continue
    return None


def ensure_schema():
    with get_db() as db:
        for sql in SCHEMA_SQL:
            db.execute(sql)
    for sql in MIGRATIONS:
        try:
            with get_db() as db:
                db.execute(sql)
        except Exception:
            pass  # column/index already there


# ── Balances ──────────────────────────────────────────────────────────────────

def _wallet_balance(db, wallet_id):
    row = db.execute("SELECT balance_after FROM wallet_transactions WHERE wallet_id=? "
                     "ORDER BY created_at DESC, rowid DESC LIMIT 1", (wallet_id,)).fetchone()
    return row["balance_after"] if row else 0


def _system_balance(db, code, currency):
    row = db.execute("SELECT balance_after FROM ledger_entries WHERE account_code=? AND currency=? "
                     "ORDER BY rowid DESC LIMIT 1", (code, currency)).fetchone()
    return row["balance_after"] if row else 0


def system_balance(code, currency, sub_ref_id=None):
    if sub_ref_id is None:
        row = fetchone("SELECT balance_after FROM ledger_entries WHERE account_code=? AND currency=? "
                       "ORDER BY rowid DESC LIMIT 1", (code, currency))
        return row["balance_after"] if row else 0
    row = fetchone("SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
                   "WHERE account_code=? AND currency=? AND sub_ref_id=?", (code, currency, sub_ref_id))
    return row["s"]


def sub_balance(db, code, currency, sub_ref_id):
    row = db.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
                     "WHERE account_code=? AND currency=? AND sub_ref_id=?",
                     (code, currency, sub_ref_id)).fetchone()
    return row["s"]


# ── Legs ──────────────────────────────────────────────────────────────────────

def W(wallet_id, amount, description="", tx_type="other"):
    """A leg on a member wallet."""
    return {"wallet_id": wallet_id, "amount": int(amount), "description": description, "tx_type": tx_type}


def S(code, currency, amount, sub_ref_type=None, sub_ref_id=None):
    """A leg on a SOHANA system account (optionally scoped to a circle cycle, campaign, ...)."""
    if code not in SYSTEM_ACCOUNTS:
        raise LedgerError(f"Unknown account {code}")
    return {"code": code, "currency": currency.upper(), "amount": int(amount),
            "sub_ref_type": sub_ref_type, "sub_ref_id": sub_ref_id}


def _holder(leg):
    if "wallet_id" in leg:
        return ("wallet", leg["wallet_id"])
    return (leg["code"], leg.get("sub_ref_id"))


# ── Posting ───────────────────────────────────────────────────────────────────

def post(db, kind, legs, memo="", ref_type=None, ref_id=None, idempotency_key=None,
         created_by=None, origin_ref=None, allow_negative=()):
    """Write one balanced transaction. MUST be called inside get_db(immediate=True).

    legs            list of W(...) / S(...) legs
    idempotency_key a key that may only ever be posted once (e.g. 'momo:<provider id>')
    origin_ref      external reference recorded on lots created from a cash/origin account
    allow_negative  system account codes allowed to go below zero (cash accounts always may)

    Returns the transaction id. Raises LedgerError (and writes nothing) if the
    legs do not balance, an amount is not a whole unit, a wallet would go
    negative, or the idempotency key was already used.
    """
    if not legs:
        raise LedgerError("A transaction needs at least two legs")
    legs = [dict(l) for l in legs if int(l["amount"]) != 0]
    if len(legs) < 2:
        raise LedgerError("A transaction needs at least two non-zero legs")

    # currency of every leg
    for l in legs:
        if "wallet_id" in l:
            w = db.execute("SELECT currency FROM wallets WHERE id=?", (l["wallet_id"],)).fetchone()
            if not w:
                raise LedgerError("Wallet not found")
            l["currency"] = w["currency"]
        if not is_whole(l["amount"], l["currency"]):
            raise LedgerError(f"{l['currency']} amounts must be whole units")

    totals = {}
    for l in legs:
        totals[l["currency"]] = totals.get(l["currency"], 0) + l["amount"]
    unbalanced = {c: t for c, t in totals.items() if t != 0}
    if unbalanced:
        raise LedgerError(f"Transaction does not balance: {unbalanced}")

    if idempotency_key:
        if db.execute("SELECT 1 FROM ledger_txns WHERE idempotency_key=?", (idempotency_key,)).fetchone():
            raise LedgerError("duplicate")

    txn_id = str(uuid.uuid4())
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    last = db.execute("SELECT seq, hash FROM ledger_txns ORDER BY seq DESC LIMIT 1").fetchone()
    seq = (last["seq"] + 1) if last else 1
    prev_hash = last["hash"] if last else GENESIS
    # the header goes in first (entries reference it); its hash is filled in once the legs are written
    db.execute("""INSERT INTO ledger_txns(id,seq,kind,memo,ref_type,ref_id,idempotency_key,created_by,created_at,prev_hash,hash)
                  VALUES(?,?,?,?,?,?,?,?,?,?,'')""",
               (txn_id, seq, kind, memo, ref_type, ref_id, idempotency_key, created_by, now, prev_hash))

    canon_legs = []
    for l in legs:
        if "wallet_id" in l:
            bal = _wallet_balance(db, l["wallet_id"]) + l["amount"]
            if bal < 0:
                raise LedgerError(f"Insufficient balance: {fmt_money(bal - l['amount'], l['currency'])} available")
            db.execute("""INSERT INTO wallet_transactions
                          (id,wallet_id,amount_cents,balance_after,description,ref_type,ref_id,tx_type,currency,txn_id,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (str(uuid.uuid4()), l["wallet_id"], l["amount"], bal, l.get("description") or memo,
                        ref_type, ref_id, l.get("tx_type") or kind, l["currency"], txn_id, now))
            canon_legs.append(["W", l["wallet_id"], l["currency"], l["amount"]])
        else:
            bal = _system_balance(db, l["code"], l["currency"]) + l["amount"]
            if l.get("sub_ref_id") and l["code"] not in CASH_ACCOUNTS and l["code"] not in allow_negative \
                    and l["code"] not in ORIGIN_ACCOUNTS and l["code"] not in ("fx_position", "rounding", "chargeback_loss"):
                sub = sub_balance(db, l["code"], l["currency"], l["sub_ref_id"]) + l["amount"]
                if sub < 0:
                    raise LedgerError(f"{SYSTEM_ACCOUNTS[l['code']][0]} would go below zero")
            db.execute("""INSERT INTO ledger_entries
                          (id,txn_id,account_code,currency,sub_ref_type,sub_ref_id,amount_cents,balance_after,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (str(uuid.uuid4()), txn_id, l["code"], l["currency"], l.get("sub_ref_type"),
                        l.get("sub_ref_id"), l["amount"], bal, now))
            canon_legs.append(["S", l["code"], l.get("sub_ref_id") or "", l["currency"], l["amount"]])

    canon_legs.sort(key=lambda l: json.dumps(tuple(l)))
    payload = json.dumps([txn_id, seq, kind, ref_type or "", ref_id or "", now, canon_legs],
                         sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256((prev_hash + payload).encode()).hexdigest()
    db.execute("UPDATE ledger_txns SET hash=? WHERE id=?", (h, txn_id))

    _move_lots(db, txn_id, legs, origin_ref, now)
    return txn_id


def canonical_hash(db, t):
    """Recompute a stored transaction's hash from its rows (used by the checks)."""
    legs = []
    for r in db.execute("SELECT wallet_id, currency, amount_cents, balance_after FROM wallet_transactions WHERE txn_id=? ORDER BY rowid",
                        (t["id"],)):
        legs.append(("W", r["wallet_id"], r["currency"], r["amount_cents"]))
    for r in db.execute("SELECT account_code, sub_ref_id, currency, amount_cents FROM ledger_entries WHERE txn_id=? ORDER BY rowid",
                        (t["id"],)):
        legs.append(("S", r["account_code"], r["sub_ref_id"] or "", r["currency"], r["amount_cents"]))
    return sorted(legs, key=lambda l: json.dumps(l))


# ── Funding lots ──────────────────────────────────────────────────────────────

def _move_lots(db, txn_id, legs, origin_ref, now):
    """Pair debits with credits per currency (in leg order) and move lots along each pair.
    A conversion bridges currencies through fx_position: lots consumed into fx_position in one
    currency become the parents of lots that leave fx_position in the other currency."""
    fx_parents = []  # (lot_row, amount) consumed into fx_position, any currency
    by_cur = {}
    for l in legs:
        by_cur.setdefault(l["currency"], []).append(l)

    pending_fx_out = []
    for cur, cl in by_cur.items():
        debits = [[l, -l["amount"]] for l in cl if l["amount"] < 0]
        credits = [[l, l["amount"]] for l in cl if l["amount"] > 0]
        di = 0
        for cr in credits:
            need = cr[1]
            while need > 0 and di < len(debits):
                take = min(need, debits[di][1])
                src, dst = debits[di][0], cr[0]
                if src.get("code") == "fx_position":
                    pending_fx_out.append((dst, take, cur))
                else:
                    consumed = _consume(db, src, cur, take, txn_id, origin_ref, now)
                    if dst.get("code") == "fx_position":
                        fx_parents.extend(consumed)
                    else:
                        for lot, amt in consumed:
                            _child(db, lot, dst, cur, amt, txn_id, now)
                debits[di][1] -= take
                need -= take
                if debits[di][1] == 0:
                    di += 1

    # lots leaving fx_position (the other side of a conversion) inherit the lots that went in
    if pending_fx_out:
        total_in = sum(a for _, a in fx_parents) or 1
        for dst, amt, cur in pending_fx_out:
            if not fx_parents:
                _new_root(db, dst, cur, amt, txn_id, "opening", None, now)
                continue
            left = amt
            for i, (lot, a) in enumerate(fx_parents):
                part = left if i == len(fx_parents) - 1 else int(amt * a / total_in)
                if part > 0:
                    _child(db, lot, dst, cur, part, txn_id, now)
                    left -= part


def _consume(db, src_leg, cur, amount, txn_id, origin_ref, now):
    """Take `amount` out of the source holder's lots, oldest first. Origin accounts mint a root lot."""
    hk, hid = _holder(src_leg)
    if hk in ORIGIN_ACCOUNTS:
        lot = {"id": str(uuid.uuid4()), "root_kind": hk, "root_ref": origin_ref, "root_created_at": now}
        lot["root_lot_id"] = lot["id"]
        db.execute("""INSERT INTO funding_lots(id,txn_id,holder_kind,holder_id,currency,amount_cents,remaining_cents,
                      parent_lot_id,root_lot_id,root_kind,root_ref,root_created_at,created_at)
                      VALUES(?,?,?,?,?,?,0,NULL,?,?,?,?,?)""",
                   (lot["id"], txn_id, hk, hid, cur, amount, lot["id"], hk, origin_ref, now, now))
        return [(lot, amount)]
    out, need = [], amount
    rows = db.execute("""SELECT * FROM funding_lots WHERE holder_kind=? AND holder_id IS ? AND currency=?
                         AND remaining_cents>0 ORDER BY root_created_at, created_at, rowid""",
                      (hk, hid, cur)).fetchall()
    for r in rows:
        if need <= 0:
            break
        take = min(need, r["remaining_cents"])
        db.execute("UPDATE funding_lots SET remaining_cents=remaining_cents-? WHERE id=?", (take, r["id"]))
        out.append((dict(r), take))
        need -= take
    if need > 0:
        # money with no lot (should not happen once the opening migration has run): record it honestly
        lot = {"id": str(uuid.uuid4()), "root_kind": "opening", "root_ref": "untraced", "root_created_at": now}
        lot["root_lot_id"] = lot["id"]
        db.execute("""INSERT INTO funding_lots(id,txn_id,holder_kind,holder_id,currency,amount_cents,remaining_cents,
                      parent_lot_id,root_lot_id,root_kind,root_ref,root_created_at,created_at)
                      VALUES(?,?,?,?,?,?,0,NULL,?,?,?,?,?)""",
                   (lot["id"], txn_id, hk, hid, cur, need, lot["id"], "opening", "untraced", now, now))
        out.append((lot, need))
    return out


def _child(db, parent, dst_leg, cur, amount, txn_id, now):
    hk, hid = _holder(dst_leg)
    lid = str(uuid.uuid4())
    # money paid out of SOHANA (to a cash account) leaves a closed lot: the record of where it went
    remaining = 0 if hk in ORIGIN_ACCOUNTS else amount
    db.execute("""INSERT INTO funding_lots(id,txn_id,holder_kind,holder_id,currency,amount_cents,remaining_cents,
                  parent_lot_id,root_lot_id,root_kind,root_ref,root_created_at,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (lid, txn_id, hk, hid, cur, amount, remaining, parent["id"], parent["root_lot_id"],
                parent["root_kind"], parent.get("root_ref"), parent["root_created_at"], now))
    return lid


def _new_root(db, dst_leg, cur, amount, txn_id, kind, ref, now):
    hk, hid = _holder(dst_leg)
    lid = str(uuid.uuid4())
    db.execute("""INSERT INTO funding_lots(id,txn_id,holder_kind,holder_id,currency,amount_cents,remaining_cents,
                  parent_lot_id,root_lot_id,root_kind,root_ref,root_created_at,created_at)
                  VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?,?)""",
               (lid, txn_id, hk, hid, cur, amount, amount, lid, kind, ref, now, now))


def card_held_cents(db, wallet_id, currency, days=CARD_HOLD_DAYS):
    """Money in a wallet that came from a card payment less than `days` ago (not yet withdrawable)."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute("""SELECT COALESCE(SUM(remaining_cents),0) AS s FROM funding_lots
                        WHERE holder_kind='wallet' AND holder_id=? AND currency=? AND remaining_cents>0
                          AND root_kind='cash_card' AND root_created_at > ?""",
                     (wallet_id, currency, since)).fetchone()
    return row["s"]


def trace_lot(lot_id, depth=0, max_depth=40):
    """Walk a lot back to its root deposit. Returns the chain, newest first."""
    chain = []
    cur = fetchone("SELECT * FROM funding_lots WHERE id=?", (lot_id,))
    while cur and depth < max_depth:
        chain.append(dict(cur))
        if not cur["parent_lot_id"]:
            break
        cur = fetchone("SELECT * FROM funding_lots WHERE id=?", (cur["parent_lot_id"],))
        depth += 1
    return chain


def sources_of_txn(txn_id):
    """For every lot a transaction created, where its money originally came from."""
    rows = fetchall("""SELECT l.*, p.holder_kind AS from_kind, p.holder_id AS from_id
                       FROM funding_lots l LEFT JOIN funding_lots p ON p.id=l.parent_lot_id
                       WHERE l.txn_id=? ORDER BY l.rowid""", (txn_id,))
    return [dict(r) for r in rows]


# ── Opening balances (one-off, when the ledger is first switched on) ─────────

def open_books(created_by="system"):
    """Bring anything that exists outside the ledger into it, with an 'opening' counterpart.

    Wallet rows written by the old single-entry code (no txn_id), circle pots, campaign
    funds and pool balances that the ledger does not hold yet are posted once against the
    'opening' account, so the trial balance is zero and every franc gets a lot
    (root: opening). Safe to run on every start: it only posts differences.
    Returns the number of opening transactions written."""
    written = 0
    with get_db(immediate=True) as db:
        per_cur = {}
        for r in db.execute("""SELECT wallet_id, currency, SUM(amount_cents) AS s, COUNT(*) AS n
                               FROM wallet_transactions WHERE txn_id IS NULL GROUP BY wallet_id, currency""").fetchall():
            per_cur.setdefault(r["currency"], {"wallets": [], "sys": []})["wallets"].append((r["wallet_id"], r["s"]))

        def add(cur, code, amount, sub_type, sub_id):
            if amount:
                per_cur.setdefault(cur, {"wallets": [], "sys": []})["sys"].append(S(code, cur, amount, sub_type, sub_id))

        for c in db.execute("""SELECT cy.id, COALESCE(r.currency,'EUR') AS currency,
                                 (SELECT COALESCE(SUM(amount_cents),0) FROM contributions WHERE cycle_id=cy.id
                                  AND status IN ('paid','late')) AS paid
                               FROM cycles cy JOIN roscas r ON r.id=cy.rosca_id
                               WHERE cy.status IN ('open','collecting')""").fetchall():
            add(c["currency"], "circle_pots", c["paid"] - sub_balance(db, "circle_pots", c["currency"], c["id"]), "cycle", c["id"])
        try:
            for c in db.execute("SELECT id, COALESCE(currency,'EUR') AS currency, raised_cents, withdrawn_cents FROM campaigns").fetchall():
                held = (c["raised_cents"] or 0) - (c["withdrawn_cents"] or 0)
                add(c["currency"], "campaign_funds", held - sub_balance(db, "campaign_funds", c["currency"], c["id"]), "campaign", c["id"])
        except Exception:
            pass
        try:
            for p in db.execute("SELECT id, COALESCE(currency,'EUR') AS currency, total_balance_cents FROM pools").fetchall():
                add(p["currency"], "pool_funds", (p["total_balance_cents"] or 0) - sub_balance(db, "pool_funds", p["currency"], p["id"]), "pool", p["id"])
        except Exception:
            pass

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for cur in sorted(per_cur):
            grp = per_cur[cur]
            total = sum(a for _, a in grp["wallets"]) + sum(l["amount"] for l in grp["sys"])
            if not grp["wallets"] and not grp["sys"]:
                continue
            txn_id = str(uuid.uuid4())
            last = db.execute("SELECT seq, hash FROM ledger_txns ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev = last["hash"] if last else GENESIS
            db.execute("""INSERT INTO ledger_txns(id,seq,kind,memo,ref_type,ref_id,idempotency_key,created_by,created_at,prev_hash,hash)
                          VALUES(?,?,?,?,?,?,?,?,?,?,'')""",
                       (txn_id, seq, "opening", f"Opening balances brought into the ledger ({cur})", None, None, None,
                        created_by, now, prev))
            if total:
                db.execute("""INSERT INTO ledger_entries(id,txn_id,account_code,currency,amount_cents,balance_after,created_at)
                              VALUES(?,?,?,?,?,?,?)""",
                           (str(uuid.uuid4()), txn_id, "opening", cur, -total, _system_balance(db, "opening", cur) - total, now))
            for l in grp["sys"]:
                db.execute("""INSERT INTO ledger_entries(id,txn_id,account_code,currency,sub_ref_type,sub_ref_id,amount_cents,balance_after,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?)""",
                           (str(uuid.uuid4()), txn_id, l["code"], cur, l["sub_ref_type"], l["sub_ref_id"], l["amount"],
                            _system_balance(db, l["code"], cur) + l["amount"], now))
                if l["amount"] > 0:
                    _new_root(db, l, cur, l["amount"], txn_id, "opening", "pre-ledger balance", now)
            for wid, amt in grp["wallets"]:
                # the old rows join this transaction: their amounts are its wallet legs
                db.execute("UPDATE wallet_transactions SET txn_id=? WHERE wallet_id=? AND txn_id IS NULL", (txn_id, wid))
                if amt > 0:
                    _new_root(db, {"wallet_id": wid}, cur, amt, txn_id, "opening", "pre-ledger balance", now)
            legs = canonical_hash(db, {"id": txn_id, "kind": "opening"})
            payload = json.dumps([txn_id, seq, "opening", "", "", now, [list(l) for l in legs]],
                                 sort_keys=True, separators=(",", ":"))
            h = hashlib.sha256((prev + payload).encode()).hexdigest()
            db.execute("UPDATE ledger_txns SET hash=? WHERE id=?", (h, txn_id))
            written += 1
    return written


# ── Reports ───────────────────────────────────────────────────────────────────

def trial_balance():
    """Every account's balance per currency. Member wallets are summed into one line per currency."""
    rows = []
    for r in fetchall("""SELECT w.currency, COUNT(*) AS n,
                                COALESCE(SUM((SELECT wt.balance_after FROM wallet_transactions wt WHERE wt.wallet_id=w.id
                                              ORDER BY wt.created_at DESC, wt.rowid DESC LIMIT 1)),0) AS bal
                         FROM wallets w GROUP BY w.currency"""):
        rows.append({"code": "member_wallets", "label": "Member wallets", "group": "liability",
                     "currency": r["currency"], "balance": r["bal"], "count": r["n"]})
    for r in fetchall("""SELECT e.account_code, e.currency, e.balance_after
                         FROM ledger_entries e
                         JOIN (SELECT account_code, currency, MAX(rowid) AS m FROM ledger_entries GROUP BY account_code, currency) x
                           ON x.m=e.rowid"""):
        label, group = SYSTEM_ACCOUNTS.get(r["account_code"], (r["account_code"], "other"))
        rows.append({"code": r["account_code"], "label": label, "group": group,
                     "currency": r["currency"], "balance": r["balance_after"], "count": None})
    return rows


def equation(currency):
    """The daily equation for one currency: cash held vs everything owed."""
    tb = [r for r in trial_balance() if r["currency"] == currency]
    get = lambda code: next((r["balance"] for r in tb if r["code"] == code), 0)
    cash = {c: -get(c) for c in CASH_ACCOUNTS}
    owed = {
        "member_wallets": get("member_wallets"), "circle_pots": get("circle_pots"),
        "campaign_funds": get("campaign_funds"), "pool_funds": get("pool_funds"),
        "withdrawals_held": get("withdrawals_held"), "suspense": get("suspense"),
        "fee_revenue": get("fee_revenue"),
    }
    other = {c: get(c) for c in ("fx_position", "rounding", "chargeback_loss", "adjustments", "simulated_funding", "opening")}
    total = sum(r["balance"] for r in tb)
    return {"currency": currency, "cash": cash, "cash_total": sum(cash.values()), "owed": owed,
            "owed_total": sum(owed.values()), "other": other, "trial_balance_sum": total}


def currencies_in_use():
    cs = {r["currency"] for r in fetchall("SELECT DISTINCT currency FROM wallets")}
    cs |= {r["currency"] for r in fetchall("SELECT DISTINCT currency FROM ledger_entries")}
    return sorted(cs)


# ── Daily checks ──────────────────────────────────────────────────────────────

def run_checks(run_by="system", save=True):
    """Every invariant from the audit map. Returns (passed, [ {name, ok, detail} ])."""
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    with get_db() as db:
        # 1. running balances: member wallets
        bad = db.execute("""SELECT w.id FROM wallets w WHERE
            (SELECT COALESCE(SUM(amount_cents),0) FROM wallet_transactions t WHERE t.wallet_id=w.id) !=
            COALESCE((SELECT balance_after FROM wallet_transactions t WHERE t.wallet_id=w.id
                      ORDER BY created_at DESC, rowid DESC LIMIT 1),0)""").fetchall()
        add("Running balances (member wallets)", not bad, f"{len(bad)} wallet(s) out of step" if bad else "")

        # 2. running balances: system accounts
        badsys = db.execute("""SELECT account_code, currency FROM ledger_entries GROUP BY account_code, currency
            HAVING SUM(amount_cents) != (SELECT balance_after FROM ledger_entries e2
               WHERE e2.account_code=ledger_entries.account_code AND e2.currency=ledger_entries.currency
               ORDER BY rowid DESC LIMIT 1)""").fetchall()
        add("Running balances (SOHANA accounts)", not badsys,
            ", ".join(f"{r['account_code']} {r['currency']}" for r in badsys))

        # 3. no overdraft
        neg = db.execute("SELECT COUNT(*) AS c FROM wallet_transactions WHERE balance_after<0").fetchone()["c"]
        negsub = db.execute("""SELECT account_code, sub_ref_id, currency, SUM(amount_cents) s FROM ledger_entries
                               WHERE sub_ref_id IS NOT NULL AND account_code IN
                               ('circle_pots','campaign_funds','pool_funds','withdrawals_held','suspense')
                               GROUP BY account_code, sub_ref_id, currency HAVING s<0""").fetchall()
        add("No balance below zero", neg == 0 and not negsub,
            (f"{neg} wallet row(s) negative; " if neg else "") + (f"{len(negsub)} held balance(s) negative" if negsub else ""))

        # 4. every transaction balances per currency
        unbal = db.execute("""SELECT txn_id, currency, SUM(a) s FROM (
                                 SELECT txn_id, currency, amount_cents a FROM wallet_transactions WHERE txn_id IS NOT NULL
                                 UNION ALL SELECT txn_id, currency, amount_cents a FROM ledger_entries)
                              GROUP BY txn_id, currency HAVING s != 0""").fetchall()
        add("Every transaction balances", not unbal, f"{len(unbal)} unbalanced" if unbal else "")

        # 5. money moved outside the ledger (a wallet row with no transaction)
        orphan = db.execute("SELECT COUNT(*) AS c FROM wallet_transactions WHERE txn_id IS NULL").fetchone()["c"]
        add("No money moved outside the ledger", orphan == 0, f"{orphan} wallet entr(ies) without a transaction" if orphan else "")

        # 6. trial balance sums to zero per currency
        tb_bad = []
        for cur in currencies_in_use():
            eq = equation(cur)
            if eq["trial_balance_sum"] != 0:
                tb_bad.append(f"{cur} {fmt_money(eq['trial_balance_sum'], cur)}")
        add("Trial balance is zero in every currency", not tb_bad, "; ".join(tb_bad))

        # 7. circle pots = paid contributions of each open cycle
        pot_bad = []
        for c in db.execute("""SELECT cy.id, r.currency,
                                 (SELECT COALESCE(SUM(amount_cents),0) FROM contributions WHERE cycle_id=cy.id AND status IN ('paid','late')) AS paid
                               FROM cycles cy JOIN roscas r ON r.id=cy.rosca_id WHERE cy.status IN ('open','collecting')""").fetchall():
            held = sub_balance(db, "circle_pots", c["currency"] or "EUR", c["id"])
            if held != c["paid"]:
                pot_bad.append(c["id"][:8])
        add("Each open circle pot equals its paid contributions", not pot_bad, ", ".join(pot_bad))

        # 8. completed cycles: pot fully paid out
        done_bad = []
        for c in db.execute("""SELECT cy.id, r.currency FROM cycles cy JOIN roscas r ON r.id=cy.rosca_id
                               WHERE cy.status='completed'""").fetchall():
            if sub_balance(db, "circle_pots", c["currency"] or "EUR", c["id"]) != 0:
                done_bad.append(c["id"][:8])
        add("Every completed cycle paid its whole pot out", not done_bad, ", ".join(done_bad))

        # 9. campaigns: held = raised - withdrawn
        camp_bad = []
        try:
            for c in db.execute("SELECT id, currency, raised_cents, withdrawn_cents FROM campaigns").fetchall():
                held = sub_balance(db, "campaign_funds", c["currency"] or "EUR", c["id"])
                if held != (c["raised_cents"] or 0) - (c["withdrawn_cents"] or 0):
                    camp_bad.append(c["id"][:8])
        except Exception:
            pass
        add("Campaign funds held = raised - released", not camp_bad, ", ".join(camp_bad))

        # 10. held withdrawals match open requests
        wd_bad = []
        for w in db.execute("SELECT id, currency, amount_cents, fee_cents, status FROM withdrawals").fetchall():
            held = sub_balance(db, "withdrawals_held", w["currency"], w["id"])
            want = (w["amount_cents"] + w["fee_cents"]) if w["status"] in ("requested", "approved") else 0
            if held != want:
                wd_bad.append(w["id"][:8])
        add("Held withdrawals match open requests", not wd_bad, ", ".join(wd_bad))

        # 11. whole units
        frac = 0
        for cur in ZERO_DECIMAL:
            frac += db.execute("SELECT COUNT(*) AS c FROM wallet_transactions WHERE currency=? AND (amount_cents % 100 != 0 OR balance_after % 100 != 0)",
                               (cur,)).fetchone()["c"]
            frac += db.execute("SELECT COUNT(*) AS c FROM ledger_entries WHERE currency=? AND amount_cents % 100 != 0",
                               (cur,)).fetchone()["c"]
        add("No fractions in whole-unit currencies (RWF, XAF...)", frac == 0, f"{frac} entr(ies) with fractions" if frac else "")

        # 12. lots: every wallet's balance is covered by its lots
        lot_bad = 0
        for w in db.execute("SELECT id, currency FROM wallets").fetchall():
            bal = _wallet_balance(db, w["id"])
            lots = db.execute("SELECT COALESCE(SUM(remaining_cents),0) AS s FROM funding_lots WHERE holder_kind='wallet' AND holder_id=? AND currency=?",
                              (w["id"], w["currency"])).fetchone()["s"]
            if lots != bal:
                lot_bad += 1
        add("Every franc in every wallet traces to a deposit", lot_bad == 0, f"{lot_bad} wallet(s) not fully traced" if lot_bad else "")

        # 13. hash chain
        prev, broken = GENESIS, None
        for t in db.execute("SELECT * FROM ledger_txns ORDER BY seq").fetchall():
            if t["prev_hash"] != prev:
                broken = t["seq"]
                break
            legs = canonical_hash(db, t)
            payload = json.dumps([t["id"], t["seq"], t["kind"], t["ref_type"] or "", t["ref_id"] or "", t["created_at"],
                                  [list(l) for l in legs]], sort_keys=True, separators=(",", ":"))
            if hashlib.sha256((prev + payload).encode()).hexdigest() != t["hash"]:
                broken = t["seq"]
                break
            prev = t["hash"]
        add("Tamper evidence: hash chain unbroken", broken is None, f"breaks at transaction #{broken}" if broken else "")

        # 14. external payments credited once
        dup = db.execute("""SELECT COUNT(*) AS c FROM (SELECT txn_id FROM external_payments WHERE txn_id IS NOT NULL
                            GROUP BY txn_id HAVING COUNT(*)>1)""").fetchone()["c"]
        add("Each external payment credited exactly once", dup == 0, f"{dup} credited twice" if dup else "")

        # 15. statement reconciliation (only where a statement balance was entered)
        rec = []
        for s in db.execute("""SELECT sb.* FROM statement_balances sb JOIN
                               (SELECT account_code, currency, MAX(created_at) m FROM statement_balances GROUP BY account_code, currency) x
                               ON x.account_code=sb.account_code AND x.currency=sb.currency AND x.m=sb.created_at""").fetchall():
            held = -_system_balance(db, s["account_code"], s["currency"])
            if held != s["balance_cents"]:
                rec.append(f"{s['account_code']} {s['currency']}: ledger {fmt_money(held, s['currency'])} vs statement {fmt_money(s['balance_cents'], s['currency'])}")
        add("Cash accounts match the latest statements", not rec, "; ".join(rec) if rec else "no statement entered yet")

    passed = all(c["ok"] for c in out)
    if save:
        with get_db() as db:
            db.execute("INSERT INTO ledger_check_runs(id,passed,results,run_by) VALUES(?,?,?,?)",
                       (str(uuid.uuid4()), 1 if passed else 0, json.dumps(out), run_by))
    return passed, out


# ── Mode ──────────────────────────────────────────────────────────────────────
import os

def money_mode():
    """'simulated' (default): money enters only as labelled simulated funds or through the
    settlement simulator. 'live': only real provider/bank confirmations may create money."""
    return "live" if os.environ.get("MONEY_MODE", "simulated").lower() == "live" else "simulated"


def is_simulated():
    return money_mode() == "simulated"


# ── Helpers used by every money path ──────────────────────────────────────────

def wallet_of(db, user_id, currency, create=False):
    currency = currency.upper()
    w = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency=?", (user_id, currency)).fetchone()
    if not w and create:
        db.execute("INSERT INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,0)",
                   (str(uuid.uuid4()), user_id, currency))
        w = db.execute("SELECT id FROM wallets WHERE user_id=? AND currency=?", (user_id, currency)).fetchone()
    return w["id"] if w else None


def balance(db, wallet_id):
    return _wallet_balance(db, wallet_id)


def rate(currency):
    r = EXCHANGE_RATES.get((currency or "EUR").upper())
    if not r:
        raise LedgerError(f"Unsupported currency {currency}")
    return r


def from_eur(eur_cents, currency):
    """An EUR-denominated amount (fees, limits) expressed in `currency`, rounded to a payable unit."""
    return whole(int(round(eur_cents * rate(currency))), currency)


def to_eur(cents, currency):
    return int(round(cents / rate(currency)))


# ── Deposits (three entry points + simulated money) ──────────────────────────

def credit_simulated(user_id, currency, cents, memo="Simulated deposit", created_by=None, key=None):
    """Simulated money (demo, tests). Refused in live mode."""
    if not is_simulated():
        raise LedgerError("Simulated money is switched off on this service")
    with get_db(immediate=True) as db:
        wid = wallet_of(db, user_id, currency, create=True)
        return post(db, "deposit_simulated",
                    [S("simulated_funding", currency, -cents), W(wid, cents, memo, "deposit")],
                    memo=memo, created_by=created_by, idempotency_key=key, origin_ref="simulated")


def receive_external(channel, external_ref, currency, cents, reference=None, user_id=None,
                     payer=None, created_by=None, note=None):
    """A confirmed payment arrived from MoMo, the bank or the card processor.

    Credited exactly once (the provider's reference is the idempotency key). If we can tell
    whose money it is (user_id, or a member reference typed on the bank transfer) it goes to
    their wallet; otherwise it waits in suspense until someone matches it.
    Returns (status, external_payment_id)."""
    channel = channel.lower()
    if channel not in CHANNELS:
        raise LedgerError("Unknown channel")
    currency = currency.upper()
    if cents <= 0:
        raise LedgerError("Amount must be positive")
    cash = CHANNELS[channel]
    with get_db(immediate=True) as db:
        prior = db.execute("SELECT id, status FROM external_payments WHERE channel=? AND external_ref=? AND direction='in'",
                           (channel, external_ref)).fetchone()
        if prior:
            # a provider resending the same confirmation: record the attempt, move nothing
            db.execute("""INSERT OR IGNORE INTO external_payments(id,channel,external_ref,direction,currency,amount_cents,
                          payer,reference,status,note,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (str(uuid.uuid4()), channel, external_ref + "#dup" + uuid.uuid4().hex[:6], "in", currency, cents,
                        payer, reference, "duplicate", f"Repeat of {external_ref}: ignored", created_by))
            return "duplicate", prior["id"]
        if not user_id and reference:
            ref = reference.strip().upper()
            u = db.execute("SELECT id FROM users WHERE UPPER(COALESCE(deposit_ref,''))=? OR UPPER(COALESCE(hanatag,''))=? "
                           "OR UPPER(COALESCE(hanatag,''))=?", (ref, ref, "@" + ref.lstrip("@"))).fetchone()
            user_id = u["id"] if u else None
        eid = str(uuid.uuid4())
        key = f"{channel}:{external_ref}"
        if user_id:
            wid = wallet_of(db, user_id, currency, create=True)
            txn = post(db, f"deposit_{channel}",
                       [S(cash, currency, -cents), W(wid, cents, f"Deposit by {_CH_LABEL[channel]}", "deposit")],
                       memo=f"{_CH_LABEL[channel]} deposit {external_ref}", ref_type="external_payment", ref_id=eid,
                       idempotency_key=key, created_by=created_by, origin_ref=external_ref)
            status = "credited"
        else:
            txn = post(db, f"deposit_{channel}_unmatched",
                       [S(cash, currency, -cents), S("suspense", currency, cents, "external_payment", eid)],
                       memo=f"Unmatched {_CH_LABEL[channel]} deposit {external_ref}", ref_type="external_payment",
                       ref_id=eid, idempotency_key=key, created_by=created_by, origin_ref=external_ref)
            status = "suspense"
        db.execute("""INSERT INTO external_payments(id,channel,external_ref,direction,currency,amount_cents,payer,reference,
                      status,user_id,wallet_id,txn_id,note,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (eid, channel, external_ref, "in", currency, cents, payer, reference, status, user_id,
                    wallet_of(db, user_id, currency) if user_id else None, txn, note, created_by))
    return status, eid


_CH_LABEL = {"momo": "Mobile Money", "bank": "bank transfer", "card": "card"}


def match_suspense(external_payment_id, user_id, created_by):
    with get_db(immediate=True) as db:
        e = db.execute("SELECT * FROM external_payments WHERE id=?", (external_payment_id,)).fetchone()
        if not e or e["status"] != "suspense":
            raise LedgerError("This payment is not waiting in suspense")
        wid = wallet_of(db, user_id, e["currency"], create=True)
        txn = post(db, "suspense_match",
                   [S("suspense", e["currency"], -e["amount_cents"], "external_payment", e["id"]),
                    W(wid, e["amount_cents"], f"Deposit by {_CH_LABEL[e['channel']]} (matched)", "deposit")],
                   memo=f"Matched {e['external_ref']} to a member", ref_type="external_payment", ref_id=e["id"],
                   idempotency_key=f"match:{e['id']}", created_by=created_by)
        db.execute("UPDATE external_payments SET status='matched', user_id=?, wallet_id=?, note=COALESCE(note,'')||? WHERE id=?",
                   (user_id, wid, f" matched by admin; txn {txn[:8]}", e["id"]))
        return txn


def refund_suspense(external_payment_id, created_by):
    with get_db(immediate=True) as db:
        e = db.execute("SELECT * FROM external_payments WHERE id=?", (external_payment_id,)).fetchone()
        if not e or e["status"] != "suspense":
            raise LedgerError("This payment is not waiting in suspense")
        cash = CHANNELS[e["channel"]]
        txn = post(db, "suspense_refund",
                   [S("suspense", e["currency"], -e["amount_cents"], "external_payment", e["id"]),
                    S(cash, e["currency"], e["amount_cents"])],
                   memo=f"Returned unmatched {e['external_ref']} to the sender", ref_type="external_payment",
                   ref_id=e["id"], idempotency_key=f"refund:{e['id']}", created_by=created_by)
        db.execute("UPDATE external_payments SET status='refunded' WHERE id=?", (e["id"],))
        return txn


def chargeback(external_payment_id, created_by):
    """The card processor took a card deposit back. Whatever is still in the member's wallet is
    taken; anything already spent is a loss SOHANA carries (and why card money is held)."""
    with get_db(immediate=True) as db:
        e = db.execute("SELECT * FROM external_payments WHERE id=?", (external_payment_id,)).fetchone()
        if not e or e["channel"] != "card" or e["status"] not in ("credited", "matched"):
            raise LedgerError("Only a credited card deposit can be charged back")
        amt, cur = e["amount_cents"], e["currency"]
        bal = _wallet_balance(db, e["wallet_id"])
        take = min(bal, amt)
        legs = [S("cash_card", cur, amt)]
        if take:
            legs.append(W(e["wallet_id"], -take, "Card payment reversed by your bank", "chargeback"))
        if amt - take:
            legs.append(S("chargeback_loss", cur, -(amt - take)))
        txn = post(db, "chargeback", legs, memo=f"Chargeback of {e['external_ref']}", ref_type="external_payment",
                   ref_id=e["id"], idempotency_key=f"chargeback:{e['id']}", created_by=created_by)
        db.execute("UPDATE external_payments SET status='reversed' WHERE id=?", (e["id"],))
        return txn, take, amt - take


# ── Withdrawals: request → approve → paid | failed | rejected ────────────────

WITHDRAW_SECOND_APPROVAL_EUR = 100_000   # EUR 1,000 equivalent and above needs two different admins


def withdrawable(db, wallet_id, currency):
    return max(0, _wallet_balance(db, wallet_id) - card_held_cents(db, wallet_id, currency))


def request_withdrawal(user_id, currency, cents, fee, method, destination):
    channel = "momo" if method in ("mobile_money", "momo", "airtel") else "bank"
    with get_db(immediate=True) as db:
        wid = wallet_of(db, user_id, currency)
        if not wid:
            raise LedgerError(f"No {currency} balance")
        total = cents + fee
        avail = withdrawable(db, wid, currency)
        if total > avail:
            held = card_held_cents(db, wid, currency)
            extra = f" ({fmt_money(held, currency)} from a recent card payment is held for {CARD_HOLD_DAYS} days)" if held else ""
            raise LedgerError(f"You can withdraw up to {fmt_money(avail, currency)} including the fee{extra}")
        wd = str(uuid.uuid4())
        need = 2 if to_eur(cents, currency) >= WITHDRAW_SECOND_APPROVAL_EUR else 1
        txn = post(db, "withdrawal_request",
                   [W(wid, -total, f"Withdrawal to {destination} (requested, fee {fmt_money(fee, currency)})", "withdrawal"),
                    S("withdrawals_held", currency, total, "withdrawal", wd)],
                   memo="Withdrawal requested", ref_type="withdrawal", ref_id=wd, idempotency_key=f"wd:req:{wd}",
                   created_by=user_id)
        db.execute("""INSERT INTO withdrawals(id,user_id,wallet_id,currency,amount_cents,fee_cents,method,channel,destination,
                      approvals_needed,request_txn_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (wd, user_id, wid, currency, cents, fee, method, channel, destination, need, txn))
        return wd, need


def approve_withdrawal(wd_id, admin_id):
    """Returns the new status: 'approved' (waiting for a second admin) or 'approved' + ready."""
    with get_db(immediate=True) as db:
        w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not w or w["status"] != "requested":
            raise LedgerError("This withdrawal is not waiting for approval")
        if admin_id == w["user_id"]:
            raise LedgerError("You cannot approve your own withdrawal")
        if not w["approved_by"]:
            db.execute("UPDATE withdrawals SET approved_by=? WHERE id=?", (admin_id, wd_id))
            count = 1
        else:
            if w["approved_by"] == admin_id:
                raise LedgerError("A second, different admin must approve this withdrawal")
            db.execute("UPDATE withdrawals SET approved_by_2=? WHERE id=?", (admin_id, wd_id))
            count = 2
        if count >= w["approvals_needed"]:
            db.execute("UPDATE withdrawals SET status='approved', decided_at=datetime('now') WHERE id=?", (wd_id,))
            return "approved"
        return "needs_second_approval"


def mark_withdrawal_paid(wd_id, paid_ref, admin_id):
    """The money actually left our MoMo/bank account (provider reference recorded)."""
    with get_db(immediate=True) as db:
        w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not w or w["status"] != "approved":
            raise LedgerError("Only an approved withdrawal can be marked paid")
        cur, total = w["currency"], w["amount_cents"] + w["fee_cents"]
        legs = [S("withdrawals_held", cur, -total, "withdrawal", wd_id), S(CHANNELS[w["channel"]], cur, w["amount_cents"])]
        if w["fee_cents"]:
            legs.append(S("fee_revenue", cur, w["fee_cents"], "fee", "withdrawal"))
        txn = post(db, "withdrawal_paid", legs, memo=f"Withdrawal paid out ({paid_ref})", ref_type="withdrawal",
                   ref_id=wd_id, idempotency_key=f"wd:paid:{wd_id}", created_by=admin_id)
        db.execute("UPDATE withdrawals SET status='paid', paid_ref=?, final_txn_id=? WHERE id=?", (paid_ref, txn, wd_id))
        db.execute("""INSERT OR IGNORE INTO external_payments(id,channel,external_ref,direction,currency,amount_cents,status,
                      user_id,wallet_id,txn_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), w["channel"], paid_ref, "out", cur, w["amount_cents"], "paid", w["user_id"],
                    w["wallet_id"], txn, admin_id))
        return txn, w


def return_withdrawal(wd_id, admin_id, reason, status="rejected"):
    """Rejected by an admin, or the payout failed at the provider: everything goes back to the wallet."""
    if status not in ("rejected", "failed"):
        raise LedgerError("Bad status")
    with get_db(immediate=True) as db:
        w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not w or w["status"] not in ("requested", "approved"):
            raise LedgerError("This withdrawal can no longer be returned")
        cur, total = w["currency"], w["amount_cents"] + w["fee_cents"]
        label = "Withdrawal rejected" if status == "rejected" else "Withdrawal failed"
        txn = post(db, f"withdrawal_{status}",
                   [S("withdrawals_held", cur, -total, "withdrawal", wd_id),
                    W(w["wallet_id"], total, f"{label}: returned in full", "withdrawal_return")],
                   memo=f"{label}: {reason}", ref_type="withdrawal", ref_id=wd_id,
                   idempotency_key=f"wd:ret:{wd_id}", created_by=admin_id)
        db.execute("UPDATE withdrawals SET status=?, failure_reason=?, final_txn_id=?, decided_at=datetime('now') WHERE id=?",
                   (status, reason, txn, wd_id))
        return txn, w


# ── Manual adjustment (two different admins) ────────────────────────────────

def adjust(user_id, currency, cents, reason, admin_id, second_admin_id):
    if not reason or len(reason.strip()) < 5:
        raise LedgerError("Give a reason")
    if not second_admin_id or second_admin_id == admin_id:
        raise LedgerError("A second, different admin must confirm an adjustment")
    with get_db(immediate=True) as db:
        wid = wallet_of(db, user_id, currency, create=True)
        return post(db, "adjustment", [S("adjustments", currency, -cents), W(wid, cents, f"Correction: {reason}", "adjustment")],
                    memo=f"{reason} (by {admin_id[:8]}, confirmed {second_admin_id[:8]})", created_by=admin_id)


# ── Statements (what the bank / MoMo says we hold) ───────────────────────────

def record_statement(account_code, currency, balance_cents, as_of, admin_id, note=""):
    if account_code not in CASH_ACCOUNTS:
        raise LedgerError("Statements are for cash accounts")
    with get_db() as db:
        db.execute("""INSERT INTO statement_balances(id,account_code,currency,balance_cents,as_of,entered_by,note)
                      VALUES(?,?,?,?,?,?,?)""", (str(uuid.uuid4()), account_code, currency.upper(), int(balance_cents),
                                               as_of, admin_id, note))
