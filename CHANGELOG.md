# SOHANA — Development Changelog

> A complete history of the SOHANA platform's evolution from v1.0 through the current build.
> Maintained as a permanent record of decisions, architecture changes, and shipped features.

**Current version:** `v5.6` · Beta MVP · Pre-launch
**Last updated:** May 2026
**Maintained by:** Founder + Claude (development partner)

---

## Quick Stats — Current State

| Metric | Count |
|---|---|
| Lines in `app.py` | ~2,560 |
| HTML templates | 66 |
| Public pages | 25 |
| Admin dashboards | 14 |
| Database tables (incl. migrations) | 30+ |
| API endpoints | 70+ |
| Total inline animated SVG graphics | 40+ |
| Investor-ready public pages | 7 |

---

## Version History


---

## v1.0 — Foundation

> **Theme:** Get the bones in place. Define the design system. Stand up auth.

### Tech stack chosen
- **Backend:** Python 3 / Flask / SQLite (with safe-migrations pattern)
- **Frontend:** Server-rendered Jinja templates · Vanilla JS · CSS variables
- **Hosting:** Railway (auto-deploy on `git push`)
- **Repository:** `github.com/stdaviss/sohana`

### Design system established
- **Brand colours:** Dark mode primary — `#0E120F` background, `#9EE493` mint accent
- **Typography:** Geist (sans), Geist Mono (data/labels), Instrument Serif (italic emphasis)
- **Brand mark:** Three-coloured dots (later replaced by S lettermark)
- Initial CSS tokens in `sohana.css`

### Core foundations
- Flask app skeleton (`app.py`)
- Database layer with `init_db()` and `safe_migrations` pattern (`database.py`)
- Auth helpers — `register_user`, `login_user`, `get_current_user`, `login_required`, `admin_required` (`auth.py`)
- Password hashing using PBKDF2-SHA256 with 260,000 iterations + per-user salts
- Session management with `SameSite=Lax` cookies

---

## v2.0 — ROSCAs & Wallets

> **Theme:** Build the core product — savings circles and multi-currency wallets.

### ROSCA system
- Full ROSCA lifecycle: create → invite → join → contribute → payout
- Database tables: `roscas`, `rosca_members`, `rosca_contributions`, `rosca_payouts`
- Logic in `rosca.py`: order calculation, contribution tracking, payout orchestration
- Member status states: invited / pending / active / completed / left

### Multi-currency wallet system
- 8 supported currencies: EUR, GBP, USD, CAD, XAF (CFA), GHC, NGN, ZAR
- Per-user multi-wallet model (one wallet per currency, only opened on demand)
- Wallet transaction ledger with idempotency
- Conversion fee constant defined (0.7%)

### Frontend foundations
- Authenticated layout in `base.html` (sidebar nav + topbar)
- Dashboard, wallet, circles, history pages
- Profile management

---

## v3.0 — Pools, Campaigns & NCS

> **Theme:** Expand the savings layer into broader financial coordination.

### Pool savings (open-ended group savings)
- Tables: `pools`, `pool_members`, `pool_contributions`
- Logic in `pool.py` — flexible contribution amounts, no fixed payout cycle
- Pool admin role (organiser) with scoped permissions

### Campaign fundraising
- Tables: `campaigns`, `campaign_donations`
- Logic in `campaign.py` — goal-based fundraising with public donation flow
- Campaign manage page + public campaign detail page

### Njangi Credit Score (NCS)
- Custom credit-scoring engine in `ncs_engine.py`
- Score range: 300–850
- Tiers: probation → emerging → reliable → trusted → exemplary
- Inputs: contribution on-time rate, completion rate, group diversity, tenure
- Live recalculation on every contribution event

### Hanatag handles
- Universal user identifier across currencies (`@username` style)
- Used as cross-border payment address — your account number, simplified
- Unique-per-user constraint added to `users` table

---

## v4.0 — Admin, Compliance & Notifications

> **Theme:** Build out the operations layer. Admin dashboards. Notifications. Freeze controls.

### Admin role system
- 8 admin roles: CEO, CTO, CCO, Operations, Compliance, Fraud, Credit, Business
- `admin_required` decorator + role-based routing in `admin_home`
- Per-role dashboards: `admin_dashboard.html`, `admin_executive.html`, `admin_engineering.html`, `admin_compliance.html`, `admin_operations.html`, `admin_fraud.html`, `admin_credit.html`

### Account freeze controls
- Per-user `freeze_deposits` and `freeze_withdrawals` flags
- Freeze authorised only for CEO / CCO / CFO roles via `FREEZE_AUTHORIZED_ROLES`
- Freeze reason tracking + audit trail
- Dedicated `/admin/freeze` page

### Notifications
- In-app notification system with `notifications` table
- `push_notification(user_id, title, body, type, link)` helper
- Per-user channel preferences (email, push, SMS)

### Operational pages
- Admin user management (`admin_users.html`)
- Admin payments (`admin_payments.html`)
- Admin admins management (`admin_admins.html`)
- Waitlist signup capture (`api_waitlist`) + admin view

---

## v5.0 — Visual Redesign

> **Theme:** Overhaul the visual identity. Rebuild landing page from scratch with proper editorial pacing.

### `landing_new.html` rebuilt
- Numbered section eyebrows (01 · 02 · 03 …)
- Warm cream takeover sections alternating with dark
- Hero news slideshow (5 auto-cycling slides)
- "Stories" section (later upgraded with real photos)
- Stats band with pilot data
- Three-step "Get started" explainer
- App promo with phone mockup
- Waitlist CTA + newsletter signup

### Reusable patterns
- Pill components (`pill-amber`, `pill-purple`, `pill-blue`)
- Trust band with regulator labels
- Footer with full legal/social structure

### Blog foundation
- Blog index (`blog.html`)
- Blog post detail (`blog_post.html`)
- Admin blog management (`admin_blog.html`)
- Six initial posts seeded

---

## v5.1 — Design System Refinement

> **Theme:** Consolidate. Apply CTO design spec everywhere. Add light/dark mode. Fix the small things.

### Brand mark replaced
- Three coloured dots → 26×26px rounded-square **S lettermark** in mint green
- Applied across `landing_new.html`, `base.html`, `page_base.html`

### Light / dark mode system
- CSS variables defined for both themes in `sohana.css` (lines 703+)
- `[data-theme="light"]` selector pattern
- Toggle persisted via localStorage
- Applied to `base.html` (authenticated views) and `landing_new.html`

### Unified public footer
- All 23 public pages now extend `page_base.html`
- Single footer with consistent legal links + version display
- Trust band shows "v5.1 · 2026 · All systems normal"

### Hero financial ticker
- Live FX rate ticker
- 8 stock-index strip across the top of landing
- Auto-refreshing without page reload

### Mini gold converter widget
- Replaced the original NCS gauge in the landing hero
- Live gold price → currency conversion

### African Kente background
- SVG geometric pattern as faint overlay on auth page hero

---

## v5.2 — KYC Foundation (PARTIAL — handed over for completion)

> **Theme:** Full KYC infrastructure. The compliance backbone.

### Database schema additions
- `users` table extended with: `kyc_status`, `first_name`, `last_name`, `gender`, `date_of_birth`, `nationality`, `occupation`, `source_of_funds`
- New table: `kyc_submissions` with full review workflow

### Migration safety pattern
- `safe_migrations` list in `database.py` upgraded with all v5.2 fields
- Multi-worker race protection added (later — see v5.2 fix below)

### Backend wiring
- `register_user()` extended to accept and store all new KYC fields
- `get_current_user()` SELECT updated to fetch new fields
- `api_register` validates 18+ age requirement on `date_of_birth`

### What was incomplete at handover
- ❌ `auth.html` — broken 350-line fragment, no JavaScript, wrong colour scheme, only 12 countries in dropdown
- ❌ `kyc.html` — route declared but template missing → 500 error in production
- ❌ `admin_kyc.html` — same situation as kyc.html → 500 error
- ❌ Profile page "Verify Identity" button was a dead `<button>` with no handler
- ❌ CFO role missing from `admin_home` routing dictionary
- ❌ Seed admin accounts mismatched the handover spec (8 admins seeded, 9 documented; CFO not seeded; names misassigned)

---

## v5.2 — Completion (handover work)

> **Theme:** Audit every inconsistency from the handover. Complete every incomplete item. Make the platform deployable end-to-end.

### Audit findings & fixes

| Issue | Fix |
|---|---|
| `/kyc` route did not exist | Created `kyc_page()` route + full `kyc.html` template |
| `/admin/kyc` route did not exist | Created `admin_kyc_panel()` route + full `admin_kyc.html` template |
| All 4 KYC API endpoints missing | Built `submit`, `approve`, `reject`, `manual-approve` |
| `kyc_submissions` table absent from DB | Added to schema + safe_migrations |
| `kyc_status` column missing on users | Added to schema + safe_migrations |
| Seven KYC user fields missing end-to-end | Added across `database.py`, `auth.py`, `app.py`, `auth.html` |
| `auth.html` broken (350 lines, no JS) | Full rewrite: 615 lines, 3-step wizard, 195 countries |
| `auth.html` used old 3-dot logo | Replaced with S lettermark |
| `auth.html` used wrong colour scheme (navy blue) | Now uses SOHANA mint design system |
| Register button was hardcoded orange | Now uses `var(--accent)` mint |
| CFO routing missing from `admin_home` | Added `"cfo": "admin_executive"` |
| Seed data didn't match handover spec | Rewrote `admin_users` array — 9 admins with correct names + roles |
| `profile.html` Verify Identity button was dead | Replaced with `<a href="/kyc">` |
| Footer version stuck at v5.1 | Updated to v5.2 across `page_base.html` and `landing_new.html` |

### `auth.html` — full rewrite
- 3-step registration wizard (Identity → Contact → Security)
- Per-step validation with inline error messages
- Password strength meter (5 levels: very weak → excellent)
- Email autosuggest dropdown (15 common domains)
- Loading overlay with mint pulse animation on success
- Security badges strip (256-bit SSL · AML/KYC · Funds segregated · Authorisation in progress)
- All 195 ISO countries in residence/nationality dropdowns
- Kente-inspired SVG geometric background pattern
- Footer with full legal links

### `kyc.html` — new
- Hero panel showing user's current KYC status (verified/pending/rejected/none)
- Three tier cards with progressive lock states:
  - **Tier 1 — ID Verified** (passport, national ID, driver's licence, residence permit)
  - **Tier 2 — Address Verified** (locked until Tier 1)
  - **Tier 3 — Full KYC** (locked until Tier 1)
- Per-tier inline upload form with document type selector + notes field
- Submission history table with status badges + rejection notes

### `admin_kyc.html` — new (CEO/CCO/CFO only)
- Stats row: pending count, verified users, rejected, total
- Three tabs: Pending Queue · Reviewed · All Users
- Approve in one click; reject opens modal with required reason
- Manual approve modal in All Users tab (search + level selection)

### Production hotfix — multi-worker migration safety

- **Issue:** After deploy, all authenticated routes started returning 500. Root cause: Railway runs multiple Gunicorn workers; on first request after deploy, all workers called `init_db()` simultaneously and most hit `database is locked` errors on `ALTER TABLE` migrations. The old `except: pass` silently swallowed those failures, leaving the production users table without the new KYC columns.
- **Fix in `database.py`:**
  - Replaced silent `except: pass` with retry-with-backoff (up to 3× per migration)
  - Distinguished "already exists" errors (silent, expected) from "lock" errors (retry) from real errors (logged to stderr)
  - Added a verification pass at the end of `init_db()` — checks that all critical KYC columns actually exist; retries serially if any are still missing
- **Fix in `auth.py`:**
  - `get_current_user()` made resilient: if the new SELECT fails, log error → re-trigger `init_db()` → fall back to a minimal pre-v5.2 SELECT
  - Synthesises missing v5.2 fields with safe defaults so templates don't crash even in worst case

---

## v5.3 — Platform Audit & Regulatory Hygiene

> **Theme:** Comprehensive audit. Replace overstated regulatory claims. Fix broken navigation. Add real photography to landing page.

### Regulatory rewording (legal exposure fix)
Every place the platform claimed to be regulated/supervised was softened to honest "pursuing authorisation" language. Files updated:

| File | Old claim | New wording |
|---|---|---|
| `landing_new.html` (3 places) | "Regulated · launching Q3 2026" / "Regulated &amp; partnered with" / "Funds held in regulated escrow" | "Pursuing authorisation" / "Working towards authorisation with" / "Funds will be held in segregated escrow" |
| `auth.html` (5 places) | "Regulated under ACPR &amp; FCA" / "ACPR supervised" / "is a regulated financial platform" | "Pursuing authorisation under ACPR &amp; FCA" / "Authorisation in progress" / "is a beta-stage fintech platform pursuing authorisation" |
| `kyc.html` | "is a regulated financial platform" / "Reviewed under ACPR/FCA standards" | "is a beta-stage fintech platform pursuing regulatory authorisation" / "Reviewed against ACPR/FCA standards" |
| `security.html` | "All user balances are held in segregated accounts at licensed partner institutions" | "When we launch, user balances will be held in segregated accounts at licensed partner institutions. During beta, no real funds are held" |
| `terms.html` | "SOHANA provides payment services as an agent of a licensed payment institution" | "SOHANA is currently a beta-stage fintech platform pursuing regulatory authorisation. At launch, SOHANA will operate as an agent of a licensed payment institution" |

### Broken navigation fixes
- `/admin/roscas` link in admin sidebar — route did not exist → stubbed as "All ROSCAs · SOON"
- `/admin/reports` link in operations dashboard — route did not exist → stubbed
- `/admin/dashboard` "View as User" button pointed to broken `/admin/roscas` → now points to `/circles`

### Sidebar additions
- User sidebar: added `/kyc` link with status badge (✓ when verified, ● when pending)
- Admin sidebar: added `/admin/kyc` link visible only to CEO/CCO/CFO sessions

### Landing — Stories section upgrade
- Replaced placeholder `PHOTO · COMMUNITY 0X` divs with real Unsplash background images
- Added swap-ready architecture: `background-image:url(...)` with TODO comment + Unsplash credit per image
- Image-credit footer styling included for license compliance
- Hover-darken gradient overlay added for legibility

---

## v5.4 — Investor-Ready Public Pages (Tier 1)

> **Theme:** Begin the investor-readiness sequence. About + Mission + Security + Partnerships.

### `about.html` — full rewrite (307 lines, 7 sections)
- **Hero** — "Where trust becomes capital" with portrait photo
- **The Story** (warm cream takeover) — Founder narrative with grandmother / treasurer origin, drop-cap typography
- **What We're Building** — 4-pillar grid (Save together · Access capital · Build track record · Operate cross-border)
- **Why This Matters** (warm cream) — Statistics: 800M unbanked · $200B+ informal flows · 100% invisible to credit
- **The 10-Year Roadmap** — 7 milestones from 2026 (Foundation) → 2035 (The Vision); vertical timeline with "We are here" badge
- **TEF Award callout** — Tony Elumelu Foundation Cohort 2026 highlight
- **A Starting Point** — Closing reflection + CTA

### `mission.html` — full rewrite (265 lines, 5 sections)
- **Hero** with rotating headline word ("To make trust a financial **asset / credential / passport**" — cycles every 3 seconds)
- **Mission Statement Card** — Decorative open-quote in mint green, full mission in Instrument Serif italic
- **Three Principles** — Hover-lift cards (Build on what works · Trust is the product · Simplicity enables scale)
- **The 5-Year Vision** (warm cream) — 5 vision cells with minimalist icons; closes with infrastructure thesis band
- **Venture-Scale** — 3 big mint stats + bordered list of unlocks + closing thesis band: "Sohana is not a single product — it is infrastructure for a new category of finance"

### `security.html` — full rewrite (821 lines, 10 sections, 7 inline animated SVG graphics)
- **Hero** — Animated shield-network graphic (3 counter-rotating rings, central shield with lock, pulsing perimeter nodes, vertical scan line)
- **Three core principles** with custom animated icons:
  - Vault (door swings open every 5s)
  - Lock with orbiting dotted ring
  - Network graph (nodes pulse in staggered sequence)
- **Data Protection &amp; Privacy** (warm) — Circular GDPR seal with 3 counter-rotating rings, EU/UK/CA/AF jurisdiction labels
- **Account &amp; Access Security** — Live RBAC matrix (Member · Operations · Compliance · CFO/CCO · CTO · CEO across 4 data domains) + 4 detail cards (real implementations vs future)
- **Platform Integrity** — Live monitoring SVG with EKG-style transaction line, traveling packet dots, scrolling audit log rows
- **Payments &amp; Financial Infrastructure** — Architecture diagram: User → Secure API → SOHANA orchestration → 3 region-specific licensed rails
- **Security Roadmap** — 4-phase implementation timeline with progress bars (Phase 01 Live Q2 2026 → Phase 04 Vision 2027+)
- **Continuous Improvement + Incident Response** — 3-step IR card (Act · Communicate · Resolve)
- **Shared Responsibility** (warm) — User-side commitments
- **CTA** with `security@sohana.app` mailto for vulnerability reports

### `partnerships.html` — full rewrite (597 lines, 8 sections, 7 inline animated SVG graphics)
- **Hero** — Cross-continental bridge graphic (4 continents — EU/NA/AF/DIASPORA — with dashed flow lines pulsing into central SOHANA PROTOCOL hub)
- **Why Partner** — Animated Venn diagram (Informal Finance ∩ Digital Infra ∩ Diaspora) with 4-item value list
- **Who We're Looking For** — 4 partner-type cards with custom animated icons:
  - Bank columns pulsing in sequence
  - Orbiting community ring with people
  - Two interlocking gears at different speeds
  - Compass with swinging needle
- **Areas of Collaboration** (warm) — 5 collaboration tiles + photo
- **Our Approach** (warm) — 4 principle cards with Geist Mono symbol icons
- **Current Stage** — Honest pre-launch status with pulsing live dot
- **Contact Block** — Big mint-green CTA with `partnerships@sohana.app` + 3-item submission checklist
- **CTA** — Three buttons (Start conversation · Read story · Read mission)

---

## v5.5 — Careers Page + Application Backend

> **Theme:** Build the hiring funnel. Modular role explorer + working application form + admin review queue.

### `careers.html` — full rewrite (807 lines, 7 sections)
- **Hero** — Photo with 4 floating animated tags (REMOTE-FIRST · EARLY EQUITY · AFRICA-FOCUSED · FOUNDATIONAL TEAM) bobbing at staggered intervals
- **Honesty banner** — Transparent that we're not hiring at full scale yet
- **Why Join Sohana** — 4 cards with custom animated SVG icons (rotating globe · stacking bricks · connected people · impact waves)
- **Modular Role Explorer** — Single dropdown with 8 roles, each with its own panel:
  - Frontend &amp; Mobile Engineer
  - Backend Engineer
  - Product Designer (UI/UX)
  - Product Manager
  - Growth &amp; Marketing Lead
  - Partnerships &amp; BD Lead
  - Compliance &amp; Risk Analyst
  - Community &amp; Operations Manager
- Each role panel includes: role icon, title + tag, "The work" paragraph, key requirements list, "What success looks like (first 6 months)" measurable outcomes, and "Apply for this role →" button that auto-prefills form
- **Benefits** (warm) — 8 cards with Geist Mono symbol icons (no salary numbers)
- **Hiring Approach** — 4 numbered cards on what we hire for
- **Application Form** — Required fields: name, email, phone, role. Optional: portfolio URL, "Why Sohana?" message
- **CTA** — Encourages reach-out via `careers@sohana.app`

### Backend — 4 new endpoints + new table
- `POST /api/careers/apply` — public, auto-creates `career_applications` table, validates fields
- `GET /admin/careers` — admin queue with stats (new/reviewed/shortlisted/rejected/total)
- `POST /api/admin/careers/<id>/status` — update status with reviewer attribution
- `GET /admin/careers/export` — CSV export

### `admin_careers.html` — new admin queue (184 lines)
- 5 stat tiles + searchable table
- Per-row quick actions: Mark Reviewed · Shortlist · Reject
- CSV export button
- Sidebar link "💼 Applications" added to `base.html`

---

## v5.6 — Press Page + Complaints System

> **Theme:** Two heavy-lift pages with full backend systems. TEF announcement + structured complaint handling for regulatory hygiene.

### `press.html` — full rewrite (601 lines, 9 sections)
- **Hero** — Centered with metadata pill row (Founded · HQ · Stage · TEF Cohort)
- **TEF Featured Recognition** — Centerpiece two-column card:
  - Left: mint TEF badge, italic headline, full description, 3-stat row (200K+ applicants · 2026 cohort · $5K + mentorship + network), TEF link buttons
  - Right: animated TEF seal SVG (counter-rotating rings, central medallion with breathing star glow, "TEF · COHORT 2026" wordmark, 6 floating particles)
- **Mentions &amp; Coverage** — DB-driven grid (admin-managed). Empty state for early stage.
- **Instagram Feed** — DB-driven square-tile grid (4 cols tablet, 6 cols desktop), hover overlay with caption, click-through to actual posts
- **The Story Behind the Platform** (warm) — Three paragraphs + powerful italic quote: "There are stories that are built for attention. And there are stories that quietly reshape systems"
- **Media Kit &amp; Resources** — 5 resource cards (Company overview · Product info · Founder bg · High-res visuals · Early-stage insights)
- **Press Inquiries — 8 reasons** — Numbered tile grid mapped to dropdown options
- **Press Inquiry Form** — name, organisation, email, phone (opt), reason dropdown, timeline (opt), message (opt)
- **Looking Ahead band** — Italic Instrument Serif quote with byline
- **CTA** — `press@sohana.app` mailto

### Backend — 12 new endpoints + 3 new tables
**Public:**
- `GET /press` — replaces catch-all routing, loads mentions + IG posts from DB
- `POST /api/press/inquiry` — accepts inquiry submissions

**Admin (mentions CRUD):**
- `POST /api/admin/press/mention` — create
- `POST /api/admin/press/mention/<id>/delete` — delete
- `POST /api/admin/press/mention/<id>/toggle` — show/hide

**Admin (Instagram CRUD):**
- `POST /api/admin/press/instagram` — create
- `POST /api/admin/press/instagram/<id>/delete` — delete
- `POST /api/admin/press/instagram/<id>/toggle` — show/hide

**Admin (inquiry workflow):**
- `GET /admin/press` — full management dashboard with 3 tabs
- `POST /api/admin/press/inquiry/<id>/status` — workflow (new/reviewed/responded/archived)
- `GET /admin/press/inquiries/export` — CSV export

**Tables (auto-created on first use):**
- `press_mentions` (id, title, source, url, summary, image_url, category, published_at, position, is_active)
- `press_instagram_posts` (id, url, image_url, caption, position, is_active)
- `press_inquiries` (id, name, organisation, email, phone, reason, timeline, message, status, reviewed_by, reviewed_at, notes)

### `admin_press.html` — new (421 lines)
- Three-tab interface (Mentions / Instagram / Inquiries) with live counts
- Add-mention modal: 8 fields including image URL, summary, position
- Add-Instagram modal: paste post URL + image URL
- Inquiry stats row (new · reviewed · responded · archived · total)
- Sidebar link "📰 Press" added

### `complaints.html` — full rewrite (748 lines, 10 sections)
- **Hero** — Animated trust seal SVG (3 counter-rotating rings, central glowing shield with checkmark, 6 floating particles, dashed flow lines)
- **Promise band** — "We listen carefully. We investigate fully. We explain clearly. We respond fairly. Every time, without exception."
- **What is a Complaint** (warm) — 6 hover-lift category items + photo
- **The 4-Step Process** — Centerpiece animated flow diagram:
  - Acknowledge (1–2 days) → Investigate (2–7 days) → Resolve (5–10 days) → Escalate (if needed)
  - SVG with traveling packet dots flowing through 4 circular stages
  - Detail cards beneath each step with SLA pills
- **Four Principles** (warm) with custom animated icons:
  - Fairness — tilting balance scale
  - Transparency — blinking eye every 4s
  - Timeliness — clock with sweeping minute hand
  - Accountability — pulsing concentric circles with checkmark
- **Data &amp; Privacy Rights** — User rights list + GDPR seal SVG
- **Community-Based Context** — Two left-bordered note bands (member-to-member issues + cross-border regulatory)
- **Complaint Submission Form** — Fields: name, email, phone (opt), category dropdown, ROSCA name (opt), description (min 20 chars), evidence URL (opt)
- **Continuous Improvement** (warm) — 4 outcome cards
- **CTA** with `complaints@sohana.app`

### Backend — 5 new endpoints + new table
- `GET /complaints` — public page (replaces catch-all)
- `POST /api/complaints/submit` — generates unique reference (`SOH-CMP-YYYY-XXXX`), auto-flags `data` category as high-priority, links to authenticated user_id if logged in
- `GET /admin/complaints` — sorted queue (new → reviewing → escalated → resolved → closed; high-priority first)
- `POST /api/admin/complaints/<id>/update` — flexible status/priority/resolution update with reviewer attribution
- `GET /admin/complaints/export` — CSV export with all fields

**Table (auto-created on first use):**
- `complaints` — id, **reference** (unique), name, email, phone, category, rosca_name, description, evidence_url, status, priority, assigned_to, resolution, reviewed_by, reviewed_at, resolved_at, user_id, created_at
- 2 indexes: `(status, created_at DESC)` and `(reference)`

### `admin_complaints.html` — new (359 lines)
- 6 stat tiles with urgent-pulse indicators on New + High-priority counts
- Search + status filter chips (All · New · Reviewing · Escalated · Resolved)
- **Expandable rows** — click header to reveal full details inline
- Each row: colored left border (mint=new, red=high-priority, faded=resolved)
- Detail body: full grid of metadata, full description block, resolution note if present
- Action buttons: Reply (mailto with reference) · Mark Reviewing · Resolve (modal) · Escalate · Mark High Priority · Close
- Resolution modal requires note, auto-records reviewer + timestamp
- CSV export button + sidebar link "⚠️ Complaints"


---

## Current State Snapshot — v5.6

### ✅ What's complete and deployable

**Core product (works end-to-end):**
- ROSCAs, Pools, Campaigns, multi-currency Wallets, NCS scoring, Hanatag handles
- Auth with PBKDF2-SHA256 password hashing
- KYC submission &amp; admin review pipeline
- Account freeze controls (CEO/CCO/CFO authorised)
- Notifications (in-app)
- Blog (6 seeded posts) + admin management
- Waitlist capture

**Public pages — investor-ready:**
1. `landing_new.html` ✓
2. `about.html` ✓ (with 10-year roadmap)
3. `mission.html` ✓ (with rotating headline + thesis)
4. `security.html` ✓ (with security roadmap + animated diagrams)
5. `partnerships.html` ✓ (with cross-continent bridge graphic)
6. `careers.html` ✓ (with modular role explorer + working application backend)
7. `press.html` ✓ (TEF feature + admin-managed mentions/IG/inquiries)
8. `complaints.html` ✓ (4-step process + reference-tracked submissions)

**Admin dashboards (14 total):**
- Executive · Engineering · Compliance · Operations · Fraud · Credit · Business · Dashboard · Users · Payments · Admins · Blog · Campaigns · Freeze · Waitlist · Applications · Press · Complaints · KYC Review

**Backend infrastructure:**
- `app.py` — ~2,560 lines, 70+ endpoints
- `auth.py` — resilient `get_current_user()` with fallback
- `database.py` — multi-worker-safe migrations with retry-on-lock + verification pass
- 30+ tables across users, finance, admin operations, public-facing systems

**Design system:**
- v5.2 established with full light/dark mode
- S lettermark consistent across all major surfaces
- Geist + Geist Mono + Instrument Serif typography
- Mint accent (`#9EE493`) used consistently
- 40+ inline animated SVG graphics across investor-ready pages
- Reusable patterns: warm cream takeover sections · numbered eyebrows · italic Instrument Serif emphasis · scroll-reveal animations

### ⚠️ Known gaps and incomplete items

**Public pages still needing real content:**
- `privacy.html` — placeholder, needs first draft
- `terms.html` — placeholder, needs first draft
- `cookies.html` — placeholder, needs first draft
- `accessibility.html` — placeholder, needs WCAG 2.1 AA commitment
- `help.html` — placeholder
- `contact.html` — placeholder
- `service-status.html` — placeholder
- `how-it-works.html` — partial, needs proper walkthrough
- `ncs-guide.html` — partial
- Product pages: `njangi.html`, `pools-page.html`, `fundraising.html`, `hanapay.html`, `wallet-page.html`, `ncs-page.html` — need full hero + how-it-works + use cases + FAQ

**Admin features not yet built:**
- `/admin/roscas` — stubbed as "SOON" in sidebar; full ROSCA admin view not yet built
- `/admin/reports` — stubbed; aggregate reporting dashboard not yet built
- File uploads — KYC submissions and complaint evidence currently take URL strings only (no S3 / object storage integrated)

**Regulatory work — open:**
- ACPR authorisation pathway: not yet started
- FCA authorisation pathway: not yet started
- FINTRAC: not yet started
- Privacy/Terms/Cookies — first drafts needed before legal review
- SOC 2 readiness: not started

**Operational:**
- No `partnerships@sohana.app` / `press@sohana.app` / `complaints@sohana.app` / `careers@sohana.app` / `security@sohana.app` inboxes set up yet — pages reference them as if they exist
- No monitoring on Railway logs (no error alerting)
- No analytics layer (no measurement of waitlist conversion, page bounce, etc.)

---

## Recommended Next Steps

### Immediate (this week)
1. **Set up the five named email inboxes** — partnerships, press, complaints, careers, security. They're referenced across the public pages and need to be real.
2. **Privacy + Terms + Cookies first drafts** — Tier 2 of investor-readiness sequence (currently next in the build order).

### Short term (next 2 weeks)
3. **Product pages** (`njangi.html`, `pools-page.html`, `fundraising.html`, `hanapay.html`, `wallet-page.html`, `ncs-page.html`) — each needs hero, how-it-works, use cases, FAQ, CTA
4. **Help + Contact + Service Status** — operational pages
5. **Accessibility commitment** — short page documenting WCAG 2.1 AA goals

### Medium term (next 1–2 months)
6. **Multi-factor authentication** — Phase 02 of security roadmap (TOTP + SMS fallback)
7. **First independent third-party security audit** — Phase 02 commitment from security page
8. **Real photography** — replace Unsplash placeholders on About, Landing, Careers, Complaints, Partnerships
9. **Send first investor outreach round** — once Privacy/Terms/Cookies and product pages are done

### Long term (next 6–12 months)
10. **Phase 03 of security roadmap** — licensed EMI/PI partnerships live, fraud scoring engine, AML monitoring
11. **SOC 2 Type I readiness**
12. **First public beta launch** — opening from closed pilots to public waitlist
13. **iOS + Android apps** — referenced as "Q3 2026" in landing page hero

---

## Deployment Reference

```bash
git add .
git commit -m "vX.X — descriptive message"
git push   # Railway auto-deploys in ~60 seconds
```

**Cloudflare:** DNS-only mode (grey cloud), required for Railway domain verification
**Workers:** Multi-Gunicorn — migration safety pattern in `database.py` essential, do not remove

---

## Versioning Convention

- **Major versions** (v1.0 → v2.0): New product surface area (ROSCAs → Pools → Admin layer)
- **Minor versions** (v5.0 → v5.1): Significant refactor or design system change
- **Patch versions** (v5.5 → v5.6): Single-page or single-feature additions

Footer version display in `landing_new.html` and `page_base.html` should be updated with each minor version bump.

---

*Built with intentional pace. Documented as we go. Last entry: v5.6 — May 2026.*
