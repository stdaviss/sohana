# SOHANA — Finance Suite Handover
**Document type:** Integration + migration brief for the CFO Suite dashboards
**Version at handover:** v7.11 (gated integration complete) → v8.0 (redesign scope)
**Last updated:** July 2026
**Purpose:** Give the next agent everything needed to (a) verify the v7.11 gated integration and (b) execute the v8.0 migration to SOHANA design tokens and live data.

---

## 1. What v7.11 shipped

Two dashboards built by the founder's finance team, integrated as-is behind a strict role gate. No visual modification, no data wiring — just secure access.

### Routes added to `app.py`
```python
FINANCE_SUITE_ROLES = {"ceo", "cfo", "cto", "compliance"}

@app.route("/admin/finance/treasury")
@_finance_suite_required
def finance_treasury_engine():
    log_admin_action("finance_suite_view", "treasury_engine", None)
    return render_template("finance_treasury_engine.html")

@app.route("/admin/finance/micro-mutual")
@_finance_suite_required
def finance_micro_mutual():
    log_admin_action("finance_suite_view", "micro_mutual", None)
    return render_template("finance_micro_mutual.html")
```

### Templates added
- `templates/finance_treasury_engine.html` — 1,880 lines (Capital State Engine)
- `templates/finance_micro_mutual.html` — 2,282 lines (Micro Mutual Fund Engine)

### Access control
- Custom decorator `_finance_suite_required` — enforces `session["admin_role"] in FINANCE_SUITE_ROLES`
- Every denied attempt logged via `log_admin_action("finance_suite_access_denied", ...)`
- Every allowed view logged via `log_admin_action("finance_suite_view", ...)`
- Denied users see `error.html` with HTTP 403

### Sidebar entry (add this manually to `base.html`)
Add inside the admin sidebar block, ideally between the existing 💬 Contact and freeze controls, gated by role:

```html
{% if session.admin_role in ['ceo','cfo','cto','compliance'] %}
  <div class="sidebar-group-label">Finance Suite</div>
  <a href="/admin/finance/treasury"    class="sidebar-item">💼 Treasury Engine</a>
  <a href="/admin/finance/micro-mutual" class="sidebar-item">📊 Micro Mutual</a>
{% endif %}
```

If the sidebar uses different classnames, wrap both links in whatever group container the other admin items use — the pattern is the same as the existing admin items.

---

## 2. Known limitations at v7.11 (deliberate)

### Design-system mismatch
The two dashboards use a completely different aesthetic from the rest of SOHANA:

| Aspect | Finance Suite (current) | SOHANA design system |
|---|---|---|
| Background | Navy `#07111D` | Forest `#0E120F` |
| Accent | Gold `#D4AF37` | Mint `#9EE493` |
| Font (body) | Inter | Geist |
| Font (mono) | IBM Plex Mono | Geist Mono |
| Serif accent | None | Instrument Serif italic |
| Icons | Emoji throughout | No emoji (per Figma handover doc) |
| Tone | Bloomberg terminal | Quiet luxury / Linear |

**This is a v8.0 issue, not a v7.11 bug.** The founder is aware; the integration was prioritised so the finance team could demo the vision to investors immediately.

### Mock data
Every metric in both dashboards is generated client-side via `Math.random()`:
- KPIs (AUM, cash, deployed capital, ROI)
- Liquidity bucket balances
- Lending engine loan portfolio
- Fund member analytics
- Activity feed events
- FX rates ticker

**Zero connection to real DB tables.** Nothing in the wallet, ROSCA, campaign, or endorsement tables is being read. This means:
- The dashboards demo the vision cleanly
- No CFO decision should be made based on these numbers today
- The `<div style="color: warning">` banner should be added at the top of both templates before demoing to anyone external (see task list in Section 4)

### No CSRF
Both dashboards are read-only, so this doesn't bite immediately. But when v8.0 adds write actions (approve a disbursement, adjust bucket allocation), CSRF becomes mandatory before shipping.

---

## 3. Data-wiring plan for v8.0

The dashboards need real numbers from these existing tables:

### For Treasury Engine

| Dashboard metric | SQL source |
|---|---|
| Total AUM | `SUM(balance_cents)` on `wallets` grouped by currency, converted to USD via `EXCHANGE_RATES` |
| Cash reserves | Same as AUM but filtered to wallets with `is_default=1` and users with `kyc_status='verified'` |
| Deployed capital | `SUM(amount_cents)` on `pool_disbursements` where `status='approved'` |
| Active loans | Not in DB yet — needs `loans` table in v8.0 |
| Daily inflow | `SUM(amount_cents)` on `wallet_transactions` where `type='deposit'` and `created_at >= date('now','-1 day')` |
| Daily outflow | Same with `type='withdrawal'` |
| Liquidity buckets | Derive from wallet aggregations grouped by currency + tier: "Hot" (freeze_deposits=0), "Warm" (frozen but recoverable), "Cold" (fully frozen or long-tenure) |
| FX exposure | Balance in each currency × exchange rate volatility from `EXCHANGE_RATES_META` |

### For Micro Mutual Fund

| Dashboard metric | SQL source |
|---|---|
| Fund NAV | `SUM(pool.balance_cents)` on `pools` where `is_public=0 AND creator_id IN <admin_ids>` (define "fund" pools by admin ownership) |
| Member count | `COUNT(*)` on `pool_members` for those pools |
| Contribution schedule | `pool.frequency_days` + `pool.contribution_cents` |
| ROI | `(current NAV - total contributions) / total contributions` — needs historical NAV snapshots table |
| Member analytics | Join `pool_members` × `users` × `ncs_events` for engagement scoring |

### New DB additions v8.0 will need

```sql
-- Historical treasury snapshots for time-series charts
CREATE TABLE IF NOT EXISTS treasury_snapshots (
  id TEXT PRIMARY KEY,
  snapshot_date TEXT NOT NULL,
  total_aum_cents INTEGER NOT NULL,
  cash_reserves_cents INTEGER NOT NULL,
  deployed_cents INTEGER NOT NULL,
  fx_exposure_json TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(snapshot_date)
);

-- Loan portfolio (for the lending engine section)
CREATE TABLE IF NOT EXISTS loans (
  id TEXT PRIMARY KEY,
  borrower_id TEXT NOT NULL REFERENCES users(id),
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  interest_rate REAL NOT NULL,
  term_days INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  originated_at TEXT,
  due_at TEXT,
  repaid_at TEXT,
  approver_id TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_loans_borrower ON loans(borrower_id, status);

-- Fund NAV history (for ROI calculation over time)
CREATE TABLE IF NOT EXISTS fund_nav_snapshots (
  id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES pools(id),
  snapshot_date TEXT NOT NULL,
  nav_cents INTEGER NOT NULL,
  member_count INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(pool_id, snapshot_date)
);
```

Add these to `_run_safe_migrations()` when v8.0 begins.

### APScheduler jobs to add

- Nightly at 00:15 UTC: snapshot treasury (aggregate wallets, insert into `treasury_snapshots`)
- Nightly at 00:30 UTC: snapshot fund NAV per fund pool (insert into `fund_nav_snapshots`)

Both add ~30 lines of Python; pattern is identical to the existing `run_health_checks()` scheduler.

---

## 4. v8.0 execution sequence

Do these in order. Test each phase in Railway staging before the next.

### Phase 1 — Data warning banner (30 min, ship first)
Before anyone demos this externally, add a persistent banner to both templates:

```html
<div style="background:rgba(255,159,67,.15); border:1px solid #FF9F43; padding:.75rem 1.25rem; color:#FF9F43; font-family:'IBM Plex Mono',monospace; font-size:.82rem; text-align:center; letter-spacing:.04em">
  ⚠ DEMO DATA — All figures on this dashboard are synthetic and refresh randomly. Not for real capital decisions. Live data wiring is v8.0 scope.
</div>
```

This is non-negotiable before showing an investor.

### Phase 2 — Migrate palette (~3 hours)
Find/replace across both files:
- `#07111D` → `#0E120F`
- `#111C2D` → `#161B17`
- `#17263A` → `#1C231D`
- `#1E3150` → drop this shade — SOHANA has no fourth surface
- `#D4AF37` (gold) → **keep**, but only for currency/gold-specific contexts. All primary CTAs and positive states → `#9EE493` mint.
- `#28C76F` (positive green) → `#9EE493` mint
- `#EA5455` (negative red) → `#FF6A55` SOHANA danger
- Text colours: `#F8F9FA` → `#F4F2EC` warm cream, `#8E9AAF` → `#B7BAB4`
- Fonts: `Inter` → `Geist`, `IBM Plex Sans` → `Geist`, `IBM Plex Mono` → `Geist Mono`
- Emoji removal: replace 👤 📈 💰 with inline Lucide SVG icons

### Phase 3 — Replace mocks with live data (~4 hours)
Per the SQL table in Section 3. Add real endpoints:
- `GET /api/admin/finance/treasury/snapshot` — aggregate current state
- `GET /api/admin/finance/treasury/history?days=30` — time-series from `treasury_snapshots`
- `GET /api/admin/finance/funds/list` — list fund pools
- `GET /api/admin/finance/funds/<pool_id>/nav-history?days=90` — from `fund_nav_snapshots`

All gated with the same `_finance_suite_required` decorator.

### Phase 4 — Financial advisor role (~2 hours, only if needed for v8.0)
- Add `admin_role='financial_advisor'` option in admin_admins UI
- Add `advisor_client_assignments` table (advisor_id → client_id many-to-many)
- New route `/advisor/dashboard` — scoped to only the advisor's assigned clients
- Advisor never sees platform totals, only their book

---

## 5. Critical patterns to preserve

### Do not break the auth model
The `_finance_suite_required` decorator is the ONLY thing standing between the CFO's dashboard and every logged-in admin. If you refactor decorators, keep this one intact. Symptom of breakage: any non-finance admin can hit `/admin/finance/*` and see AUM data.

### Do not create tables outside `_run_safe_migrations()`
The three new tables in Section 3 must go inside the migrations block in `app.py`. Never create tables lazily inside route handlers — see Lesson 7 in `SOHANA_PLATFORM_STATE_v7.8.md`.

### Do not remove `log_admin_action` calls
Every dashboard view and every access denial is logged for audit. This is a regulatory-grade requirement — ACPR and FCA will both ask "who accessed treasury data on date X" and we need to answer with rows.

### Do not paginate away the demo data warning until Phase 3 is complete
The warning banner from Phase 1 stays visible until real data is wired. Removing it prematurely and demoing mock numbers as real is the single biggest reputational risk with this feature.

---

## 6. Deployment for v7.11

```bash
git add app.py \
        templates/finance_treasury_engine.html \
        templates/finance_micro_mutual.html \
        templates/base.html   # after adding the sidebar snippet from Section 1

git commit -m "v7.11 — Finance Suite: Treasury Engine + Micro Mutual Fund dashboards behind CEO/CFO/CTO/Compliance role gate"

git push
```

Railway auto-deploys in ~60 seconds. No migrations needed (v7.11 is display-only). Test as CEO, CFO, CTO, Compliance (all four should see the dashboards). Test as Fraud, Credit, Operations, Business (all four should get 403 with the "Access restricted" error page).

---

## 7. Open questions for the founder before v8.0 starts

Answer these before Phase 2 kicks off:

1. **Fund pool identification.** How do you distinguish "fund" pools (Sohana-run treasury vehicles) from user-created contribution pools? Options: (a) new column `pool.is_treasury_fund BOOLEAN`, (b) admin creator + a naming convention, (c) new table entirely. Recommend (a) — one column, one migration, no ambiguity.

2. **Loan origination workflow.** The lending engine section implies loans exist. Are these:
   - (a) Real user loans from an existing SOHANA loan product? (Doesn't exist yet.)
   - (b) Internal treasury lending to fund pools?
   - (c) Aspirational for a v9+ product?

   If (c), leave the lending card as decorative/aspirational and note it clearly.

3. **XAU (gold) reference feed.** The Treasury Engine shows gold-normalised values. Is this live from a paid feed (Bloomberg/Refinitiv), or does it use the same manual XAU/USD reference we're using on `/currencies` (indicative, updated by staff)? Recommend the latter for v8.0, with a live feed as v9.0 backlog.

4. **Financial advisor timeline.** Is this a v8.0 requirement or a v9.0 aspiration? Building the advisor role adds ~2 hours of work; skipping it lets v8.0 ship faster.

5. **Data snapshot frequency.** Nightly snapshots (recommended) means CFO sees "yesterday's numbers" every morning. Real-time aggregation is possible but expensive on SQLite and would need PostgreSQL migration first. Recommend nightly for v8.0.

---

*Update this document at the end of every Finance Suite session, then rename to the next version (v7.12, v8.0, etc.) to preserve history.*
