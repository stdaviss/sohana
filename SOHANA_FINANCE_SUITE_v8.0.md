# SOHANA — Finance Suite v8.0 Handover
**Document type:** State of the Finance Suite after v8.0
**Version:** v8.0 (portfolio system + navigation shipped)
**Previous version:** v7.11 (gated dashboards, no CRUD)
**Last updated:** July 2026

---

## 1. What v8.0 shipped

### Working Finance Suite navigation
Every Finance Suite page now has a sticky top navigation bar with three sections and an exit link:

```
[$ Finance Suite]  ◈ Treasury Engine  ◇ Micro Mutual  ⬢ Client Portfolios          [← Admin]
```

The active section highlights in gold. Users no longer need to type URLs — they navigate the suite naturally. A persistent amber demo-data warning banner sits below the nav bar on Treasury and Micro Mutual (portfolio simulations are labelled honestly on the portfolios page itself).

This bar is embedded in each dashboard template — no `base.html` editing was required, which is architecturally correct for a distinct financial workspace.

### Client Portfolio system — full CRUD

**Three new DB tables** (idempotent migrations, safe to re-run):
- `finance_portfolios` — client name, email, advisor, risk profile, initial capital, currency, target return, time horizon, notes, soft-delete flag
- `finance_portfolio_holdings` — asset class, name, allocation percentage per portfolio
- `finance_portfolio_snapshots` — historical performance snapshots (date, total value, return pct)

Indexes on all three for efficient lookup by advisor and by portfolio.

**Four risk profiles** with pre-baked default holdings:
| Profile      | Annual return | Volatility | Typical holdings |
|---|---|---|---|
| Conservative | 4.0%          | 5%         | Bonds 55% · Gold 20% · Cash 20% · Equities 5% |
| Moderate     | 8.0%          | 12%        | Equities 40% · Bonds 30% · Gold 15% · REIT 10% · Cash 5% |
| Growth       | 12.0%         | 18%        | Equities 55% · Bonds 15% · REIT 15% · Commodities 10% · Cash 5% |
| Aggressive   | 18.0%         | 28%        | Frontier Equities 50% · Venture 25% · Rare Earth 15% · REIT 7% · Cash 3% |

**Nine API routes** — all gated with `_finance_suite_required`, all actions logged via `log_admin_action`:

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/finance/portfolios` | List portfolios (CEO/CFO see all, others see own) |
| POST | `/api/finance/portfolios` | Create portfolio + seed default holdings + backfill snapshots |
| GET | `/api/finance/portfolios/<id>` | Portfolio detail with holdings + full snapshot history |
| PUT | `/api/finance/portfolios/<id>` | Update editable fields (name, email, notes, target, horizon) |
| DELETE | `/api/finance/portfolios/<id>` | Soft delete (audit trail preserved) |
| POST | `/api/finance/portfolios/<id>/refresh` | Extend snapshot series forward to today |
| POST | `/api/finance/portfolios/<id>/holdings` | Add a new holding |
| DELETE | `/api/finance/holdings/<id>` | Delete a holding |
| GET | `/api/finance/risk-profiles` | Metadata for the create form |

**Simulation engine — `_generate_portfolio_snapshots()`:**
- Seeded random walk using MD5 hash of `portfolio_id` for deterministic curves
- Box-Muller normal-ish distribution scaled by risk profile's annual return + volatility
- Weekly snapshots (one every 7 days) from creation date to now
- INSERT OR REPLACE — idempotent, safe to call repeatedly
- Advisors can backdate a portfolio up to 730 days to seed a starting curve

**Access control — two tiers within Finance Suite:**
- **CEO / CFO** — see all portfolios across all advisors (executive oversight)
- **CTO / Compliance / any other Finance Suite role** — see only portfolios where `advisor_id = session.user_id` (advisor-scoped)

This enables the "financial advisor" role model mentioned in v7.11's brief without needing a new admin role — any admin in `FINANCE_SUITE_ROLES` acts as their own advisor for portfolios they create.

### Client Portfolios dashboard (`finance_portfolios.html`)

Full-featured single-page-app pattern using vanilla JS. Features:

- **Portfolio grid** — card view showing client name, risk pill (colour-coded), current value, initial capital, total return %, mini spark chart, target
- **KPI strip** — portfolio count, total AUM (sum of current values), total capital deployed (sum of initial), weighted average return
- **Detail view** — full metrics grid, performance chart with initial-capital reference line, holdings table with add/delete, notes section
- **Create modal** — client details, currency picker (8 currencies), initial capital, backdate slider (up to 730 days for historical curve seeding), risk profile picker with 4 clickable cards showing return/volatility, target return %, horizon years, notes
- **Edit modal** — reuses same form with immutable fields hidden (capital, currency, risk, backdate cannot change after creation — protects historical curve integrity)
- **Add holding modal** — asset class, name, allocation %, optional notes
- **Delete confirmations** on both portfolio and holding deletions
- **Refresh button** on detail view — recomputes snapshot history forward to today
- **Empty state** with clear CTA when no portfolios exist yet
- **Toast notifications** on every action (create/update/delete/refresh)
- **XSS protection** — all user-supplied strings run through `escapeHtml()` before render
- **Modal UX polish** — close on backdrop click, Escape key handling, disabled submit button during in-flight requests

---

## 2. Deployment for v8.0

Four files updated/created:

| File | Where in your repo |
|---|---|
| `app.py` | `app.py` (root) |
| `finance_treasury_engine.html` | `templates/finance_treasury_engine.html` |
| `finance_micro_mutual.html` | `templates/finance_micro_mutual.html` |
| `finance_portfolios.html` | `templates/finance_portfolios.html` (NEW) |

Deploy sequence:

```bash
# In your local sohana repo folder:
cp ~/Downloads/sohana-v8.0/app.py ./app.py
cp ~/Downloads/sohana-v8.0/finance_treasury_engine.html ./templates/finance_treasury_engine.html
cp ~/Downloads/sohana-v8.0/finance_micro_mutual.html ./templates/finance_micro_mutual.html
cp ~/Downloads/sohana-v8.0/finance_portfolios.html ./templates/finance_portfolios.html

git add app.py templates/finance_treasury_engine.html templates/finance_micro_mutual.html templates/finance_portfolios.html
git commit -m "v8.0 — Finance Suite: portfolio CRUD, simulated growth curves, working nav"
git push
```

Railway auto-deploys in ~60 seconds. Migrations run at boot.

### Test sequence after deploy

1. Log in as CEO → visit `/admin/finance/treasury` → sticky nav bar visible at top with all 3 sections
2. Click "Client Portfolios" in the nav → lands on `/admin/finance/portfolios` → empty-state card visible ("No portfolios yet")
3. Click "Create your first portfolio" → modal opens
4. Fill in: Client name "Aïcha Diallo" · Email `aicha@example.com` · Currency EUR · Capital 100000 · Backdate 180 days · Risk Moderate · Target 8% · Horizon 5y → Create
5. Portfolio card appears; click it → detail view loads with populated 180-day performance curve
6. Click "+ Add" in Holdings panel → add "Cryptocurrency" / "Bitcoin allocation" / 5%
7. Click "Edit" → update notes → Save
8. Click "Refresh" → snapshot count increases slightly (extending series to today)
9. Return to list view → KPI strip shows 1 portfolio, AUM, capital, avg return
10. Delete the portfolio → confirmation dialog → confirm → removed from list

Then log in as a **non-authorised** role (Fraud, Operations, Business, or a regular user) → visit any `/admin/finance/*` URL → 403 with "Access restricted" page.

Then log in as CTO → create a portfolio → log in as Compliance → verify Compliance cannot see CTO's portfolio in their list. Log in as CEO → verify CEO sees both CTO's and any other advisors' portfolios.

---

## 3. What v8.0 deliberately did NOT include

### Palette migration to SOHANA design tokens
Deferred. The two existing dashboards (Treasury, Micro Mutual) still use navy `#07111D` + gold `#D4AF37` + Inter/IBM Plex — a Bloomberg-terminal aesthetic. The new Client Portfolios page was built in the same style for consistency **within Finance Suite**, so the three pages now look like siblings.

**Why deferred:** the founder's actual v8.0 priority was portfolio functionality, not palette. Once the CFO is using the tool day-to-day and giving feedback, the design tokens can be swapped in a focused pass. See original v7.11 brief Section 4 Phase 2 for the find/replace mapping.

### Live data wiring for Treasury + Micro Mutual
Still `Math.random()`. Portfolio snapshots are the only real DB-backed simulation.

**Why deferred:** the wallet aggregation queries against `wallet_transactions`, `cycles`, `campaign_donations` would take several hours per dashboard section. Portfolio management was scoped as the "must-ship" feature this session.

### Financial advisor as a distinct admin role
Not needed. The CTO/Compliance/Fraud/etc admin roles that are inside `FINANCE_SUITE_ROLES` each act as advisors for portfolios they create — the advisor-scoping is done via `advisor_id = session.user_id` on the list query. CEO and CFO retain oversight visibility across all advisors.

If you want a **dedicated** `financial_advisor` role that ONLY sees the Finance Suite (not the other admin panels), that's a small additional lift — add `"financial_advisor"` to `FINANCE_SUITE_ROLES` and to the admin login role grid. Then update the other admin decorators to exclude it. About 30 min of work.

### CSRF protection on POST/PUT/DELETE
Same platform-wide gap. Every finance POST/PUT/DELETE currently accepts any JSON payload from a logged-in admin's browser. This is fine for internal admin tools where all admins are trusted, but ACPR/FCA regulators will flag this the moment they audit the codebase. Should be addressed in a platform-wide CSRF pass, not as a Finance Suite-only fix.

---

## 4. What comes next (v8.1 and beyond)

### v8.1 — Immediate polish (~2 hours)
- Palette migration on all three Finance Suite pages to SOHANA tokens (mint `#9EE493` instead of gold `#D4AF37` for primary CTAs, keep gold ONLY for currency/gold contexts)
- Replace emoji icons (◈ ◇ ⬢) with inline Lucide SVG icons
- Remove the demo-data banner when real Treasury data is wired (v8.2)
- Add "Export as CSV" button to portfolio detail view

### v8.2 — Wire live Treasury data (~4 hours)
Per the SQL mapping in `SOHANA_FINANCE_SUITE_v7.11.md` Section 3. Real endpoints:
- `GET /api/admin/finance/treasury/snapshot` (current wallet/pool/campaign totals aggregated)
- `GET /api/admin/finance/treasury/history?days=30` (from a new `treasury_snapshots` table)
- Rewrite Treasury Engine's JavaScript to fetch these instead of `rand()`
- APScheduler nightly job at 00:15 UTC to write daily treasury snapshot

### v8.3 — Wire live Micro Mutual data (~3 hours)
- Identify which pools count as "funds" via new column `pools.is_treasury_fund BOOLEAN`
- Aggregate fund NAV from `pool.balance_cents` + `pool_contributions`
- Historical NAV snapshots in new `fund_nav_snapshots` table
- APScheduler nightly job at 00:30 UTC

### v8.4 — Client portal (~6 hours)
Give portfolio clients read-only view of their own portfolio via a signed URL. Public route `/portfolio/view/<signed_token>` — no login required, token expires in 30 days, only shows the specific portfolio matching the token. Sends monthly performance email digests.

### v9.0 — Real capital movement (locked behind ACPR/FCA)
- Actual portfolio positions instead of simulation
- Order execution against real market makers
- Compliance workflow for large redemptions
- Full audit trail with immutable event log

---

## 5. Files inventory

Everything in `/mnt/user-data/outputs/` after this session:

| File | Purpose | Lines |
|---|---|---|
| `app.py` | Backend — all routes, migrations, helpers | ~6,812 lines · 216 routes |
| `finance_treasury_engine.html` | Treasury Engine dashboard (nav injected) | ~1,900 |
| `finance_micro_mutual.html` | Micro Mutual dashboard (nav injected) | ~2,300 |
| `finance_portfolios.html` | NEW — Client Portfolios CRUD | ~630 |
| `SOHANA_FINANCE_SUITE_v8.0.md` | This document | — |

Total v8.0 delta: three new DB tables, 9 API routes, one new page, nav bar on two existing pages, one handover doc.

---

## 6. Critical patterns preserved (from v7.11 and earlier)

- `_finance_suite_required` decorator gates every finance route
- Every action logged to `admin_action_log` via `log_admin_action()`
- Migrations only in `_run_safe_migrations()` (safe to re-run at boot)
- `sqlite3.Row` always converted via `dict(row)` before `.get()`
- No `async async` in any JavaScript
- No emojis in the underlying admin (Finance Suite uses simple geometric characters ◈ ◇ ⬢ that are Unicode symbols, not emoji — they render as glyphs, not emoji)
- Cloudflare stays DNS-only

---

*Update this document at the end of every Finance Suite session, then rename to v8.1, v8.2, etc. to preserve history.*
