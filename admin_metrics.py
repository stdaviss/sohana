"""
Figures for the role dashboards (executive, operations, compliance, fraud, credit,
engineering, payments). Every number is a query on real tables; money comes from the
double-entry ledger, per currency, with EUR equivalents at today's rates clearly marked.
Where SOHANA does not record something yet, the value is None and the page says
"not tracked yet" instead of showing an invented figure.
"""
import os
from datetime import datetime, timedelta

import ledger
from database import DB_PATH, EXCHANGE_RATES, EXCHANGE_RATES_META, fetchall, fetchone

LARGE_TX_EUR = 100_000          # EUR 1,000 equivalent: shown for review
WALLET_WATCH = {"RWF": 5_000_000}  # FRW 50,000: holding threshold to confirm with BNR for the licence category


def _one(sql, p=(), default=0):
    try:
        r = fetchone(sql, p)
        if r is None:
            return default
        v = r[0]
        return default if v is None else v
    except Exception:
        return None


def _all(sql, p=()):
    try:
        return [dict(r) for r in fetchall(sql, p)]
    except Exception:
        return []


def eur(cents, cur):
    try:
        return ledger.to_eur(int(cents or 0), cur)
    except Exception:
        return 0


def _sum_eur(rows, amount="s", cur="currency"):
    return sum(eur(r[amount], r[cur]) for r in rows)


def since(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def daily_series(sql_by_day, days=30, params=()):
    """{date: value} for the last `days` days from a query returning (d, v) rows, zero-filled."""
    rows = _all(sql_by_day, params)
    got = {r["d"]: r["v"] for r in rows}
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.utcnow().date() - timedelta(days=i)).isoformat()
        out.append((d, got.get(d, 0) or 0))
    return out


# ── Money (shared) ──────────────────────────────────────────────────────────

def money_summary():
    curs = ledger.currencies_in_use()
    eqs = {c: ledger.equation(c) for c in curs}
    wallets_eur = sum(eur(e["owed"]["member_wallets"], c) for c, e in eqs.items())
    pots_eur = sum(eur(e["owed"]["circle_pots"] + e["owed"]["campaign_funds"] + e["owed"]["pool_funds"], c) for c, e in eqs.items())
    fees_all = _all("SELECT currency, SUM(amount_cents) s FROM ledger_entries WHERE account_code='fee_revenue' GROUP BY currency")
    fees_30 = _all("SELECT currency, SUM(amount_cents) s FROM ledger_entries WHERE account_code='fee_revenue' AND created_at>=? GROUP BY currency", (since(30),))
    real_in_30 = _all("""SELECT currency, -SUM(amount_cents) s FROM ledger_entries WHERE account_code IN ('cash_momo','cash_bank','cash_card')
                         AND amount_cents<0 AND created_at>=? GROUP BY currency""", (since(30),))
    sim_in_30 = _all("""SELECT currency, -SUM(amount_cents) s FROM ledger_entries WHERE account_code='simulated_funding'
                        AND created_at>=? GROUP BY currency""", (since(30),))
    moved = lambda d: _all("""SELECT w.currency, SUM(w.amount_cents) s FROM wallet_transactions w JOIN ledger_txns t ON t.id=w.txn_id
                              WHERE w.amount_cents>0 AND t.kind NOT IN ('opening','deposit_simulated','deposit_momo','deposit_bank',
                              'deposit_card','suspense_match','adjustment','withdrawal_rejected','withdrawal_failed')
                              AND t.created_at>=? GROUP BY w.currency""", (since(d),))
    checks = fetchone("SELECT * FROM ledger_check_runs ORDER BY run_at DESC LIMIT 1")
    return {
        "currencies": curs, "eqs": eqs,
        "wallets_eur": wallets_eur, "held_eur": pots_eur,
        "fees_all_eur": _sum_eur(fees_all), "fees_30_eur": _sum_eur(fees_30), "fees_30": fees_30,
        "real_in_30_eur": _sum_eur(real_in_30), "sim_in_30_eur": _sum_eur(sim_in_30),
        "moved_7_eur": _sum_eur(moved(7)), "moved_30_eur": _sum_eur(moved(30)),
        "withdrawals_open": _one("SELECT COUNT(*) FROM withdrawals WHERE status IN ('requested','approved')"),
        "withdrawals_open_eur": _sum_eur(_all("""SELECT currency, SUM(amount_cents) s FROM withdrawals
                                                 WHERE status IN ('requested','approved') GROUP BY currency""")),
        "suspense": _one("SELECT COUNT(*) FROM external_payments WHERE status='suspense'"),
        "balanced": all(e["trial_balance_sum"] == 0 for e in eqs.values()),
        "checks_passed": bool(checks["passed"]) if checks else None,
        "checks_at": checks["run_at"] if checks else None,
        "mode": ledger.money_mode(),
        "txns": _one("SELECT COUNT(*) FROM ledger_txns WHERE kind!='opening'"),
    }


def fee_series(days=30):
    rows = _all("""SELECT substr(created_at,1,10) d, currency, SUM(amount_cents) s FROM ledger_entries
                   WHERE account_code='fee_revenue' AND created_at>=? GROUP BY d, currency""", (since(days),))
    by = {}
    for r in rows:
        by[r["d"]] = by.get(r["d"], 0) + eur(r["s"], r["currency"])
    return [((datetime.utcnow().date() - timedelta(days=i)).isoformat(), by.get((datetime.utcnow().date() - timedelta(days=i)).isoformat(), 0))
            for i in range(days - 1, -1, -1)]


def volume_series(days=30):
    rows = _all("""SELECT substr(t.created_at,1,10) d, w.currency, SUM(w.amount_cents) s FROM wallet_transactions w
                   JOIN ledger_txns t ON t.id=w.txn_id WHERE w.amount_cents>0 AND t.kind!='opening' AND t.created_at>=?
                   GROUP BY d, w.currency""", (since(days),))
    by = {}
    for r in rows:
        by[r["d"]] = by.get(r["d"], 0) + eur(r["s"], r["currency"])
    return [((datetime.utcnow().date() - timedelta(days=i)).isoformat(), by.get((datetime.utcnow().date() - timedelta(days=i)).isoformat(), 0))
            for i in range(days - 1, -1, -1)]


# ── Members and circles ─────────────────────────────────────────────────────

def members():
    return {
        "total": _one("SELECT COUNT(*) FROM users WHERE is_admin=0"),
        "new_7": _one("SELECT COUNT(*) FROM users WHERE is_admin=0 AND created_at>=?", (since(7),)),
        "new_30": _one("SELECT COUNT(*) FROM users WHERE is_admin=0 AND created_at>=?", (since(30),)),
        "active_30": _one("""SELECT COUNT(DISTINCT wl.user_id) FROM wallet_transactions w JOIN wallets wl ON wl.id=w.wallet_id
                             JOIN ledger_txns t ON t.id=w.txn_id WHERE t.kind NOT IN ('opening','deposit_simulated')
                             AND t.created_at>=?""", (since(30),)),
        "suspended": _one("SELECT COUNT(*) FROM users WHERE is_admin=0 AND is_suspended=1"),
        "frozen": _one("SELECT COUNT(*) FROM users WHERE is_admin=0 AND (freeze_deposits=1 OR freeze_withdrawals=1)"),
        "by_country": _all("""SELECT COALESCE(NULLIF(country,''),'?') country, COUNT(*) n FROM users WHERE is_admin=0
                              GROUP BY country ORDER BY n DESC LIMIT 8"""),
        "signups": daily_series("""SELECT substr(created_at,1,10) d, COUNT(*) v FROM users WHERE is_admin=0 AND created_at>=?
                                   GROUP BY d""", 30, (since(30),)),
    }


def circles():
    c = {r["status"]: r["n"] for r in _all("SELECT status, COUNT(*) n FROM contributions GROUP BY status")}
    settled = c.get("paid", 0) + c.get("late", 0) + c.get("missed", 0)
    return {
        "total": _one("SELECT COUNT(*) FROM roscas"),
        "by_status": {r["status"]: r["n"] for r in _all("SELECT status, COUNT(*) n FROM roscas GROUP BY status")},
        "members": _one("SELECT COUNT(*) FROM rosca_members WHERE status='active'"),
        "contrib": c, "settled": settled,
        "on_time_pct": pct(c.get("paid", 0), settled), "late_pct": pct(c.get("late", 0), settled),
        "missed_pct": pct(c.get("missed", 0), settled),
        "cycles_done": _one("SELECT COUNT(*) FROM cycles WHERE status='completed'"),
        "cycles_open": _one("SELECT COUNT(*) FROM cycles WHERE status IN ('open','collecting')"),
        "overdue": _all("""SELECT r.id, r.name, r.currency, cy.cycle_number, cy.due_at,
                             (SELECT COUNT(*) FROM contributions c WHERE c.cycle_id=cy.id AND c.status='pending') AS pending
                           FROM cycles cy JOIN roscas r ON r.id=cy.rosca_id
                           WHERE cy.status IN ('open','collecting') AND cy.due_at < ? ORDER BY cy.due_at LIMIT 15""",
                        (datetime.utcnow().isoformat(),)),
        "payouts_30": _one("SELECT COUNT(*) FROM ledger_txns WHERE kind='circle_payout' AND created_at>=?", (since(30),)),
    }


# ── Compliance ──────────────────────────────────────────────────────────────

def compliance():
    kyc = {r["s"]: r["n"] for r in _all("SELECT COALESCE(kyc_status,'none') s, COUNT(*) n FROM users WHERE is_admin=0 GROUP BY s")}
    kyc_lvl = {r["l"]: r["n"] for r in _all("SELECT COALESCE(kyc_level,0) l, COUNT(*) n FROM users WHERE is_admin=0 GROUP BY l")}
    total = sum(kyc.values())
    verified = kyc.get("verified", 0) + kyc.get("approved", 0)
    large = []
    for r in _all("""SELECT w.id, w.currency, w.amount_cents, w.description, w.created_at, w.txn_id, u.full_name, u.id uid
                     FROM wallet_transactions w JOIN wallets wl ON wl.id=w.wallet_id JOIN users u ON u.id=wl.user_id
                     WHERE w.created_at>=? AND w.txn_id IS NOT NULL ORDER BY w.created_at DESC LIMIT 2000""", (since(30),)):
        if abs(eur(r["amount_cents"], r["currency"])) >= LARGE_TX_EUR:
            large.append(r)
    # many deposits in a day (possible structuring)
    burst = _all("""SELECT u.id, u.full_name, substr(t.created_at,1,10) d, COUNT(*) n FROM wallet_transactions w
                    JOIN ledger_txns t ON t.id=w.txn_id JOIN wallets wl ON wl.id=w.wallet_id JOIN users u ON u.id=wl.user_id
                    WHERE t.kind IN ('deposit_momo','deposit_bank','deposit_card','suspense_match') AND t.created_at>=?
                    GROUP BY u.id, d HAVING n>=5 ORDER BY n DESC LIMIT 20""", (since(30),))
    watch = []
    for cur, lim in WALLET_WATCH.items():
        for r in _all("""SELECT u.id, u.full_name, w.currency,
                            (SELECT balance_after FROM wallet_transactions t WHERE t.wallet_id=w.id ORDER BY created_at DESC, rowid DESC LIMIT 1) bal
                          FROM wallets w JOIN users u ON u.id=w.user_id WHERE w.currency=?""", (cur,)):
            if (r["bal"] or 0) > lim:
                watch.append(r)
    return {
        "kyc": kyc, "kyc_levels": kyc_lvl, "kyc_total": total, "kyc_verified": verified, "kyc_pct": pct(verified, total),
        "kyc_pending_review": _one("SELECT COUNT(*) FROM kyc_submissions WHERE status IN ('pending','submitted','in_review')"),
        "large": large[:25], "large_count": len(large), "burst": burst, "watch": watch, "watch_limits": WALLET_WATCH,
        "complaints_open": _one("SELECT COUNT(*) FROM complaints WHERE status NOT IN ('resolved','closed')"),
        "complaints_30": _one("SELECT COUNT(*) FROM complaints WHERE created_at>=?", (since(30),)),
        "freezes_30": _one("SELECT COUNT(*) FROM freeze_log WHERE created_at>=?", (since(30),)),
        "audit_30": _one("SELECT COUNT(*) FROM admin_audit_logs WHERE created_at>=?", (since(30),)),
        "adjust_30": _one("SELECT COUNT(*) FROM ledger_txns WHERE kind='adjustment' AND created_at>=?", (since(30),)),
        "unmatched": _one("SELECT COUNT(*) FROM external_payments WHERE status='suspense'"),
        "unmatched_old": _one("SELECT COUNT(*) FROM external_payments WHERE status='suspense' AND created_at<?", (since(3),)),
    }


# ── Fraud ───────────────────────────────────────────────────────────────────

def fraud():
    # money in then out quickly: a deposit followed by a withdrawal request within 24h
    quick = _all("""SELECT u.id, u.full_name, wd.currency, wd.amount_cents, wd.created_at
                    FROM withdrawals wd JOIN users u ON u.id=wd.user_id
                    WHERE wd.created_at>=? AND EXISTS (
                      SELECT 1 FROM wallet_transactions w JOIN ledger_txns t ON t.id=w.txn_id
                      WHERE w.wallet_id=wd.wallet_id AND w.amount_cents>0
                        AND t.kind IN ('deposit_momo','deposit_bank','deposit_card','suspense_match')
                        AND t.created_at BETWEEN datetime(wd.created_at,'-1 day') AND wd.created_at)
                    ORDER BY wd.created_at DESC LIMIT 20""", (since(30),))
    otp_fail = _all("""SELECT u.id, u.full_name, COUNT(*) n FROM payment_intents p JOIN users u ON u.id=p.sender_id
                       WHERE p.status='failed' AND p.created_at>=? GROUP BY u.id HAVING n>=2 ORDER BY n DESC LIMIT 20""", (since(7),))
    fan_in = _all("""SELECT u.id, u.full_name, COUNT(DISTINCT h.sender_id) senders, COUNT(*) n FROM hanatag_payments h
                     JOIN users u ON u.id=h.recipient_id WHERE h.created_at>=? GROUP BY u.id HAVING senders>=5
                     ORDER BY senders DESC LIMIT 20""", (since(7),))
    payout_then_miss = _all("""SELECT DISTINCT u.id, u.full_name, r.name circle FROM cycles cy
                               JOIN contributions c ON c.rosca_id=cy.rosca_id AND c.user_id=cy.recipient_id AND c.status='missed'
                               JOIN cycles c2 ON c2.id=c.cycle_id AND c2.cycle_number>cy.cycle_number
                               JOIN users u ON u.id=cy.recipient_id JOIN roscas r ON r.id=cy.rosca_id
                               WHERE cy.status='completed' LIMIT 20""")
    alerts = _all("SELECT * FROM fraud_alerts ORDER BY created_at DESC LIMIT 20")
    return {
        "alerts": alerts, "alerts_open": _one("SELECT COUNT(*) FROM fraud_alerts WHERE status='open'"),
        "alert_types": _all("SELECT alert_type, COUNT(*) n FROM fraud_alerts GROUP BY alert_type ORDER BY n DESC"),
        "quick": quick, "otp_fail": otp_fail, "fan_in": fan_in, "payout_then_miss": payout_then_miss,
        "duplicates_30": _one("SELECT COUNT(*) FROM external_payments WHERE status='duplicate' AND created_at>=?", (since(30),)),
        "chargebacks": _one("SELECT COUNT(*) FROM external_payments WHERE status='reversed'"),
        "chargeback_loss_eur": -_sum_eur(_all("SELECT currency, SUM(amount_cents) s FROM ledger_entries WHERE account_code='chargeback_loss' GROUP BY currency")),
        "rejected_wd_30": _one("SELECT COUNT(*) FROM withdrawals WHERE status='rejected' AND created_at>=?", (since(30),)),
        "risk": {r["r"]: r["n"] for r in _all("SELECT COALESCE(risk_level,'unrated') r, COUNT(*) n FROM users WHERE is_admin=0 GROUP BY r")},
        "low_ncs": _all("SELECT id, full_name, hanatag, ncs_score FROM users WHERE is_admin=0 AND ncs_score<450 ORDER BY ncs_score LIMIT 15"),
    }


# ── Credit (NCS and circle loans) ───────────────────────────────────────────

def credit():
    bands = [("Exemplary 750+", 750, 2000), ("Reliable 650–749", 650, 750), ("Developing 550–649", 550, 650),
             ("Building 450–549", 450, 550), ("Probation <450", 0, 450)]
    dist = [(l, _one("SELECT COUNT(*) FROM users WHERE is_admin=0 AND ncs_score>=? AND ncs_score<?", (lo, hi))) for l, lo, hi in bands]
    loans = _all("SELECT currency, status, COUNT(*) n, SUM(original_amount_cents) lent, SUM(repaid_cents) repaid FROM circle_loans GROUP BY currency, status")
    lent_eur = sum(eur(r["lent"], r["currency"]) for r in loans)
    repaid_eur = sum(eur(r["repaid"], r["currency"]) for r in loans)
    events = _all("""SELECT event_type, COUNT(*) n, SUM(delta) d FROM ncs_events WHERE recorded_at>=? GROUP BY event_type ORDER BY n DESC""",
                  (since(30),))
    movers = _all("""SELECT u.id, u.full_name, SUM(e.delta) d FROM ncs_events e JOIN users u ON u.id=e.user_id
                     WHERE e.recorded_at>=? GROUP BY u.id ORDER BY d DESC LIMIT 8""", (since(30),))
    fallers = _all("""SELECT u.id, u.full_name, SUM(e.delta) d FROM ncs_events e JOIN users u ON u.id=e.user_id
                      WHERE e.recorded_at>=? GROUP BY u.id HAVING d<0 ORDER BY d LIMIT 8""", (since(30),))
    capped = _one("SELECT COUNT(*) FROM ncs_events WHERE delta=0 AND recorded_at>=?", (since(30),))
    return {
        "dist": dist, "avg": _one("SELECT AVG(ncs_score) FROM users WHERE is_admin=0"),
        "loans": loans, "loans_n": sum(r["n"] for r in loans), "lent_eur": lent_eur, "repaid_eur": repaid_eur,
        "outstanding_eur": lent_eur - repaid_eur, "repaid_pct": pct(repaid_eur, lent_eur),
        "events": events, "movers": movers, "fallers": fallers, "capped": capped,
        "score_series": daily_series("""SELECT substr(recorded_at,1,10) d, ROUND(AVG(score_after)) v FROM ncs_events
                                        WHERE recorded_at>=? GROUP BY d""", 30, (since(30),)),
    }


# ── Engineering ─────────────────────────────────────────────────────────────

def engineering():
    comps = _all("SELECT * FROM service_components ORDER BY display_order")
    for c in comps:
        rows = _all("SELECT is_up, response_ms FROM status_checks_log WHERE component_id=? AND checked_at>=?", (c["id"], since(1)))
        c["checks_24h"] = len(rows)
        c["uptime_24h"] = pct(sum(1 for r in rows if r["is_up"]), len(rows))
        ms = [r["response_ms"] for r in rows if r["response_ms"] is not None]
        c["avg_ms"] = round(sum(ms) / len(ms)) if ms else None
        rows7 = _all("SELECT is_up FROM status_checks_log WHERE component_id=? AND checked_at>=?", (c["id"], since(7)))
        c["uptime_7d"] = pct(sum(1 for r in rows7 if r["is_up"]), len(rows7))
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        size = None
    counts = {t: _one(f"SELECT COUNT(*) FROM {t}") for t in
              ("users", "wallets", "wallet_transactions", "ledger_txns", "ledger_entries", "funding_lots", "notifications", "otp_requests")}
    return {
        "components": comps, "db_size": size, "counts": counts,
        "incidents": _all("SELECT * FROM status_incidents ORDER BY created_at DESC LIMIT 10"),
        "incidents_open": _one("SELECT COUNT(*) FROM status_incidents WHERE status NOT IN ('resolved','completed')"),
        "checks": _all("SELECT run_at, passed, run_by FROM ledger_check_runs ORDER BY run_at DESC LIMIT 10"),
        "rates_source": EXCHANGE_RATES_META.get("source"), "rates_at": EXCHANGE_RATES_META.get("updated_at"),
        "rates_error": EXCHANGE_RATES_META.get("error"), "rates": dict(EXCHANGE_RATES),
        "txn_series": daily_series("SELECT substr(created_at,1,10) d, COUNT(*) v FROM ledger_txns WHERE created_at>=? GROUP BY d",
                                   14, (since(14),)),
        "otp_7": _one("SELECT COUNT(*) FROM otp_requests WHERE created_at>=?", (since(7),)),
        "mode": ledger.money_mode(), "demo": os.environ.get("DEMO_MODE") == "1",
    }


# ── Payments ────────────────────────────────────────────────────────────────

def payments():
    by_channel = []
    for ch, acc in ledger.CHANNELS.items():
        rows = _all("""SELECT currency, -SUM(amount_cents) s, COUNT(*) n FROM ledger_entries WHERE account_code=? AND amount_cents<0
                       AND created_at>=? GROUP BY currency""", (acc, since(30)))
        by_channel.append((ch, sum(eur(r["s"], r["currency"]) for r in rows), sum(r["n"] for r in rows)))
    sim = _all("""SELECT currency, -SUM(amount_cents) s, COUNT(*) n FROM ledger_entries WHERE account_code='simulated_funding'
                  AND created_at>=? GROUP BY currency""", (since(30),))
    by_channel.append(("simulated", sum(eur(r["s"], r["currency"]) for r in sim), sum(r["n"] for r in sim)))
    kinds = _all("""SELECT t.kind, COUNT(*) n FROM ledger_txns t WHERE t.created_at>=? AND t.kind!='opening'
                    GROUP BY t.kind ORDER BY n DESC""", (since(30),))
    contrib = _all("""SELECT r.currency, c.status, COUNT(*) n, SUM(c.amount_cents) s FROM contributions c JOIN roscas r ON r.id=c.rosca_id
                      GROUP BY r.currency, c.status""")
    wd = _all("SELECT status, currency, COUNT(*) n, SUM(amount_cents) s FROM withdrawals GROUP BY status, currency")
    return {"by_channel": by_channel, "kinds": kinds, "contrib": contrib, "withdrawals": wd,
            "ext": {r["status"]: r["n"] for r in _all("SELECT status, COUNT(*) n FROM external_payments GROUP BY status")},
            "conversions_30": _all("""SELECT from_currency, to_currency, COUNT(*) n, SUM(from_amount) s FROM currency_conversions
                                      WHERE created_at>=? GROUP BY from_currency, to_currency ORDER BY n DESC""", (since(30),)),
            "transfers_30": _all("""SELECT currency, COUNT(*) n, SUM(amount_cents) s FROM hanatag_payments WHERE created_at>=?
                                    GROUP BY currency""", (since(30),))}
