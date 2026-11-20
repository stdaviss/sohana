"""
Finance & Accounting console (/admin/accounting/...).

Everything on these pages is read from the double-entry ledger (ledger.py): nothing is
typed in or estimated. Read access: CEO, CFO, Operations, Compliance, CCO.
Money actions (approve or pay withdrawals, match suspense, statements, adjustments,
the settlement simulator): CEO, CFO, Operations. Every action is written to the admin
audit log.
"""
import csv
import io
import json
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import Response, jsonify, redirect, render_template, request, session, url_for

import ledger
from database import CURRENCIES, EXCHANGE_RATES, fetchall, fetchone, get_db, push_notification

READ_ROLES = {"ceo", "cfo", "operations", "compliance", "cco"}
WRITE_ROLES = {"ceo", "cfo", "operations"}

KIND_LABELS = {
    "deposit_simulated": "Simulated deposit", "deposit_momo": "MoMo deposit", "deposit_bank": "Bank deposit",
    "deposit_card": "Card deposit", "deposit_momo_unmatched": "MoMo deposit (suspense)",
    "deposit_bank_unmatched": "Bank deposit (suspense)", "deposit_card_unmatched": "Card deposit (suspense)",
    "suspense_match": "Suspense matched", "suspense_refund": "Suspense returned", "chargeback": "Card chargeback",
    "transfer": "HanaPay transfer", "conversion": "Currency conversion",
    "circle_contribution": "Circle contribution", "circle_cover": "Covered contribution", "circle_payout": "Circle payout",
    "fee_circle_creation": "Circle creation fee", "fee_pool_creation": "Pool creation fee",
    "pool_contribution": "Pool contribution", "pool_disbursement": "Pool disbursement",
    "campaign_donation": "Donation", "campaign_release": "Campaign funds released",
    "loan_repayment": "Circle loan repayment", "withdrawal_request": "Withdrawal requested",
    "withdrawal_paid": "Withdrawal paid", "withdrawal_rejected": "Withdrawal rejected",
    "withdrawal_failed": "Withdrawal failed", "adjustment": "Adjustment", "opening": "Opening balances",
}
FEE_LABELS = {"conversion": "Currency conversion", "transfer": "Transfers", "withdrawal": "Withdrawals",
              "circle_payout": "Circle payouts (1.25%)", "circle_creation": "Circle creation", "pool_creation": "Pool creation",
              "campaign": "Donations (2.5%)"}


def _role():
    u = fetchone("SELECT is_admin, admin_role FROM users WHERE id=?", (session.get("user_id"),))
    return (u["admin_role"] or "") if u and u["is_admin"] else None


def _guard(write=False):
    def deco(f):
        @wraps(f)
        def inner(*a, **kw):
            if "user_id" not in session:
                return redirect("/admin/login")
            role = _role()
            allowed = WRITE_ROLES if write else READ_ROLES
            if role is None or role not in allowed:
                if request.path.startswith("/api/") or request.method == "POST":
                    return jsonify({"error": "Your admin role cannot do this"}), 403
                return render_template("admin_acct_denied.html", role=role), 403
            return f(*a, **kw)
        return inner
    return deco


def _money(cents, cur):
    return ledger.fmt_money(int(cents or 0), cur)


def _eur(cents, cur):
    try:
        return ledger.to_eur(int(cents or 0), cur)
    except Exception:
        return 0


def _period():
    """from/to (inclusive dates) from the query string; default: this month to date."""
    today = datetime.utcnow().date()
    try:
        f = datetime.strptime(request.args.get("from", ""), "%Y-%m-%d").date()
    except ValueError:
        f = today.replace(day=1)
    try:
        t = datetime.strptime(request.args.get("to", ""), "%Y-%m-%d").date()
    except ValueError:
        t = today
    return f.isoformat(), t.isoformat(), (t + timedelta(days=1)).isoformat()


def _csv(name, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for r in rows:
        w.writerow(["'" + str(c) if isinstance(c, str) and c[:1] in "=+-@" else c for c in r])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def _user_label(uid):
    u = fetchone("SELECT full_name, hanatag FROM users WHERE id=?", (uid,))
    return f"{u['full_name']} ({u['hanatag']})" if u else (uid or "")[:8]


def _txn_legs(txn_id):
    legs = []
    for r in fetchall("""SELECT wt.wallet_id, wt.currency, wt.amount_cents, wt.balance_after, wt.description, w.user_id,
                                u.full_name, u.hanatag
                         FROM wallet_transactions wt JOIN wallets w ON w.id=wt.wallet_id
                         LEFT JOIN users u ON u.id=w.user_id WHERE wt.txn_id=? ORDER BY wt.rowid""", (txn_id,)):
        legs.append({"side": "member", "account": f"{r['full_name'] or '?'} {r['hanatag'] or ''}", "user_id": r["user_id"],
                     "currency": r["currency"], "amount": r["amount_cents"], "balance_after": r["balance_after"],
                     "detail": r["description"]})
    for r in fetchall("SELECT * FROM ledger_entries WHERE txn_id=? ORDER BY rowid", (txn_id,)):
        legs.append({"side": "sohana", "account": ledger.SYSTEM_ACCOUNTS.get(r["account_code"], (r["account_code"],))[0],
                     "code": r["account_code"], "currency": r["currency"], "amount": r["amount_cents"],
                     "balance_after": r["balance_after"],
                     "detail": (f"{r['sub_ref_type']}: {r['sub_ref_id'][:8] if r['sub_ref_id'] and len(r['sub_ref_id']) > 20 else r['sub_ref_id']}"
                                if r["sub_ref_id"] else "")})
    return legs


def install(app, log_admin_action):

    @app.context_processor
    def _acct_ctx():
        return {"acct_money": _money, "acct_kind": lambda k: KIND_LABELS.get(k, (k or "").replace("_", " ").capitalize()),
                "acct_can_write": lambda: _role() in WRITE_ROLES}

    # ── Overview ──────────────────────────────────────────────────────────────
    @app.route("/admin/accounting")
    @_guard()
    def acct_overview():
        curs = ledger.currencies_in_use() or ["EUR"]
        eqs = [ledger.equation(c) for c in curs]
        f, t, t_excl = _period()
        flows = {}
        for c in curs:
            cash_in = fetchone("""SELECT COALESCE(-SUM(amount_cents),0) s FROM ledger_entries
                                  WHERE account_code IN ('cash_momo','cash_bank','cash_card') AND currency=? AND amount_cents<0
                                  AND created_at>=? AND created_at<?""", (c, f, t_excl))["s"]
            cash_out = fetchone("""SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries
                                   WHERE account_code IN ('cash_momo','cash_bank','cash_card') AND currency=? AND amount_cents>0
                                   AND created_at>=? AND created_at<?""", (c, f, t_excl))["s"]
            sim_in = fetchone("""SELECT COALESCE(-SUM(amount_cents),0) s FROM ledger_entries WHERE account_code='simulated_funding'
                                 AND currency=? AND created_at>=? AND created_at<?""", (c, f, t_excl))["s"]
            fees = fetchone("""SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE account_code='fee_revenue'
                               AND currency=? AND created_at>=? AND created_at<?""", (c, f, t_excl))["s"]
            internal = fetchone("""SELECT COALESCE(SUM(wt.amount_cents),0) s FROM wallet_transactions wt
                                   JOIN ledger_txns t ON t.id=wt.txn_id
                                   WHERE wt.currency=? AND wt.amount_cents>0 AND t.kind IN ('transfer','loan_repayment')
                                   AND t.created_at>=? AND t.created_at<?""", (c, f, t_excl))["s"]
            flows[c] = {"cash_in": cash_in, "cash_out": cash_out, "sim_in": sim_in, "fees": fees, "internal": internal}
        last = fetchone("SELECT * FROM ledger_check_runs ORDER BY run_at DESC LIMIT 1")
        last_results = json.loads(last["results"]) if last else []
        queue = {
            "withdrawals": fetchone("SELECT COUNT(*) c FROM withdrawals WHERE status IN ('requested','approved')")["c"],
            "suspense": fetchone("SELECT COUNT(*) c FROM external_payments WHERE status='suspense'")["c"],
            "adjust": fetchone("SELECT COUNT(*) c FROM ledger_adjustment_requests WHERE status='proposed'")["c"],
        }
        txns = fetchone("SELECT COUNT(*) c FROM ledger_txns")["c"]
        recent = fetchall("SELECT * FROM ledger_txns ORDER BY seq DESC LIMIT 12")
        return render_template("admin_acct_overview.html", tab="overview", eqs=eqs, flows=flows, f=f, t=t,
                               last=last, last_results=last_results, queue=queue, txns=txns, recent=recent,
                               mode=ledger.money_mode(), rates=EXCHANGE_RATES)

    @app.route("/admin/accounting/checks/run", methods=["POST"])
    @_guard()
    def acct_run_checks():
        passed, res = ledger.run_checks(run_by=session["user_id"])
        log_admin_action("ledger_checks_run", "ledger", None, new_data={"passed": passed})
        return jsonify({"ok": True, "passed": passed, "results": res})

    @app.route("/admin/accounting/checks")
    @_guard()
    def acct_checks():
        runs = fetchall("SELECT * FROM ledger_check_runs ORDER BY run_at DESC LIMIT 60")
        runs = [dict(r, results=json.loads(r["results"])) for r in runs]
        return render_template("admin_acct_checks.html", tab="checks", runs=runs)

    # ── Trial balance and account drill-down ────────────────────────────────
    @app.route("/admin/accounting/trial-balance")
    @_guard()
    def acct_trial_balance():
        tb = ledger.trial_balance()
        by_cur = {}
        for r in tb:
            by_cur.setdefault(r["currency"], []).append(r)
        order = {"cash": 0, "liability": 1, "holding": 2, "revenue": 3, "treasury": 4, "equity": 5}
        for c in by_cur:
            by_cur[c].sort(key=lambda r: (order.get(r["group"], 9), r["code"]))
        if request.args.get("format") == "csv":
            return _csv("sohana-trial-balance.csv", ["Currency", "Account", "Code", "Group", "Balance", "EUR equivalent"],
                        [(r["currency"], r["label"], r["code"], r["group"], r["balance"] / 100, _eur(r["balance"], r["currency"]) / 100)
                         for c in sorted(by_cur) for r in by_cur[c]])
        return render_template("admin_acct_tb.html", tab="tb", by_cur=by_cur,
                               sums={c: sum(r["balance"] for r in rows) for c, rows in by_cur.items()})

    @app.route("/admin/accounting/account/<code>")
    @_guard()
    def acct_account(code):
        cur = (request.args.get("currency") or "RWF").upper()
        sub = request.args.get("sub") or None
        if code == "member_wallets":
            rows = fetchall("""SELECT w.id, w.user_id, u.full_name, u.hanatag,
                                 (SELECT balance_after FROM wallet_transactions t WHERE t.wallet_id=w.id
                                  ORDER BY created_at DESC, rowid DESC LIMIT 1) AS bal
                               FROM wallets w LEFT JOIN users u ON u.id=w.user_id WHERE w.currency=? ORDER BY bal DESC""", (cur,))
            return render_template("admin_acct_wallets.html", tab="tb", rows=rows, cur=cur)
        if code not in ledger.SYSTEM_ACCOUNTS:
            return redirect("/admin/accounting/trial-balance")
        subs = fetchall("""SELECT sub_ref_type, sub_ref_id, SUM(amount_cents) s, COUNT(*) n FROM ledger_entries
                           WHERE account_code=? AND currency=? AND sub_ref_id IS NOT NULL
                           GROUP BY sub_ref_type, sub_ref_id ORDER BY s DESC LIMIT 200""", (code, cur))
        q = """SELECT e.*, t.kind, t.memo, t.seq FROM ledger_entries e JOIN ledger_txns t ON t.id=e.txn_id
               WHERE e.account_code=? AND e.currency=?"""
        params = [code, cur]
        if sub:
            q += " AND e.sub_ref_id=?"
            params.append(sub)
        q += " ORDER BY e.rowid DESC LIMIT 500"
        entries = fetchall(q, params)
        if request.args.get("format") == "csv":
            return _csv(f"sohana-{code}-{cur}.csv", ["Date", "Txn #", "Kind", "Memo", "Sub-account", "Amount", "Balance after"],
                        [(e["created_at"], e["seq"], e["kind"], e["memo"], e["sub_ref_id"] or "", e["amount_cents"] / 100,
                          e["balance_after"] / 100) for e in entries])
        return render_template("admin_acct_account.html", tab="tb", code=code, cur=cur, sub=sub, subs=subs,
                               entries=entries, label=ledger.SYSTEM_ACCOUNTS[code][0],
                               balance=ledger.system_balance(code, cur, sub) if sub else ledger.system_balance(code, cur))

    # ── Transaction explorer ────────────────────────────────────────────────
    def _txn_query():
        f, t, t_excl = _period()
        where, params = ["t.created_at>=?", "t.created_at<?"], [f, t_excl]
        kind = request.args.get("kind") or ""
        if kind:
            where.append("t.kind=?")
            params.append(kind)
        cur = (request.args.get("currency") or "").upper()
        if cur:
            where.append("""(EXISTS(SELECT 1 FROM ledger_entries e WHERE e.txn_id=t.id AND e.currency=?)
                             OR EXISTS(SELECT 1 FROM wallet_transactions w WHERE w.txn_id=t.id AND w.currency=?))""")
            params += [cur, cur]
        member = (request.args.get("member") or "").strip()
        if member:
            where.append("""EXISTS(SELECT 1 FROM wallet_transactions w JOIN wallets wl ON wl.id=w.wallet_id
                                   JOIN users u ON u.id=wl.user_id WHERE w.txn_id=t.id AND
                                   (u.hanatag LIKE ? OR u.full_name LIKE ? OR u.email LIKE ? OR u.phone LIKE ?))""")
            params += [f"%{member}%"] * 4
        qtext = (request.args.get("q") or "").strip()
        if qtext:
            where.append("(t.id LIKE ? OR t.memo LIKE ? OR t.ref_id LIKE ? OR CAST(t.seq AS TEXT)=?)")
            params += [f"{qtext}%", f"%{qtext}%", f"{qtext}%", qtext]
        return f, t, " AND ".join(where), params, kind, cur, member, qtext

    def _txn_summary(rows):
        out = []
        for r in rows:
            amts = fetchall("""SELECT currency, SUM(CASE WHEN a>0 THEN a ELSE 0 END) s FROM (
                                 SELECT currency, amount_cents a FROM wallet_transactions WHERE txn_id=?
                                 UNION ALL SELECT currency, amount_cents a FROM ledger_entries WHERE txn_id=?)
                               GROUP BY currency""", (r["id"], r["id"]))
            members = fetchall("""SELECT DISTINCT u.full_name FROM wallet_transactions w JOIN wallets wl ON wl.id=w.wallet_id
                                  JOIN users u ON u.id=wl.user_id WHERE w.txn_id=? LIMIT 3""", (r["id"],))
            out.append(dict(r, amounts=[(a["currency"], a["s"]) for a in amts], members=", ".join(m["full_name"] for m in members)))
        return out

    @app.route("/admin/accounting/transactions")
    @_guard()
    def acct_transactions():
        f, t, where, params, kind, cur, member, qtext = _txn_query()
        page = max(1, int(request.args.get("page", 1) or 1))
        total = fetchone(f"SELECT COUNT(*) c FROM ledger_txns t WHERE {where}", params)["c"]
        if request.args.get("format") == "csv":
            rows = fetchall(f"SELECT t.* FROM ledger_txns t WHERE {where} ORDER BY t.seq", params)
            lines = []
            for r in rows:
                for l in _txn_legs(r["id"]):
                    lines.append((r["seq"], r["created_at"], r["kind"], r["memo"], r["id"], l["side"], l["account"].strip(),
                                  l["currency"], l["amount"] / 100, l.get("detail") or ""))
            return _csv(f"sohana-transactions-{f}-to-{t}.csv",
                        ["Txn #", "Date (UTC)", "Kind", "Memo", "Txn id", "Side", "Account", "Currency", "Amount", "Detail"], lines)
        rows = fetchall(f"SELECT t.* FROM ledger_txns t WHERE {where} ORDER BY t.seq DESC LIMIT 100 OFFSET ?",
                        params + [(page - 1) * 100])
        kinds = [r["kind"] for r in fetchall("SELECT DISTINCT kind FROM ledger_txns ORDER BY kind")]
        return render_template("admin_acct_txns.html", tab="txns", rows=_txn_summary(rows), total=total, page=page,
                               f=f, t=t, kind=kind, cur=cur, member=member, q=qtext, kinds=kinds,
                               currencies=ledger.currencies_in_use())

    @app.route("/admin/accounting/txn/<txn_id>")
    @_guard()
    def acct_txn(txn_id):
        t = fetchone("SELECT * FROM ledger_txns WHERE id=? OR CAST(seq AS TEXT)=?", (txn_id, txn_id))
        if not t:
            return redirect("/admin/accounting/transactions")
        legs = _txn_legs(t["id"])
        sources = ledger.sources_of_txn(t["id"])
        for s in sources:
            s["holder_label"] = _holder_label(s["holder_kind"], s["holder_id"])
        totals = {}
        for l in legs:
            totals[l["currency"]] = totals.get(l["currency"], 0) + l["amount"]
        creator = _user_label(t["created_by"]) if t["created_by"] and len(t["created_by"]) > 20 else (t["created_by"] or "system")
        return render_template("admin_acct_txn.html", tab="txns", t=t, legs=legs, sources=sources, totals=totals,
                               creator=creator)

    def _holder_label(kind, hid):
        if kind == "wallet":
            w = fetchone("SELECT u.full_name, w.currency FROM wallets w JOIN users u ON u.id=w.user_id WHERE w.id=?", (hid,))
            return f"{w['full_name']} · {w['currency']} wallet" if w else "wallet"
        return ledger.SYSTEM_ACCOUNTS.get(kind, (kind,))[0] + (f" · {hid[:8]}" if hid and len(hid) > 20 else (f" · {hid}" if hid else ""))

    @app.route("/admin/accounting/trace/<lot_id>")
    @_guard()
    def acct_trace(lot_id):
        chain = ledger.trace_lot(lot_id)
        for c in chain:
            c["holder_label"] = _holder_label(c["holder_kind"], c["holder_id"])
            tx = fetchone("SELECT seq, kind, memo, created_at FROM ledger_txns WHERE id=?", (c["txn_id"],))
            c["txn"] = dict(tx) if tx else {}
        root = chain[-1] if chain else None
        ext = None
        if root and root.get("root_ref"):
            ext = fetchone("SELECT * FROM external_payments WHERE external_ref=? AND direction='in'", (root["root_ref"],))
        return render_template("admin_acct_trace.html", tab="txns", chain=chain, root=root, ext=ext)

    # ── Member statement ────────────────────────────────────────────────────
    @app.route("/admin/accounting/member")
    @app.route("/admin/accounting/member/<user_id>")
    @_guard()
    def acct_member(user_id=None):
        search = (request.args.get("q") or "").strip()
        found = []
        if not user_id and search:
            found = fetchall("""SELECT id, full_name, hanatag, email, phone FROM users WHERE full_name LIKE ? OR hanatag LIKE ?
                                OR email LIKE ? OR phone LIKE ? OR deposit_ref=? LIMIT 30""",
                             (f"%{search}%",) * 4 + (search.upper(),))
            if len(found) == 1:
                return redirect(f"/admin/accounting/member/{found[0]['id']}")
        if not user_id:
            return render_template("admin_acct_member.html", tab="member", u=None, found=found, q=search)
        u = fetchone("SELECT * FROM users WHERE id=?", (user_id,))
        if not u:
            return redirect("/admin/accounting/member")
        f, t, t_excl = _period()
        wallets = []
        for w in fetchall("SELECT * FROM wallets WHERE user_id=? ORDER BY is_default DESC, currency", (user_id,)):
            open_row = fetchone("""SELECT balance_after FROM wallet_transactions WHERE wallet_id=? AND created_at<?
                                   ORDER BY created_at DESC, rowid DESC LIMIT 1""", (w["id"], f))
            rows = fetchall("""SELECT wt.*, t.kind, t.seq FROM wallet_transactions wt LEFT JOIN ledger_txns t ON t.id=wt.txn_id
                               WHERE wt.wallet_id=? AND wt.created_at>=? AND wt.created_at<?
                               ORDER BY wt.created_at, wt.rowid""", (w["id"], f, t_excl))
            opening = open_row["balance_after"] if open_row else 0
            closing = rows[-1]["balance_after"] if rows else opening
            with get_db() as db:
                held = ledger.card_held_cents(db, w["id"], w["currency"])
            lots = fetchall("""SELECT root_kind, SUM(remaining_cents) s FROM funding_lots WHERE holder_kind='wallet' AND holder_id=?
                               AND remaining_cents>0 GROUP BY root_kind""", (w["id"],))
            wallets.append({"w": w, "rows": rows, "opening": opening, "closing": closing,
                            "ins": sum(r["amount_cents"] for r in rows if r["amount_cents"] > 0),
                            "outs": -sum(r["amount_cents"] for r in rows if r["amount_cents"] < 0),
                            "held": held, "origins": lots})
        if request.args.get("format") == "csv":
            lines = []
            for wb in wallets:
                cur = wb["w"]["currency"]
                lines.append((cur, f, "", "Opening balance", "", "", wb["opening"] / 100))
                for r in wb["rows"]:
                    lines.append((cur, r["created_at"], r["seq"] or "", r["description"], r["kind"] or r["tx_type"],
                                  r["amount_cents"] / 100, r["balance_after"] / 100))
                lines.append((cur, t, "", "Closing balance", "", "", wb["closing"] / 100))
            return _csv(f"sohana-statement-{u['hanatag'] or user_id[:8]}-{f}-to-{t}.csv",
                        ["Currency", "Date (UTC)", "Txn #", "Description", "Type", "Amount", "Balance"], lines)
        withdrawals = fetchall("SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (user_id,))
        externals = fetchall("SELECT * FROM external_payments WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (user_id,))
        loans = fetchall("""SELECT cl.*, r.name AS circle FROM circle_loans cl LEFT JOIN roscas r ON r.id=cl.circle_id
                            WHERE cl.lender_id=? OR cl.borrower_id=? ORDER BY cl.created_at DESC""", (user_id, user_id))
        return render_template("admin_acct_member.html", tab="member", u=u, wallets=wallets, f=f, t=t,
                               withdrawals=withdrawals, externals=externals, loans=loans,
                               deposit_ref=ledger.deposit_ref_for(user_id), found=[], q="")

    # ── Circle, pool and campaign audits ────────────────────────────────────
    @app.route("/admin/accounting/circles")
    @_guard()
    def acct_circles():
        rows = fetchall("""SELECT r.id, r.name, r.currency, r.status, r.contribution_cents,
                             (SELECT COUNT(*) FROM rosca_members m WHERE m.rosca_id=r.id AND m.status='active') AS members,
                             (SELECT COUNT(*) FROM cycles c WHERE c.rosca_id=r.id AND c.status='completed') AS done,
                             (SELECT COALESCE(SUM(e.amount_cents),0) FROM ledger_entries e JOIN cycles c ON c.id=e.sub_ref_id
                              WHERE e.account_code='circle_pots' AND c.rosca_id=r.id) AS held
                           FROM roscas r ORDER BY r.created_at DESC LIMIT 300""")
        camps = fetchall("""SELECT c.id, c.title, c.currency, c.status, c.raised_cents, c.withdrawn_cents, c.goal_cents,
                              (SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries e WHERE e.account_code='campaign_funds'
                               AND e.sub_ref_id=c.id) AS held
                            FROM campaigns c ORDER BY c.created_at DESC LIMIT 300""")
        pools = fetchall("""SELECT p.id, p.name, p.currency, p.status, p.total_balance_cents,
                              (SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries e WHERE e.account_code='pool_funds'
                               AND e.sub_ref_id=p.id) AS held
                            FROM pools p ORDER BY p.created_at DESC LIMIT 300""")
        return render_template("admin_acct_circles.html", tab="circles", rows=rows, camps=camps, pools=pools)

    @app.route("/admin/accounting/circle/<rosca_id>")
    @_guard()
    def acct_circle(rosca_id):
        r = fetchone("SELECT * FROM roscas WHERE id=?", (rosca_id,))
        if not r:
            return redirect("/admin/accounting/circles")
        cur = r["currency"] or "EUR"
        cycles = []
        for c in fetchall("""SELECT cy.*, u.full_name AS recipient FROM cycles cy LEFT JOIN users u ON u.id=cy.recipient_id
                             WHERE cy.rosca_id=? ORDER BY cy.cycle_number""", (rosca_id,)):
            contribs = fetchall("""SELECT co.*, u.full_name, pb.full_name AS payer FROM contributions co
                                   LEFT JOIN users u ON u.id=co.user_id LEFT JOIN users pb ON pb.id=co.paid_by
                                   WHERE co.cycle_id=? ORDER BY u.full_name""", (c["id"],))
            paid = sum(x["amount_cents"] for x in contribs if x["status"] in ("paid", "late"))
            pot_in = fetchone("""SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE account_code='circle_pots'
                                 AND sub_ref_id=? AND amount_cents>0""", (c["id"],))["s"]
            pot_out = -fetchone("""SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE account_code='circle_pots'
                                   AND sub_ref_id=? AND amount_cents<0""", (c["id"],))["s"]
            payout_txn = fetchone("SELECT id, seq FROM ledger_txns WHERE kind='circle_payout' AND ref_id=?", (c["id"],))
            fee = 0
            net = 0
            if payout_txn:
                fee = fetchone("SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE txn_id=? AND account_code='fee_revenue'",
                               (payout_txn["id"],))["s"]
                net = fetchone("SELECT COALESCE(SUM(amount_cents),0) s FROM wallet_transactions WHERE txn_id=? AND amount_cents>0",
                               (payout_txn["id"],))["s"]
            ok = (pot_in - pot_out == (paid if c["status"] in ("open", "collecting") else 0)) and \
                 (c["status"] in ("open", "collecting") or pot_in == paid)
            cycles.append({"c": c, "contribs": contribs, "paid": paid, "pot_in": pot_in, "pot_out": pot_out,
                           "fee": fee, "net": net, "payout_txn": payout_txn, "ok": ok})
        loans = fetchall("""SELECT cl.*, lu.full_name AS lender, bu.full_name AS borrower FROM circle_loans cl
                            JOIN users lu ON lu.id=cl.lender_id JOIN users bu ON bu.id=cl.borrower_id
                            WHERE cl.circle_id=? ORDER BY cl.created_at""", (rosca_id,))
        creation = fetchone("SELECT id, seq FROM ledger_txns WHERE kind='fee_circle_creation' AND ref_id=?", (rosca_id,))
        return render_template("admin_acct_circle.html", tab="circles", r=r, cur=cur, cycles=cycles, loans=loans,
                               creation=creation)

    @app.route("/admin/accounting/cycle/<cycle_id>")
    @_guard()
    def acct_cycle(cycle_id):
        c = fetchone("SELECT rosca_id FROM cycles WHERE id=?", (cycle_id,))
        return redirect(f"/admin/accounting/circle/{c['rosca_id']}#cy-{cycle_id}" if c else "/admin/accounting/circles")

    @app.route("/admin/accounting/campaign/<cid>")
    @_guard()
    def acct_campaign(cid):
        c = fetchone("SELECT c.*, u.full_name AS creator FROM campaigns c JOIN users u ON u.id=c.creator_id WHERE c.id=?", (cid,))
        if not c:
            return redirect("/admin/accounting/circles")
        cur = c["currency"] or "EUR"
        entries = fetchall("""SELECT e.*, t.kind, t.memo, t.seq, t.id AS tid FROM ledger_entries e JOIN ledger_txns t ON t.id=e.txn_id
                              WHERE e.account_code='campaign_funds' AND e.sub_ref_id=? ORDER BY e.rowid""", (cid,))
        fees = fetchone("""SELECT COALESCE(SUM(e2.amount_cents),0) s FROM ledger_entries e JOIN ledger_entries e2 ON e2.txn_id=e.txn_id
                           WHERE e.account_code='campaign_funds' AND e.sub_ref_id=? AND e2.account_code='fee_revenue'""", (cid,))["s"]
        donations = fetchall("SELECT * FROM campaign_donations WHERE campaign_id=? ORDER BY created_at DESC LIMIT 200", (cid,))
        return render_template("admin_acct_campaign.html", tab="circles", c=c, cur=cur, entries=entries, fees=fees,
                               donations=donations, held=sum(e["amount_cents"] for e in entries))

    # ── Reports ──────────────────────────────────────────────────────────────
    def _report(f, t_excl):
        curs = ledger.currencies_in_use()
        rep = {}
        for c in curs:
            def s(sql, *p):
                return fetchone(sql, p)["s"] or 0
            by_channel = {ch: s("""SELECT COALESCE(-SUM(amount_cents),0) s FROM ledger_entries WHERE account_code=? AND currency=?
                                   AND amount_cents<0 AND created_at>=? AND created_at<?""", acc, c, f, t_excl)
                          for ch, acc in ledger.CHANNELS.items()}
            by_channel["simulated"] = s("""SELECT COALESCE(-SUM(amount_cents),0) s FROM ledger_entries WHERE account_code='simulated_funding'
                                          AND currency=? AND created_at>=? AND created_at<?""", c, f, t_excl)
            paid_out = s("""SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE account_code IN ('cash_momo','cash_bank')
                            AND currency=? AND amount_cents>0 AND created_at>=? AND created_at<?""", c, f, t_excl)
            fees = {r["sub_ref_id"] or "other": r["s"] for r in fetchall(
                """SELECT sub_ref_id, SUM(amount_cents) s FROM ledger_entries WHERE account_code='fee_revenue' AND currency=?
                   AND created_at>=? AND created_at<? GROUP BY sub_ref_id""", (c, f, t_excl))}
            kinds = {r["kind"]: (r["n"], r["s"]) for r in fetchall(
                """SELECT t.kind, COUNT(DISTINCT t.id) n, COALESCE(SUM(CASE WHEN x.a>0 THEN x.a ELSE 0 END),0) s
                   FROM ledger_txns t JOIN (SELECT txn_id, amount_cents a, currency FROM wallet_transactions
                                            UNION ALL SELECT txn_id, amount_cents a, currency FROM ledger_entries) x
                     ON x.txn_id=t.id AND x.currency=?
                   WHERE t.created_at>=? AND t.created_at<? AND t.kind!='opening' GROUP BY t.kind ORDER BY s DESC""", (c, f, t_excl))}
            members = s("""SELECT COUNT(DISTINCT wl.user_id) s FROM wallet_transactions w JOIN wallets wl ON wl.id=w.wallet_id
                           WHERE w.currency=? AND w.created_at>=? AND w.created_at<? AND w.txn_id IN
                           (SELECT id FROM ledger_txns WHERE kind!='opening')""", c, f, t_excl)
            rep[c] = {"in": by_channel, "in_total": sum(by_channel.values()), "paid_out": paid_out, "fees": fees,
                      "fees_total": sum(fees.values()), "kinds": kinds, "members": members}
        return rep

    @app.route("/admin/accounting/reports")
    @_guard()
    def acct_reports():
        f, t, t_excl = _period()
        rep = _report(f, t_excl)
        months = []
        first = fetchone("SELECT MIN(created_at) m FROM ledger_txns")["m"]
        if first:
            d = datetime.strptime(first[:7] + "-01", "%Y-%m-%d").date()
            end = datetime.utcnow().date()
            while d <= end and len(months) < 24:
                nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
                fee_eur = sum(_eur(r["s"], r["currency"]) for r in fetchall(
                    """SELECT currency, SUM(amount_cents) s FROM ledger_entries WHERE account_code='fee_revenue'
                       AND created_at>=? AND created_at<? GROUP BY currency""", (d.isoformat(), nxt.isoformat())))
                in_eur = sum(_eur(r["s"], r["currency"]) for r in fetchall(
                    """SELECT currency, -SUM(amount_cents) s FROM ledger_entries WHERE account_code IN
                       ('cash_momo','cash_bank','cash_card','simulated_funding') AND amount_cents<0
                       AND created_at>=? AND created_at<? GROUP BY currency""", (d.isoformat(), nxt.isoformat())))
                vol_eur = sum(_eur(r["s"], r["currency"]) for r in fetchall(
                    """SELECT w.currency, SUM(w.amount_cents) s FROM wallet_transactions w JOIN ledger_txns t ON t.id=w.txn_id
                       WHERE w.amount_cents>0 AND t.kind!='opening' AND t.created_at>=? AND t.created_at<? GROUP BY w.currency""",
                    (d.isoformat(), nxt.isoformat())))
                n = fetchone("SELECT COUNT(*) c FROM ledger_txns WHERE kind!='opening' AND created_at>=? AND created_at<?",
                             (d.isoformat(), nxt.isoformat()))["c"]
                months.append({"m": d.strftime("%b %Y"), "fees": fee_eur, "in": in_eur, "vol": vol_eur, "n": n})
                d = nxt
        if request.args.get("format") == "csv":
            lines = []
            for c, r in rep.items():
                for ch, v in r["in"].items():
                    lines.append((c, "Money in", ch, v / 100))
                lines.append((c, "Money paid out", "momo+bank", r["paid_out"] / 100))
                for k, v in r["fees"].items():
                    lines.append((c, "Fee revenue", FEE_LABELS.get(k, k), v / 100))
                for k, (n, v) in r["kinds"].items():
                    lines.append((c, "Activity", KIND_LABELS.get(k, k) + f" ({n})", v / 100))
            return _csv(f"sohana-report-{f}-to-{t}.csv", ["Currency", "Section", "Line", "Amount"], lines)
        return render_template("admin_acct_reports.html", tab="reports", rep=rep, f=f, t=t, months=months,
                               fee_labels=FEE_LABELS, eur=_eur)

    # ── Withdrawals queue ───────────────────────────────────────────────────
    @app.route("/admin/accounting/withdrawals")
    @_guard()
    def acct_withdrawals():
        status = request.args.get("status") or "open"
        q = """SELECT w.*, u.full_name, u.hanatag, a1.full_name AS ap1, a2.full_name AS ap2 FROM withdrawals w
               JOIN users u ON u.id=w.user_id LEFT JOIN users a1 ON a1.id=w.approved_by LEFT JOIN users a2 ON a2.id=w.approved_by_2"""
        if status == "open":
            rows = fetchall(q + " WHERE w.status IN ('requested','approved') ORDER BY w.created_at", ())
        else:
            rows = fetchall(q + " WHERE w.status=? ORDER BY w.created_at DESC LIMIT 200", (status,))
        return render_template("admin_acct_withdrawals.html", tab="withdrawals", rows=rows, status=status,
                               me=session["user_id"], second=ledger.WITHDRAW_SECOND_APPROVAL_EUR)

    @app.route("/admin/accounting/withdrawals/<wd_id>/<action>", methods=["POST"])
    @_guard(write=True)
    def acct_withdrawal_action(wd_id, action):
        d = request.get_json(silent=True) or {}
        me = session["user_id"]
        try:
            if action == "approve":
                st = ledger.approve_withdrawal(wd_id, me)
                log_admin_action("withdrawal_approved", "withdrawal", wd_id, new_data={"result": st})
                return jsonify({"ok": True, "status": st})
            if action == "paid":
                ref = (d.get("paid_ref") or "").strip()
                if len(ref) < 4:
                    return jsonify({"error": "Enter the MoMo / bank reference of the payout"}), 400
                txn, w = ledger.mark_withdrawal_paid(wd_id, ref, me)
                log_admin_action("withdrawal_paid", "withdrawal", wd_id, new_data={"paid_ref": ref, "txn": txn})
                push_notification(w["user_id"], "Withdrawal paid",
                                  f"{ledger.fmt_money(w['amount_cents'], w['currency'])} was sent to {w['destination']}. Reference {ref}.",
                                  "success", "/wallet")
                return jsonify({"ok": True})
            if action in ("reject", "failed"):
                reason = (d.get("reason") or "").strip()
                if len(reason) < 4:
                    return jsonify({"error": "Give a reason"}), 400
                txn, w = ledger.return_withdrawal(wd_id, me, reason, "rejected" if action == "reject" else "failed")
                log_admin_action(f"withdrawal_{action}", "withdrawal", wd_id, new_data={"reason": reason, "txn": txn},
                                 reason=reason)
                push_notification(w["user_id"], "Withdrawal returned",
                                  f"Your withdrawal of {ledger.fmt_money(w['amount_cents'], w['currency'])} was not paid "
                                  f"({reason}). The full amount and fee are back in your balance.", "warning", "/wallet")
                return jsonify({"ok": True})
        except ledger.LedgerError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"error": "Unknown action"}), 400

    # ── Suspense and external payments ──────────────────────────────────────
    @app.route("/admin/accounting/payments")
    @_guard()
    def acct_payments():
        status = request.args.get("status") or "suspense"
        if status == "all":
            rows = fetchall("""SELECT e.*, u.full_name, u.hanatag FROM external_payments e LEFT JOIN users u ON u.id=e.user_id
                               ORDER BY e.created_at DESC LIMIT 300""")
        else:
            rows = fetchall("""SELECT e.*, u.full_name, u.hanatag FROM external_payments e LEFT JOIN users u ON u.id=e.user_id
                               WHERE e.status=? ORDER BY e.created_at DESC LIMIT 300""", (status,))
        counts = {r["status"]: r["c"] for r in fetchall("SELECT status, COUNT(*) c FROM external_payments GROUP BY status")}
        return render_template("admin_acct_payments.html", tab="payments", rows=rows, status=status, counts=counts)

    @app.route("/admin/accounting/payments/<eid>/<action>", methods=["POST"])
    @_guard(write=True)
    def acct_payment_action(eid, action):
        d = request.get_json(silent=True) or {}
        try:
            if action == "match":
                who = (d.get("member") or "").strip()
                u = fetchone("SELECT id FROM users WHERE hanatag=? OR hanatag=? OR deposit_ref=? OR phone=? OR email=?",
                             (who, "@" + who.lstrip("@"), who.upper(), who, who.lower()))
                if not u:
                    return jsonify({"error": "No member found with that Hanatag, deposit reference, phone or email"}), 404
                txn = ledger.match_suspense(eid, u["id"], session["user_id"])
                log_admin_action("suspense_matched", "external_payment", eid, new_data={"user_id": u["id"], "txn": txn})
                return jsonify({"ok": True})
            if action == "refund":
                txn = ledger.refund_suspense(eid, session["user_id"])
                log_admin_action("suspense_refunded", "external_payment", eid, new_data={"txn": txn})
                return jsonify({"ok": True})
            if action == "chargeback":
                txn, took, loss = ledger.chargeback(eid, session["user_id"])
                log_admin_action("card_chargeback", "external_payment", eid, new_data={"txn": txn, "recovered": took, "loss": loss})
                return jsonify({"ok": True, "recovered": took, "loss": loss})
        except ledger.LedgerError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"error": "Unknown action"}), 400

    # ── Reconciliation (statements) ─────────────────────────────────────────
    @app.route("/admin/accounting/reconciliation")
    @_guard()
    def acct_recon():
        rows = []
        for cur in ledger.currencies_in_use():
            for code in sorted(ledger.CASH_ACCOUNTS):
                held = -ledger.system_balance(code, cur)
                st = fetchone("""SELECT * FROM statement_balances WHERE account_code=? AND currency=?
                                 ORDER BY created_at DESC LIMIT 1""", (code, cur))
                if held or st:
                    rows.append({"code": code, "label": ledger.SYSTEM_ACCOUNTS[code][0], "cur": cur, "held": held,
                                 "st": st, "diff": (st["balance_cents"] - held) if st else None})
        history = fetchall("""SELECT s.*, u.full_name FROM statement_balances s LEFT JOIN users u ON u.id=s.entered_by
                              ORDER BY s.created_at DESC LIMIT 50""")
        return render_template("admin_acct_recon.html", tab="recon", rows=rows, history=history,
                               currencies=ledger.currencies_in_use(), cash=sorted(ledger.CASH_ACCOUNTS),
                               labels=ledger.SYSTEM_ACCOUNTS)

    @app.route("/admin/accounting/reconciliation", methods=["POST"])
    @_guard(write=True)
    def acct_recon_add():
        d = request.get_json(silent=True) or {}
        cur = (d.get("currency") or "").upper()
        from decimal import Decimal, InvalidOperation
        try:
            cents = int((Decimal(str(d.get("balance"))) * 100).to_integral_value())
        except (InvalidOperation, TypeError, ValueError):
            return jsonify({"error": "Enter the statement balance"}), 400
        try:
            ledger.record_statement(d.get("account"), cur, cents, d.get("as_of") or datetime.utcnow().date().isoformat(),
                                    session["user_id"], (d.get("note") or "")[:200])
        except ledger.LedgerError as e:
            return jsonify({"error": str(e)}), 400
        log_admin_action("statement_recorded", "ledger", d.get("account"), new_data={"currency": cur, "balance": cents})
        return jsonify({"ok": True})

    # ── Adjustments (two different admins) ──────────────────────────────────
    @app.route("/admin/accounting/adjustments")
    @_guard()
    def acct_adjustments():
        rows = fetchall("""SELECT a.*, u.full_name AS member, u.hanatag, p.full_name AS proposer, c.full_name AS confirmer
                           FROM ledger_adjustment_requests a JOIN users u ON u.id=a.user_id
                           LEFT JOIN users p ON p.id=a.proposed_by LEFT JOIN users c ON c.id=a.decided_by
                           ORDER BY a.created_at DESC LIMIT 100""")
        return render_template("admin_acct_adjust.html", tab="adjust", rows=rows, me=session["user_id"],
                               currencies=sorted(CURRENCIES))

    @app.route("/admin/accounting/adjustments", methods=["POST"])
    @_guard(write=True)
    def acct_adjust_propose():
        d = request.get_json(silent=True) or {}
        who = (d.get("member") or "").strip()
        u = fetchone("SELECT id FROM users WHERE hanatag=? OR hanatag=? OR email=? OR phone=?",
                     (who, "@" + who.lstrip("@"), who.lower(), who))
        if not u:
            return jsonify({"error": "Member not found"}), 404
        cur = (d.get("currency") or "").upper()
        from decimal import Decimal, InvalidOperation
        try:
            cents = int((Decimal(str(d.get("amount"))) * 100).to_integral_value())
        except (InvalidOperation, TypeError, ValueError):
            return jsonify({"error": "Enter an amount (negative to take money back)"}), 400
        reason = (d.get("reason") or "").strip()
        if cur not in CURRENCIES or not cents or not ledger.is_whole(cents, cur) or len(reason) < 5:
            return jsonify({"error": "Choose a currency, a whole amount and a reason"}), 400
        aid = str(uuid.uuid4())
        with get_db() as db:
            db.execute("""INSERT INTO ledger_adjustment_requests(id,user_id,currency,amount_cents,reason,proposed_by)
                          VALUES(?,?,?,?,?,?)""", (aid, u["id"], cur, cents, reason, session["user_id"]))
        log_admin_action("adjustment_proposed", "adjustment", aid, new_data={"user_id": u["id"], "currency": cur, "amount": cents},
                         reason=reason)
        return jsonify({"ok": True})

    @app.route("/admin/accounting/adjustments/<aid>/<action>", methods=["POST"])
    @_guard(write=True)
    def acct_adjust_decide(aid, action):
        a = fetchone("SELECT * FROM ledger_adjustment_requests WHERE id=?", (aid,))
        if not a or a["status"] != "proposed":
            return jsonify({"error": "This adjustment is not waiting for a decision"}), 400
        me = session["user_id"]
        if action == "cancel":
            with get_db() as db:
                db.execute("UPDATE ledger_adjustment_requests SET status='cancelled', decided_by=?, decided_at=datetime('now') WHERE id=?",
                           (me, aid))
            log_admin_action("adjustment_cancelled", "adjustment", aid)
            return jsonify({"ok": True})
        if action != "confirm":
            return jsonify({"error": "Unknown action"}), 400
        try:
            txn = ledger.adjust(a["user_id"], a["currency"], a["amount_cents"], a["reason"], a["proposed_by"], me)
        except (ledger.LedgerError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        with get_db() as db:
            db.execute("""UPDATE ledger_adjustment_requests SET status='posted', decided_by=?, decided_at=datetime('now'), txn_id=?
                          WHERE id=?""", (me, txn, aid))
        log_admin_action("adjustment_posted", "adjustment", aid, new_data={"txn": txn})
        return jsonify({"ok": True})

    # ── Settlement simulator (simulated money only) ─────────────────────────
    @app.route("/admin/accounting/simulator")
    @_guard()
    def acct_simulator():
        circles = fetchall("""SELECT r.id, r.name, r.currency, cy.id AS cycle_id, cy.cycle_number, cy.due_at,
                                (SELECT COUNT(*) FROM contributions c WHERE c.cycle_id=cy.id AND c.status='pending') AS pending
                              FROM roscas r JOIN cycles cy ON cy.rosca_id=r.id AND cy.status IN ('open','collecting')
                              ORDER BY r.name LIMIT 100""")
        recent = fetchall("""SELECT e.*, u.full_name FROM external_payments e LEFT JOIN users u ON u.id=e.user_id
                             ORDER BY e.created_at DESC LIMIT 15""")
        return render_template("admin_acct_sim.html", tab="sim", simulated=ledger.is_simulated(), circles=circles,
                               recent=recent, currencies=sorted(CURRENCIES))

    @app.route("/admin/accounting/simulator", methods=["POST"])
    @_guard(write=True)
    def acct_simulate():
        if not ledger.is_simulated():
            return jsonify({"error": "The simulator only runs on a simulated-money service"}), 403
        d = request.get_json(silent=True) or {}
        act = d.get("action")
        me = session["user_id"]
        try:
            if act == "deposit":
                ch = d.get("channel")
                cur = (d.get("currency") or "RWF").upper()
                from decimal import Decimal
                cents = int((Decimal(str(d.get("amount") or 0)) * 100).to_integral_value())
                if cents <= 0 or not ledger.is_whole(cents, cur):
                    return jsonify({"error": f"Enter a whole {cur} amount"}), 400
                ext = (d.get("external_ref") or "").strip() or f"SIM-{ch.upper()}-{uuid.uuid4().hex[:8].upper()}"
                who = (d.get("member") or "").strip()
                uid = None
                reference = None
                if ch == "bank":
                    reference = who or None  # a bank transfer is matched by the reference the payer typed
                elif who:
                    u = fetchone("SELECT id FROM users WHERE hanatag=? OR hanatag=? OR phone=? OR email=? OR deposit_ref=?",
                                 (who, "@" + who.lstrip("@"), who, who.lower(), who.upper()))
                    uid = u["id"] if u else None
                status, eid = ledger.receive_external(ch, ext, cur, cents, reference=reference, user_id=uid,
                                                      payer=d.get("payer") or "simulated payer", created_by=me,
                                                      note="settlement simulator")
                log_admin_action("simulated_settlement", "external_payment", eid,
                                 new_data={"channel": ch, "ref": ext, "amount": cents, "currency": cur, "status": status})
                return jsonify({"ok": True, "status": status, "external_ref": ext})
            if act == "fast_forward":
                cyid = d.get("cycle_id")
                days = int(d.get("days") or 0)
                with get_db(immediate=True) as db:
                    db.execute("UPDATE cycles SET due_at=?, opens_at=? WHERE id=? AND status IN ('open','collecting')",
                               ((datetime.utcnow() - timedelta(days=max(days, 4))).isoformat(),
                                (datetime.utcnow() - timedelta(days=max(days, 4) + 30)).isoformat(), cyid))
                    db.execute("UPDATE contributions SET due_at=? WHERE cycle_id=? AND status='pending'",
                               ((datetime.utcnow() - timedelta(days=max(days, 4))).isoformat(), cyid))
                import rosca
                n = rosca.process_overdue()
                log_admin_action("simulated_fast_forward", "cycle", cyid, new_data={"missed": n})
                return jsonify({"ok": True, "missed": n})
            if act == "run_overdue":
                import rosca
                return jsonify({"ok": True, "missed": rosca.process_overdue()})
        except ledger.LedgerError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"error": "Unknown action"}), 400


