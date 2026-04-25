# SOHANA — Technical Specification & Architecture Document

> **Document purpose:** Complete technical reference for the SOHANA platform codebase.  
> Written for Claude (AI assistant), developers, and technical collaborators working on the project.  
> All specs reflect the `sohana_v3.zip` release.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Technology Stack](#2-technology-stack)
3. [Project Structure](#3-project-structure)
4. [Database Schema](#4-database-schema)
5. [Backend Architecture](#5-backend-architecture)
6. [Frontend Architecture](#6-frontend-architecture)
7. [Admin System](#7-admin-system)
8. [NCS Credit Scoring Engine](#8-ncs-credit-scoring-engine)
9. [Hanatag System](#9-hanatag-system)
10. [Multicurrency Wallet](#10-multicurrency-wallet)
11. [ROSCA Engine](#11-rosca-engine)
12. [API Reference](#12-api-reference)
13. [Security Model](#13-security-model)
14. [Demo Accounts](#14-demo-accounts)
15. [Scaling Roadmap](#15-scaling-roadmap)
16. [Payment Gateway Integration Plan](#16-payment-gateway-integration-plan)
17. [Production Deployment](#17-production-deployment)

---

## 1. Project Overview

SOHANA is a full-stack fintech platform that digitises the Tontine, Njangi, Esusu, and ROSCA (Rotating Savings and Credit Association) — community savings models that operate across Africa and the diaspora. It adds:

- A portable **Njangi Credit Score (NCS)** built from savings behaviour
- A **Hanatag** payment identity system (`@username`) for instant internal transfers
- A **multicurrency wallet** (8 currencies) with Wise-style currency conversion
- **8 specialised admin dashboards** for CEO, CTO, CCO, Operations, Compliance, Fraud, Credit, and Business roles
- A **blog/news** section, notification system, badge/achievement system, and leaderboard

**Current status:** MVP — suitable for closed beta testing. Database is SQLite (file-based). All financial transactions are simulated. Ready to connect to real payment gateways.

---

## 2. Technology Stack

### Backend

| Layer | Technology | Version | Notes |
|---|---|---|---|
| Language | Python | 3.10+ (3.12 recommended) | No async — synchronous WSGI |
| Web framework | Flask | 3.1.0 | Minimal, no ORM |
| WSGI server | Gunicorn | 21.2.0 | 2 workers in production |
| Database | SQLite3 | Built-in | File-based, zero-config |
| Auth | PBKDF2-HMAC-SHA256 | Python stdlib | 260,000 iterations, salted |
| Sessions | Flask server-side sessions | — | Cookie-based, signed with SECRET_KEY |
| Password hashing | `hashlib.pbkdf2_hmac` | Python stdlib | Salt stored inline with hash |

**No external Python dependencies beyond Flask and Gunicorn.** The entire backend runs on 2 packages.

### Frontend

| Layer | Technology | Notes |
|---|---|---|
| Markup | HTML5 | Jinja2 templates rendered server-side |
| Styling | Vanilla CSS | Custom design system in `sohana.css` (2,200+ lines) |
| Scripting | Vanilla JavaScript (ES2020) | No framework, no build step |
| Charts | HTML5 Canvas API | Custom sparklines, bar charts, donut charts drawn in JS |
| Icons | Unicode emoji + CSS | No icon font dependencies |
| Fonts | Inter (Google Fonts CDN) | weights 400/500/600/700/800 |
| Template engine | Jinja2 (via Flask) | Server-side rendering |

**Zero JavaScript build pipeline.** No webpack, no npm, no React, no node_modules. The entire frontend ships as static files.

### Infrastructure (current — MVP)

| Component | Solution | Notes |
|---|---|---|
| Hosting | Railway / Render / Fly.io | See deployment section |
| Database | SQLite file on persistent volume | Upgradeable to PostgreSQL |
| File storage | Local filesystem | No media uploads yet |
| Email | Not connected | Placeholder in code |
| SMS / OTP | Demo mode (any 6 digits) | Ready for Twilio/Africa's Talking |
| CDN | None | Static files served by Flask |

---

## 3. Project Structure

```
sohana/
│
├── app.py                      # Flask application — all routes, page handlers, API endpoints
├── auth.py                     # Authentication — register, login, session, decorators
├── database.py                 # Database — schema, migrations, helpers, fee schedules
├── ncs_engine.py               # NCS credit scoring engine — tiers, badges, events, leaderboard
├── rosca.py                    # ROSCA engine — create, join, contribute, cycles, payouts
│
├── requirements.txt            # flask==3.1.0, gunicorn==21.2.0
├── Procfile                    # gunicorn app:app --bind 0.0.0.0:$PORT --workers 2
├── railway.toml                # Railway platform config
├── render.yaml                 # Render platform config
├── README.md                   # User-facing setup guide
├── CLAUDE.md                   # This document
│
├── api/
│   └── __init__.py             # API module stub (reserved for future API versioning)
│
├── static/
│   ├── css/
│   │   └── sohana.css          # Complete CSS design system (~2,200 lines)
│   └── js/
│       └── app.js              # Global JS — API helpers, toast, modal, sparkline, OTP
│
└── templates/
    ├── base.html               # User shell — sidebar, topbar, notification panel
    ├── landing.html            # Public landing page
    ├── auth.html               # Login + register (tab-switched, no redirect)
    ├── dashboard.html          # Main user dashboard
    ├── wallet.html             # Multicurrency wallet — deposit / withdraw / pay / convert
    ├── circles.html            # ROSCA marketplace + my circles
    ├── circle_detail.html      # Individual circle — contribute, leaderboard, endorse
    ├── organiser.html          # Organiser control panel
    ├── ncs.html                # Full NCS score breakdown, history, tips, loan eligibility
    ├── profile.html            # User profile — hanatag, QR, badges, settings, limits
    ├── history.html            # Contribution history table
    ├── notifications.html      # Notification list (full page)
    ├── blog.html               # Blog/news article grid
    ├── blog_post.html          # Single blog post
    │
    ├── admin_base.html         # Dark admin shell — sidebar, topbar, nav
    ├── admin_login.html        # Dedicated admin sign-in page
    ├── admin_dashboard.html    # Admin ROSCA overview (Operations/Business)
    ├── admin_executive.html    # CEO Super Admin Dashboard
    ├── admin_operations.html   # Operations Officer Dashboard
    ├── admin_compliance.html   # CCO / Compliance Manager Dashboard
    ├── admin_fraud.html        # Fraud Analyst Dashboard
    ├── admin_credit.html       # Credit Officer Dashboard
    ├── admin_engineering.html  # CTO / Engineering Dashboard
    ├── admin_payments.html     # Payments overview table
    ├── admin_admins.html       # Admin user management + invite
    ├── admin_users.html        # All users table with NCS scores
    └── admin_blog.html         # Blog post management
```

**Total files:** 40  
**Total lines of code:** ~5,500 (Python) + ~3,500 (HTML/Jinja2) + ~2,200 (CSS) + ~400 (JS)

---

## 4. Database Schema

**Engine:** SQLite3  
**Mode:** WAL (Write-Ahead Logging) for concurrent reads  
**Foreign keys:** Enforced via `PRAGMA foreign_keys=ON`

### Tables

#### `users`
```sql
id            TEXT PRIMARY KEY          -- UUID v4
phone         TEXT UNIQUE NOT NULL      -- E.164 format e.g. +33611000001
email         TEXT UNIQUE               -- Optional, required for admin accounts
full_name     TEXT NOT NULL
password_hash TEXT NOT NULL             -- salt$pbkdf2_sha256_hex
country       TEXT DEFAULT 'RW'         -- ISO 3166-1 alpha-2
hanatag       TEXT UNIQUE               -- e.g. @mariang1234
bio           TEXT
language      TEXT DEFAULT 'en'         -- en, fr, sw, yo
base_currency TEXT DEFAULT 'EUR'        -- preferred display currency
ncs_score     INTEGER DEFAULT 300       -- 300–850
ncs_tier      TEXT DEFAULT 'probation'  -- probation | developing | reliable | exemplary
kyc_level     TEXT DEFAULT 'phone'      -- phone | id | full
is_admin      INTEGER DEFAULT 0         -- 0 = user, 1 = admin
admin_role    TEXT                      -- ceo | cto | cco | fraud | credit | operations | compliance | business
notif_email   INTEGER DEFAULT 1
notif_push    INTEGER DEFAULT 1
notif_sms     INTEGER DEFAULT 0
created_at    TEXT DEFAULT datetime()
updated_at    TEXT DEFAULT datetime()
```

#### `wallets`
```sql
id          TEXT PRIMARY KEY
user_id     TEXT → users(id)
currency    TEXT DEFAULT 'EUR'   -- EUR | GBP | USD | CAD | XAF | GHC | NGN | ZAR
is_default  INTEGER DEFAULT 0
created_at  TEXT
UNIQUE(user_id, currency)
```

#### `wallet_transactions`
```sql
id            TEXT PRIMARY KEY
wallet_id     TEXT → wallets(id)
amount_cents  INTEGER             -- negative = debit, positive = credit
balance_after INTEGER             -- running balance for audit trail
description   TEXT
ref_type      TEXT                -- contribution | cycle | withdrawal | deposit | conversion | pay_in | pay_out | fee
ref_id        TEXT                -- UUID of the related record
tx_type       TEXT DEFAULT 'other'
created_at    TEXT
```

#### `currency_conversions`
```sql
id              TEXT PRIMARY KEY
user_id         TEXT → users(id)
from_currency   TEXT
to_currency     TEXT
from_cents      INTEGER
to_cents        INTEGER
exchange_rate   REAL
fee_cents       INTEGER           -- 0.7% of from_cents, min 50 cents
created_at      TEXT
```

#### `roscas`
```sql
id                  TEXT PRIMARY KEY
name                TEXT NOT NULL
description         TEXT
organiser_id        TEXT → users(id)
rosca_type          TEXT DEFAULT 'fixed_order'   -- fixed_order | random_draw | bidding
status              TEXT DEFAULT 'forming'        -- forming | active | completed | cancelled
contribution_cents  INTEGER NOT NULL
currency            TEXT DEFAULT 'EUR'
frequency_days      INTEGER DEFAULT 30
max_members         INTEGER DEFAULT 12
ncs_min_score       INTEGER DEFAULT 300
is_public           INTEGER DEFAULT 1
current_cycle       INTEGER DEFAULT 0
total_cycles        INTEGER DEFAULT 0
creation_fee_cents  INTEGER DEFAULT 0
created_at          TEXT
updated_at          TEXT
```

#### `rosca_members`
```sql
id        TEXT PRIMARY KEY
rosca_id  TEXT → roscas(id)
user_id   TEXT → users(id)
slot      INTEGER              -- payout position (1 = first to receive)
status    TEXT DEFAULT 'active' -- active | left | defaulted
joined_at TEXT
UNIQUE(rosca_id, user_id)
```

#### `cycles`
```sql
id           TEXT PRIMARY KEY
rosca_id     TEXT → roscas(id)
cycle_number INTEGER
recipient_id TEXT → users(id)
pot_cents    INTEGER DEFAULT 0   -- grows only from actual contributions
status       TEXT DEFAULT 'open' -- open | collecting | completed
due_at       TEXT
completed_at TEXT
created_at   TEXT
UNIQUE(rosca_id, cycle_number)
```

#### `contributions`
```sql
id            TEXT PRIMARY KEY
cycle_id      TEXT → cycles(id)
rosca_id      TEXT
user_id       TEXT → users(id)
amount_cents  INTEGER
status        TEXT DEFAULT 'pending' -- pending | paid | late | missed
due_at        TEXT
paid_at       TEXT
late_days     INTEGER DEFAULT 0
created_at    TEXT
UNIQUE(cycle_id, user_id)
```

#### `ncs_events`
```sql
id            TEXT PRIMARY KEY
user_id       TEXT → users(id)
event_type    TEXT               -- contribution_on_time | contribution_late | cycle_completed | etc.
score_before  INTEGER
delta         INTEGER            -- positive or negative
score_after   INTEGER
ref_type      TEXT
ref_id        TEXT
metadata      TEXT               -- JSON blob
recorded_at   TEXT
```

#### `badges`
```sql
id          TEXT PRIMARY KEY
user_id     TEXT → users(id)
badge_type  TEXT                 -- first_contribution | streak_3 | cycle_1 | etc.
label       TEXT
earned_at   TEXT
UNIQUE(user_id, badge_type)
```

#### `endorsements`
```sql
id         TEXT PRIMARY KEY
from_id    TEXT → users(id)
to_id      TEXT → users(id)
rosca_id   TEXT → roscas(id)
created_at TEXT
UNIQUE(from_id, to_id, rosca_id)   -- one endorsement per pair per circle
```

#### `notifications`
```sql
id         TEXT PRIMARY KEY
user_id    TEXT → users(id)
title      TEXT
body       TEXT
notif_type TEXT DEFAULT 'info'  -- info | success | warning | danger
link       TEXT
is_read    INTEGER DEFAULT 0
created_at TEXT
```

#### `payment_methods`
```sql
id          TEXT PRIMARY KEY
user_id     TEXT → users(id)
method_type TEXT    -- bank_eu | bank_uk | bank_us | bank_swift | mobile_money
label       TEXT
details     TEXT    -- JSON: {iban, bic, account, sort_code, routing, holder, number}
is_default  INTEGER DEFAULT 0
created_at  TEXT
```

#### `hanatag_payments`
```sql
id           TEXT PRIMARY KEY
sender_id    TEXT → users(id)
recipient_id TEXT → users(id)
amount_cents INTEGER
currency     TEXT
note         TEXT
created_at   TEXT
```

#### `fraud_alerts`
```sql
id          TEXT PRIMARY KEY
user_id     TEXT → users(id)
alert_type  TEXT       -- suspicious_tx | account_takeover | multiple_accounts | velocity
risk_level  TEXT       -- low | medium | high
risk_score  INTEGER    -- 0–100
amount_cents INTEGER
resolved    INTEGER DEFAULT 0
created_at  TEXT
```

#### `blog_posts`
```sql
id           TEXT PRIMARY KEY
title        TEXT
slug         TEXT UNIQUE         -- URL-safe identifier
excerpt      TEXT
body         TEXT
category     TEXT DEFAULT 'news' -- news | tips | education
author_id    TEXT → users(id)
is_published INTEGER DEFAULT 1
published_at TEXT
created_at   TEXT
```

---

## 5. Backend Architecture

### Module responsibilities

| Module | Lines | Responsibility |
|---|---|---|
| `app.py` | 907 | Route handlers, page controllers, API endpoints, seed data |
| `database.py` | 387 | Schema definition, migrations, connection management, fee constants, helper functions |
| `ncs_engine.py` | 198 | NCS scoring model, event processing, badge logic, leaderboard calculation |
| `rosca.py` | 137 | ROSCA lifecycle — create, join, contribute, cycle rotation, automatic payouts |
| `auth.py` | 68 | Password hashing, login (phone or email), session decorator, admin guard |

### Request flow

```
Browser request
    ↓
Flask router (app.py)
    ↓
before_request → ensure_db() → init_db() + _seed_all() [first request only]
    ↓
Route handler
    ├── @auth.login_required     (user pages)
    ├── @admin_required(role)    (role-specific admin pages)
    └── @any_admin_required      (shared admin pages)
         ↓
    fetchone() / fetchall()  →  SQLite3 via get_db() context manager
         ↓
    render_template()        →  Jinja2 → HTML response
         OR
    jsonify()                →  JSON API response
```

### Key design decisions

- **No ORM** — raw SQLite3 queries for maximum transparency and minimal dependencies
- **Append-only ledger** — `wallet_transactions` never deletes or updates rows; balance is always derived from `balance_after` of the last row per wallet
- **Connection-per-request** — `get_db()` opens and closes a connection per operation; no connection pooling needed at SQLite scale
- **`_db` parameter pattern** — functions that must run inside an existing transaction accept `_db=None`; if provided, they reuse the connection instead of opening a new one (prevents SQLite locking)
- **Seed guard** — `_seed_all()` checks for a known phone number; if present, it returns immediately. Safe to call on every cold start.

### Fee schedule (defined in `database.py`)

```python
WITHDRAWAL_FEES = {
    "bank_eu":      0.010,   # 1.0%  — EU SEPA
    "bank_uk":      0.015,   # 1.5%  — UK Faster Payments
    "bank_us":      0.020,   # 2.0%  — US ACH
    "bank_swift":   0.035,   # 3.5%  — SWIFT International
    "mobile_money": 0.015,   # 1.5%  — Mobile Money
    "sohana_user":  0.000,   # 0%    — Internal Pay (always free)
}
WITHDRAWAL_FEE_MIN = 50      # Minimum 50 cents (€0.50)

CONVERSION_FEE_RATE = 0.007  # 0.7% currency conversion fee
CONVERSION_FEE_MIN  = 50     # Minimum 50 cents

ROSCA_CREATION_FEES = {
    "probation":  500,        # €5.00
    "developing": 300,        # €3.00
    "reliable":   100,        # €1.00
    "exemplary":  0,          # Free
}

ROSCA_PAYOUT_FEE = 0.0125    # 1.25% platform fee on each ROSCA payout
```

---

## 6. Frontend Architecture

### CSS design system (`sohana.css`)

The entire UI is built with a single custom CSS file. Key design tokens:

```css
/* Admin (dark theme) */
--admin-bg:      #0d1117    /* GitHub-style dark */
--admin-surface: #161b22
--admin-border:  rgba(255,255,255,.08)
--admin-text:    rgba(255,255,255,.87)
--admin-muted:   rgba(255,255,255,.45)

/* User (light theme) */
--navy:       #1a2b4a
--navy-dark:  #14213a
--blue:       #2563eb
--green:      #16a34a
--amber:      #d97706
--red:        #dc2626
--orange:     #ea580c
--gold:       #f59e0b
--purple:     #7c3aed
```

**Component classes:**
- `.stat-card`, `.astat` — metric cards
- `.rosca-card` — navy-gradient ROSCA summary cards
- `.market-card` — light marketplace cards
- `.pill`, `.apill` — status badges
- `.modal-backdrop`, `.modal` — confirmation modals
- `.otp-input` — OTP verification inputs
- `.progress-track`, `.progress-fill` — progress bars
- `.notif-item`, `.afeed-item` — notification/feed rows
- `.admin-nav`, `.sidebar-nav` — navigation lists

### JavaScript (`app.js`)

Global functions available on every page:

```javascript
API.post(url, data)          // fetch() wrapper returning JSON
API.get(url)                 // fetch() GET wrapper
toast(message, type)         // Toast notification (success/error/warning/info)
openModal(id)                // Open modal by ID
closeModal(id)               // Close modal by ID
setLoading(btn, loading)     // Spinner state on buttons
sparkline(canvasId, data, color) // Draw line sparkline chart
doLogout()                   // POST /api/auth/logout + redirect
```

### OTP flow (client-side)

All sensitive operations (withdraw, pay, convert) use a 6-digit OTP confirmation modal. In the current MVP, any 6 digits are accepted — the field is fully wired and ready for real SMS integration.

```javascript
// OTP input — auto-advances focus between boxes
function otpNext(i, el, prefix='otp') {
  if (el.value && i < 5) document.getElementById(prefix+(i+1)).focus();
}
```

### Chart rendering

Charts are drawn directly on `<canvas>` elements using the Canvas 2D API — no Chart.js, no D3. Admin dashboards use custom `drawLine()`, `drawBar()`, and `donut()` functions defined inline in each template's `<script>` block.

---

## 7. Admin System

### Role hierarchy

```
CEO (Super Admin)
├── All privileges — access to every dashboard
├── Can view and operate all admin routes
└── Only role that can manage other admins

CTO          → /admin/engineering  (system health, deployments, API performance)
CCO          → /admin/compliance   (KYC, AML, regulatory reports, risk heatmap)
Fraud        → /admin/fraud        (alerts, transaction monitoring, blacklist)
Credit       → /admin/credit       (NCS distribution, loans, default analysis)
Operations   → /admin/operations   (ROSCA activity, pending deposits/withdrawals)
Compliance   → /admin/compliance   (same as CCO)
Business     → /admin/dashboard    (platform overview)
```

### Admin login flow

```
GET  /admin/login              → admin_login.html (dark theme)
POST /api/auth/admin-login     → validates phone/email + password
                                  checks is_admin=1 in DB
                                  sets session: user_id, is_admin=True, admin_role
                                  returns {ok, role}
GET  /admin/home               → redirect based on admin_role to correct dashboard
```

### Admin seed accounts

All admin passwords are `Admin@2024`. Seeded on first launch.

| Phone | Email | Role | Dashboard |
|---|---|---|---|
| +00000000001 | kwame.mensah@sohana.app | CEO | /admin/executive |
| +00000000002 | kojo.agyeman@sohana.app | CTO | /admin/engineering |
| +00000000003 | akosua.mensah@sohana.app | CCO | /admin/compliance |
| +00000000004 | daniel.owusu@sohana.app | Fraud | /admin/fraud |
| +00000000005 | philip.mensah@sohana.app | Credit | /admin/credit |
| +00000000006 | samuel.mensah@sohana.app | Operations | /admin/operations |
| +00000000007 | ama.boateng@sohana.app | Compliance | /admin/compliance |
| +00000000008 | emmanuel.asante@sohana.app | Business | /admin/dashboard |

---

## 8. NCS Credit Scoring Engine

### Score range and tiers

| Tier | Range | Label | Creation fee | Key benefits |
|---|---|---|---|---|
| Probation | 300–549 | 🔴 Probation | €5.00 | Basic wallet, small ROSCAs |
| Developing | 550–649 | 🟡 Developing | €3.00 | Most ROSCAs, emergency loan |
| Reliable | 650–749 | 🟢 Reliable | €1.00 | All ROSCAs, early payout loan, fee discount |
| Exemplary | 750–850 | 🟣 Exemplary | Free | Max loans, credit bureau reporting |

### Score components (weighted)

```
Reliability      35%  — on-time contribution rate across all circles
Completion       25%  — full ROSCA cycles completed without default
Default Recovery 20%  — recovery behaviour after missed payments
Social Trust     10%  — peer endorsements received (log-scaled to 20)
Wallet Behaviour 10%  — deposit regularity and wallet activity (last 90 days)
```

### Event delta table

```python
EVENT_DELTAS = {
    "contribution_on_time":    +8,
    "cycle_completed":        +12,
    "contribution_recovered":  +5,
    "peer_endorsement":        +3,
    "peer_endorsement_removed":-2,
    "dispute_resolved":        +4,
    "wallet_deposit":          +1,
    "contribution_late":       -5,
    "contribution_missed":    -18,  # -9 on first miss (grace rule)
    "cycle_defaulted":        -30,
    "dispute_raised":          -8,
}
```

### Badge definitions (18 types)

| Badge | Trigger |
|---|---|
| First Step | First contribution ever |
| Hat Trick | 3 consecutive on-time payments |
| On a Roll | 5 consecutive on-time payments |
| Ironclad | 10 consecutive on-time payments |
| Unstoppable | 25 consecutive on-time payments |
| Full Circle | Completed first ROSCA cycle |
| Circle Elder | Completed 5 ROSCA cycles |
| Circle Master | Completed 10 ROSCA cycles |
| Trusted Voice | Gave 3+ endorsements |
| Well Regarded | Received 5+ endorsements |
| Community Hero | Received 20+ endorsements |
| Developing | Reached 550 NCS |
| Reliable | Reached 650 NCS |
| Exemplary | Reached 750 NCS |
| Circle Leader | Organised first circle |
| Early Bird | Paid contribution 5+ days early |
| Comeback | Recovered from a missed payment |
| Big Saver | Rotated over €5,000 total |
| Circle Hopper | Active in 3+ circles simultaneously |

### Loan eligibility gates

```
Emergency Liquidity Loan   → NCS ≥ 550 (50% of next payout)
Early Payout Loan          → NCS ≥ 650 (full pot in advance)
ROSCA-Backed Loan          → NCS ≥ 700 (collateralised)
```

---

## 9. Hanatag System

The Hanatag is a unique payment identity — `@firstname1234` — that lets users send and receive money without sharing phone numbers or bank details.

### Auto-generation logic

```python
def generate_hanatag(full_name):
    base   = re.sub(r"[^a-z0-9]", "", full_name.lower())[:12]
    suffix = "".join(random.choices(string.digits, k=4))
    return f"@{base}{suffix}"
```

### Hanatag payment flow

```
User enters @recipient in Pay modal
    ↓
GET /api/profile/lookup-hanatag?tag=@recipient
    ↓ Returns: { ok, user: { id, full_name, ncs_score } }
    ↓
System shows recipient's full name for confirmation
    ↓
User enters amount + note
    ↓
6-digit OTP entered (any digits in demo; real SMS in production)
    ↓
POST /api/wallet/pay  { hanatag, amount, note, otp }
    ↓
Debit sender wallet (tx_type=pay_out)
Credit recipient wallet (tx_type=pay_in)
Push notification to recipient
    ↓
Both transactions recorded with matching ref_id UUID
```

### Pay vs Withdraw distinction

| Feature | Pay | Withdraw |
|---|---|---|
| Destination | Another SOHANA user (@hanatag) | External bank or mobile money |
| Fee | Always free | 1.0–3.5% depending on method |
| Speed | Instant | 1–5 business days (in production) |
| Reversibility | Not reversible | Not reversible |
| OTP required | Yes | Yes |

---

## 10. Multicurrency Wallet

### Supported currencies

```python
CURRENCIES = {
    "EUR": { "symbol": "€",  "flag": "🇪🇺", "name": "Euro" },
    "GBP": { "symbol": "£",  "flag": "🇬🇧", "name": "British Pound" },
    "USD": { "symbol": "$",  "flag": "🇺🇸", "name": "US Dollar" },
    "CAD": { "symbol": "C$", "flag": "🇨🇦", "name": "Canadian Dollar" },
    "XAF": { "symbol": "Fr", "flag": "🌍",  "name": "CFA Franc" },
    "GHC": { "symbol": "₵",  "flag": "🇬🇭", "name": "Ghanaian Cedi" },
    "NGN": { "symbol": "₦",  "flag": "🇳🇬", "name": "Nigerian Naira" },
    "ZAR": { "symbol": "R",  "flag": "🇿🇦", "name": "South African Rand" },
}
```

### Exchange rates (hardcoded for MVP — live in production)

```python
EXCHANGE_RATES = {
    "EUR": 1.000,  "GBP": 0.856,  "USD": 1.085,
    "CAD": 1.468,  "XAF": 655.96, "GHC": 13.45,
    "NGN": 1620.0, "ZAR": 20.18,
}
```

### Conversion fee

- **Rate:** 0.7% of the source amount
- **Minimum:** €0.50 equivalent
- **Formula:** `fee = max(50_cents, int(from_cents * 0.007))`
- **What changes:** Production will replace hardcoded rates with a live exchange rate API (e.g. Open Exchange Rates or Wise API)

### Wallet page actions

| Action | Route | Fee | OTP |
|---|---|---|---|
| Deposit | `POST /api/wallet/deposit` | None | No |
| Withdraw | `POST /api/wallet/withdraw` | 1.0–3.5% by method | Yes |
| Pay (internal) | `POST /api/wallet/pay` | None | Yes |
| Convert | `POST /api/wallet/convert` | 0.7% | No |
| Open currency | `POST /api/wallet/open-currency` | None | No |
| Statement CSV | `GET /api/wallet/statement` | None | No |

---

## 11. ROSCA Engine

### Lifecycle states

```
forming  →  active  →  completed
              ↓
           [cancelled]
```

### Pot size rule

The pot size equals **only actual contributions received**. It starts at 0 and grows with each payment. There is no initial or placeholder pot amount.

```python
# Each contribution payment:
db.execute("UPDATE cycles SET pot_cents=pot_cents+? WHERE id=?",
           (contrib["amount_cents"], cycle_id))
```

### Creation fee by NCS tier

| Tier | Fee |
|---|---|
| Probation | €5.00 |
| Developing | €3.00 |
| Reliable | €1.00 |
| Exemplary | Free |

### Payout flow

When all members of a cycle have contributed, the cycle closes automatically:

```python
def _check_cycle_complete(cycle_id):
    pending = fetchone("SELECT COUNT(*) as c FROM contributions WHERE cycle_id=? AND status='pending'")
    if pending["c"] > 0: return
    fee = int(cycle.pot_cents * 0.0125)       # 1.25% platform fee
    net = cycle.pot_cents - fee
    post_transaction(recipient_wallet, net, "ROSCA payout")
    push_notification(recipient, "You received your payout! 🎉")
    # NCS +12 for all members (cycle_completed event)
```

---

## 12. API Reference

### Auth

| Method | Route | Auth | Body | Returns |
|---|---|---|---|---|
| POST | `/api/auth/register` | None | `{phone, full_name, password, email?, country?}` | `{ok, user_id}` |
| POST | `/api/auth/login` | None | `{phone, password}` | `{ok, user: {id, name}}` |
| POST | `/api/auth/admin-login` | None | `{email_or_phone, password}` | `{ok, role}` |
| POST | `/api/auth/logout` | Session | — | `{ok}` |

### Wallet

| Method | Route | Auth | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/api/wallet/balances` | Session | — | `{wallets: [{currency, balance_cents, balance_display, symbol, flag}]}` |
| POST | `/api/wallet/open-currency` | Session | `{currency}` | `{ok, wallet_id}` |
| POST | `/api/wallet/deposit` | Session | `{amount, currency?}` | `{ok, new_balance_cents}` |
| POST | `/api/wallet/withdraw` | Session | `{amount, method, otp, destination_name?}` | `{ok, withdrawn_cents, fee_cents}` |
| POST | `/api/wallet/pay` | Session | `{hanatag, amount, note?, otp}` | `{ok, recipient_name}` |
| POST | `/api/wallet/convert` | Session | `{from_currency, to_currency, amount}` | `{ok, from_amount_display, to_amount_display, fee_display, rate}` |
| GET | `/api/wallet/statement` | Session | — | `text/csv` file download |

### Currency

| Method | Route | Auth | Returns |
|---|---|---|---|
| GET | `/api/currency/rates` | None | `{rates: {EUR: 1.0, GBP: 0.856, ...}}` |
| GET | `/api/currency/preview-conversion?from=EUR&to=GBP&amount=100` | Session | `{to_amount, fee, rate}` |

### Profile

| Method | Route | Body | Returns |
|---|---|---|---|
| POST | `/api/profile/update` | `{full_name?, email?, bio?, language?, currency?}` | `{ok}` |
| POST | `/api/profile/hanatag` | `{hanatag}` | `{ok, hanatag}` |
| GET | `/api/profile/lookup-hanatag?tag=@name` | — | `{ok, user: {id, full_name, ncs_score}}` |
| POST | `/api/profile/payment-method` | `{method_type, label, details, is_default?}` | `{ok}` |

### Notifications

| Method | Route | Returns |
|---|---|---|
| GET | `/api/notifications` | `{notifications: [...], unread: N}` |
| POST | `/api/notifications/mark-read` | `{ok}` |

### ROSCA

| Method | Route | Body | Returns |
|---|---|---|---|
| POST | `/api/rosca/create` | `{name, description?, contribution, max_members, frequency_days, ncs_min, is_public}` | `{ok, rosca_id, creation_fee_cents}` |
| POST | `/api/rosca/:id/join` | — | `{ok}` |
| POST | `/api/rosca/:id/contribute` | — | `{ok}` |
| POST | `/api/rosca/:id/activate` | — | `{ok}` (organiser only) |
| POST | `/api/rosca/:id/start-cycle` | — | `{ok, cycle_id}` (organiser only) |
| GET | `/api/rosca/marketplace?q=search` | — | `{roscas: [...]}` |

### NCS

| Method | Route | Returns |
|---|---|---|
| GET | `/api/ncs/score` | `{score, tier, tier_label}` |
| POST | `/api/ncs/recalculate` | `{score, components}` |

### Endorsements

| Method | Route | Body | Returns |
|---|---|---|---|
| POST | `/api/endorsement` | `{user_id, rosca_id?, action: "endorse" | "unendorse"}` | `{ok, action}` |

---

## 13. Security Model

### Current (MVP)

| Layer | Implementation |
|---|---|
| Password hashing | PBKDF2-HMAC-SHA256, 260,000 iterations, 16-byte random salt |
| Session auth | Flask signed cookies, `SECRET_KEY` env var |
| SQL injection | Parameterised queries throughout — no string interpolation |
| Admin guard | `is_admin=1` + `admin_role` check on every admin route |
| Stale session protection | `login_required` verifies user exists in DB before granting access |
| OTP | Demo mode — any 6 digits accepted. Ready for SMS integration. |
| CSRF | Not implemented (MVP). Add Flask-WTF in production. |
| Rate limiting | Not implemented (MVP). Add Flask-Limiter in production. |
| HTTPS | Handled by Railway/Render at the platform level |

### Production security additions required before launch

```
1. Flask-WTF CSRF tokens on all forms
2. Flask-Limiter on auth endpoints (5 attempts per minute)
3. Real SMS OTP via Twilio or Africa's Talking
4. KYC document verification integration (Smile Identity or Onfido)
5. Fraud rule engine with database-driven rules (not just seeded data)
6. IP-based geolocation checks on login
7. Device fingerprinting for fraud detection
8. Audit log table for all admin actions
9. Data encryption at rest for sensitive fields (KYC docs, payment details)
10. PCI-DSS compliance assessment before live card payments
```

---

## 14. Demo Accounts

### Regular users (password: `demo123`)

| Phone | Name | Country | NCS Score | Tier |
|---|---|---|---|---|
| +33611000001 | Maria Ngono | France | 480 | Probation |
| +33611000002 | Samuel Eto | Cameroon | 680 | Reliable |
| +25078100001 | Alice Uwase | Rwanda | 750 | Exemplary |
| +44795000001 | Kwame Asante | UK | 560 | Developing |
| +33611000003 | Fatou Diallo | France | 390 | Probation |

All start with **€500.00** in their EUR wallet.

### Admin users (password: `Admin@2024`) — login at `/admin/login`

| Phone | Name | Role | Dashboard |
|---|---|---|---|
| +00000000001 | Kwame Mensah | CEO | /admin/executive |
| +00000000002 | Kojo Agyeman | CTO | /admin/engineering |
| +00000000003 | Akosua Mensah | CCO | /admin/compliance |
| +00000000004 | Daniel Owusu | Fraud Analyst | /admin/fraud |
| +00000000005 | Philip Mensah | Credit Officer | /admin/credit |
| +00000000006 | Samuel Mensah | Operations | /admin/operations |
| +00000000007 | Ama Boateng | Compliance Mgr | /admin/compliance |
| +00000000008 | Emmanuel Asante | Business Mgr | /admin/dashboard |

---

## 15. Scaling Roadmap

### Phase 1 — Closed Beta (current)
- SQLite on persistent volume (handles up to ~2,000 concurrent users)
- 2 Gunicorn workers
- All on a single Railway/Render instance
- Simulated payments, demo OTP

### Phase 2 — Open Beta (1,000–10,000 users)

**Database migration: SQLite → PostgreSQL**

```bash
# 1. Install psycopg2
pip install psycopg2-binary

# 2. Replace get_db() in database.py
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ["DATABASE_URL"]

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()

# 3. Change ? placeholders to %s throughout
# 4. Run on Railway with Postgres add-on ($5/month)
```

**Infrastructure changes:**
- Add Redis for session storage and task queue (Celery)
- Add Nginx reverse proxy in front of Gunicorn
- Scale to 4+ Gunicorn workers
- Add database connection pooling (pgBouncer)
- Separate static file serving (Cloudflare CDN)

### Phase 3 — Growth (10,000–100,000 users)

**Architecture evolution:**
```
[Cloudflare CDN] → [Load Balancer]
                         ↓
              [App Server 1] [App Server 2] [App Server N]
                         ↓
              [PostgreSQL Primary] → [PostgreSQL Replica(s)]
                         ↓
              [Redis Cluster] (sessions + cache + queues)
                         ↓
              [Celery Workers] (async: NCS recalculation, notifications, payouts)
```

**Code changes required:**
- Extract NCS recalculation to async Celery task (nightly batch)
- Add read replicas for admin dashboard queries
- Implement proper API versioning (`/api/v1/`)
- Add comprehensive request logging (Sentry)
- Feature flags for gradual rollouts

### Phase 4 — Scale (100,000+ users)

- Microservices extraction: Wallet Service, ROSCA Service, NCS Service, Notification Service
- Event-driven architecture (Kafka or AWS SQS) for inter-service communication
- Separate compliance and fraud services with dedicated databases
- Geographic distribution (EU + West Africa data centres for GDPR compliance)
- Real-time NCS recalculation pipeline (Apache Airflow or Prefect)
- Machine learning NCS model replacing rule-based system (see `p3_model_training.py` reference)

---

## 16. Payment Gateway Integration Plan

### Current state

All financial operations are **simulated**. The `wallet_transactions` table records debits and credits but no real money moves. The `api_withdraw()`, `api_deposit()`, and `api_pay()` functions are fully structured and ready for gateway integration.

---

### 16.1 Stripe Integration (cards + bank transfers — EU/US/UK)

**Use cases:** Card deposits, SEPA bank transfers, UK Faster Payments

**Step 1 — Install**
```bash
pip install stripe
```

**Step 2 — Environment variables**
```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

**Step 3 — Replace deposit endpoint**
```python
import stripe
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

@app.route("/api/wallet/deposit/stripe-intent", methods=["POST"])
@auth.login_required
def create_payment_intent():
    d = request.json or {}
    cents = int(float(d["amount"]) * 100)
    user = auth.get_current_user()
    intent = stripe.PaymentIntent.create(
        amount=cents,
        currency="eur",
        metadata={"user_id": user["id"]},
        automatic_payment_methods={"enabled": True},
    )
    return jsonify({"client_secret": intent["client_secret"]})

# Stripe calls this webhook when payment succeeds
@app.route("/api/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, os.environ["STRIPE_WEBHOOK_SECRET"])
    except Exception:
        return jsonify({"error": "Invalid signature"}), 400
    if event["type"] == "payment_intent.succeeded":
        pi     = event["data"]["object"]
        uid    = pi["metadata"]["user_id"]
        cents  = pi["amount_received"]
        wallet = _get_wallet(uid, "EUR")
        post_transaction(wallet["id"], cents, "Card deposit via Stripe", tx_type="deposit")
        push_notification(uid, "Deposit received ✓", f"€{cents/100:.2f} added to your wallet.")
    return jsonify({"ok": True})
```

**Step 4 — Stripe Connect for payouts**
```python
# Send ROSCA payouts to users' bank accounts via Stripe Connect
stripe.Transfer.create(
    amount=net_cents,
    currency="eur",
    destination=user_stripe_account_id,   # stored in users table
    description=f"ROSCA payout — cycle {cycle_number}",
)
```

**Stripe fees to account for:**
- European cards: 1.4% + €0.25
- Non-European cards: 2.9% + €0.25
- SEPA Direct Debit: 0.8%, capped at €5
- Payouts via Stripe Connect: 0.25% + €0.10

---

### 16.2 Africa's Talking — Mobile Money (MTN, Vodafone, Airtel)

**Use cases:** Mobile Money deposits/withdrawals for Ghana, Nigeria, Rwanda, Uganda, Kenya

**Step 1 — Install**
```bash
pip install africastalking
```

**Step 2 — Environment variables**
```
AT_API_KEY=your_api_key
AT_USERNAME=sohana
AT_ENVIRONMENT=production   # or sandbox
```

**Step 3 — Initiate Mobile Money payment**
```python
import africastalking

africastalking.initialize(os.environ["AT_USERNAME"], os.environ["AT_API_KEY"])
payment = africastalking.Payment

@app.route("/api/wallet/deposit/mobile-money", methods=["POST"])
@auth.login_required
def deposit_mobile_money():
    d = request.json or {}
    user   = auth.get_current_user()
    phone  = d.get("phone") or user["phone"]
    amount = float(d["amount"])
    currency_code = d.get("currency", "GHS")   # GHS | NGN | KES | RWF

    product_name = "SOHANA"
    providers = {
        "GHS": "Vodafone",
        "NGN": "Airtel",
        "KES": "Mpesa",
        "RWF": "MTN",
    }
    response = payment.mobile_checkout(
        product_name=product_name,
        phone_number=phone,
        currency_code=currency_code,
        amount=amount,
        metadata={"user_id": user["id"]}
    )
    # AT sends payment prompt to user's phone
    # Confirmation comes via webhook
    return jsonify({"ok": True, "status": response["status"]})

# Africa's Talking payment notification webhook
@app.route("/api/webhooks/at-payment", methods=["POST"])
def at_payment_webhook():
    data     = request.form
    status   = data.get("status")
    uid      = data.get("requestMetadata[user_id]")
    amount   = float(data.get("value", "0").replace("GHS", "").replace("NGN","").strip())
    currency_code = data.get("currencyCode","GHS")
    if status == "Success" and uid:
        # Convert to EUR cents for ledger
        rate   = EXCHANGE_RATES.get(currency_code, 1.0) / EXCHANGE_RATES["EUR"]
        cents  = int(amount / rate * 100)
        wallet = _get_wallet(uid, "EUR")
        post_transaction(wallet["id"], cents, f"Mobile Money deposit ({currency_code})", tx_type="deposit")
        push_notification(uid, "Mobile Money deposit received ✓", f"€{cents/100:.2f} added.")
    return "OK", 200
```

**Africa's Talking fees:**
- Mobile checkout: varies by country and network (typically 1–2% of transaction)
- B2C payouts: flat fee per transaction (~0.5%)

---

### 16.3 Flutterwave (pan-Africa — cards + mobile money + bank transfer)

**Use cases:** Multi-country collection and payout across 35+ African countries

**Step 1 — Install**
```bash
pip install flutterwave-python
```

**Step 2 — Environment variables**
```
FLW_PUBLIC_KEY=FLWPUBK_TEST-...
FLW_SECRET_KEY=FLWSECK_TEST-...
FLW_WEBHOOK_SECRET=your_webhook_secret
```

**Step 3 — Initiate payment (hosted checkout)**
```python
import requests

@app.route("/api/wallet/deposit/flutterwave", methods=["POST"])
@auth.login_required
def flutterwave_deposit():
    d    = request.json or {}
    user = auth.get_current_user()
    ref  = str(uuid.uuid4())
    payload = {
        "tx_ref":       ref,
        "amount":       d["amount"],
        "currency":     d.get("currency","EUR"),
        "redirect_url": "https://yourdomain.com/wallet?deposit=success",
        "customer": {
            "email":      user["email"] or f"{user['id']}@sohana.app",
            "phonenumber": user["phone"],
            "name":       user["full_name"],
        },
        "customizations": {
            "title":       "SOHANA Deposit",
            "description": "Top up your SOHANA wallet",
        },
        "meta": {"user_id": user["id"]},
    }
    r = requests.post(
        "https://api.flutterwave.com/v3/payments",
        json=payload,
        headers={"Authorization": f"Bearer {os.environ['FLW_SECRET_KEY']}"},
    )
    link = r.json()["data"]["link"]
    return jsonify({"ok": True, "payment_url": link})

# Flutterwave webhook
@app.route("/api/webhooks/flutterwave", methods=["POST"])
def flw_webhook():
    secret = request.headers.get("verif-hash")
    if secret != os.environ["FLW_WEBHOOK_SECRET"]:
        return "Forbidden", 403
    data   = request.json
    if data.get("event") == "charge.completed" and data["data"]["status"] == "successful":
        uid    = data["data"]["meta"]["user_id"]
        amount = int(float(data["data"]["amount"]) * 100)
        wallet = _get_wallet(uid, "EUR")
        post_transaction(wallet["id"], amount, "Flutterwave deposit", tx_type="deposit")
        push_notification(uid, "Deposit confirmed ✓", f"€{amount/100:.2f} added to your wallet.")
    return "OK", 200
```

---

### 16.4 Wise API (international payouts + live exchange rates)

**Use cases:** EUR, GBP, USD payouts to bank accounts; live exchange rate data

```python
# Replace hardcoded EXCHANGE_RATES with live Wise rates
import requests

def get_live_rates(base="EUR"):
    r = requests.get(
        f"https://api.wise.com/v1/rates?source={base}",
        headers={"Authorization": f"Bearer {os.environ['WISE_API_KEY']}"}
    )
    return {rate["target"]: rate["rate"] for rate in r.json()}

# Cache with Redis (update every 15 minutes)
# from redis import Redis
# r = Redis.from_url(os.environ["REDIS_URL"])
# r.setex("exchange_rates", 900, json.dumps(get_live_rates()))
```

---

### 16.5 Recommended stack by market

| Market | Collection | Payouts | Exchange Rates |
|---|---|---|---|
| EU/UK/US | Stripe | Stripe Connect | Wise API |
| West Africa (GH/NG/SN) | Flutterwave | Flutterwave Transfer | Flutterwave Rates |
| East Africa (KE/UG/RW) | Africa's Talking | Africa's Talking B2C | Flutterwave Rates |
| Universal fallback | Flutterwave | Flutterwave | Open Exchange Rates |

---

### 16.6 Integration checklist

Before connecting any gateway to real money:

```
□ Move SECRET_KEY and all API keys to environment variables (never commit to git)
□ Implement real SMS OTP (Twilio or Africa's Talking SMS)
□ Add KYC verification (Smile Identity, Onfido, or Sumsub)
□ Add Flask-WTF CSRF protection
□ Add Flask-Limiter rate limiting on auth + payment endpoints
□ Test all webhook endpoints with the gateway's test mode
□ Set up Sentry for error monitoring
□ Enable HTTPS (automatic on Railway/Render)
□ Register as a Money Service Business (MSB) in your operating jurisdiction
□ Obtain EMI (Electronic Money Institution) licence or partner with a licensed EMI
□ Implement transaction monitoring rules in the fraud_alerts table
□ Add PCI-DSS compliant card data handling (use hosted fields — never touch raw card numbers)
□ Run penetration test before public launch
```

---

## 17. Production Deployment

### Railway (recommended)

```bash
# 1. Push to GitHub
git init && git add . && git commit -m "SOHANA v3"
git remote add origin https://github.com/YOUR_USERNAME/sohana.git
git push -u origin main

# 2. Go to railway.app → New Project → Deploy from GitHub

# 3. Add environment variables in Railway dashboard:
SECRET_KEY=your-64-char-random-string
DATABASE_PATH=/data/sohana.db

# 4. Add Volume: mount path /data

# 5. Deploy → live URL in ~2 minutes
```

### Environment variables required

| Variable | Example | Notes |
|---|---|---|
| `SECRET_KEY` | `sohana-prod-xyz...` | 64+ random chars |
| `DATABASE_PATH` | `/data/sohana.db` | Path on persistent volume |
| `STRIPE_SECRET_KEY` | `sk_live_...` | When Stripe is connected |
| `AT_API_KEY` | `...` | Africa's Talking |
| `FLW_SECRET_KEY` | `FLWSECK_...` | Flutterwave |
| `WISE_API_KEY` | `...` | Wise for live rates |
| `TWILIO_ACCOUNT_SID` | `AC...` | Real OTP |
| `TWILIO_AUTH_TOKEN` | `...` | Real OTP |
| `SENTRY_DSN` | `https://...` | Error monitoring |

### Connecting Claude for ongoing development

Since this project was built by Claude (Anthropic), you can continue development by:

1. Sharing the GitHub repository URL in the Claude conversation
2. Describing the bug or feature needed
3. Claude generates the fix and provides the updated file contents
4. Push the updated file to GitHub → Railway auto-redeploys in 60 seconds

All user data stays in the Railway persistent volume. Only the code changes.

---

*Document generated: April 2026*  
*Platform version: SOHANA v3*  
*Built with: Python 3.12, Flask 3.1, SQLite3, Vanilla JS/CSS*
