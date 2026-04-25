# SOHANA — Where Trust Becomes Capital

A full-stack ROSCA (Rotating Savings & Credit Association) fintech platform with integrated Njangi Credit Score (NCS) engine.

## Features

- **ROSCA engine** — create and join savings circles with automated contribution tracking and payout rotation
- **Njangi Credit Score (NCS)** — behavioral credit score (300–850) built from contribution history across all 5 components
- **Digital wallet** — append-only ledger, deposit/withdraw, instant internal transfers
- **Marketplace** — browse and join public circles, filter by amount/size/NCS requirement
- **Organiser dashboard** — member management, cycle control, contribution heatmap
- **Gamification** — badges, tier progression, endorsements, score events
- **Loan eligibility** — NCS-gated financial products unlocked progressively

## Demo accounts

| Phone | Password | NCS Score | Country |
|-------|----------|-----------|---------|
| +33611000001 | demo123 | 480 (Probation) | France |
| +33611000002 | demo123 | 680 (Reliable) | Cameroon |
| +25078100001 | demo123 | 750 (Exemplary) | Rwanda |
| +44795000001 | demo123 | 560 (Developing) | UK |

## Local development

```bash
# 1. Clone / unzip the project
cd sohana

# 2. Install dependencies (Python 3.10+)
pip install -r requirements.txt

# 3. Run the development server
python app.py

# Open http://localhost:5000
```

The database (`sohana.db`) is created automatically on first run with demo seed data.

## Deploy to Railway (recommended — free tier available)

1. Push this folder to a GitHub repository
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variable: `SECRET_KEY` = any random 64-char string
5. Add a Volume: mount path `/data`, then set `DATABASE_PATH=/data/sohana.db`
6. Deploy — Railway auto-detects Python and uses `Procfile`

Total deploy time: ~3 minutes.

## Deploy to Render (free tier available)

1. Push to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your repo
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`
6. Add disk: mount at `/opt/render/project/src`, 1 GB
7. Set env var `SECRET_KEY`

Or use the included `render.yaml` for one-click deploy via Render Blueprints.

## Deploy to Fly.io

```bash
brew install flyctl
fly auth login
fly launch          # auto-detects Python, creates fly.toml
fly volumes create sohana_data --size 1
fly secrets set SECRET_KEY=$(openssl rand -hex 32)
fly deploy
```

## Project structure

```
sohana/
├── app.py                  # Flask app, all routes, API endpoints
├── database.py             # SQLite schema, helpers, ledger functions
├── ncs_engine.py           # NCS scoring — components, tiers, events, badges
├── auth.py                 # Registration, login, session management
├── rosca.py                # ROSCA engine — create, join, contribute, cycle rotation
├── requirements.txt        # Flask + gunicorn only
├── Procfile                # For Railway / Heroku
├── render.yaml             # For Render one-click deploy
├── railway.toml            # For Railway config
├── static/
│   ├── css/sohana.css      # Full SOHANA design system
│   └── js/app.js           # API helpers, sparklines, toasts, modals
└── templates/
    ├── base.html           # Shared layout, nav, scripts
    ├── landing.html        # Public landing page
    ├── auth.html           # Login + register (tabs)
    ├── dashboard.html      # Main dashboard — wallet, circles, NCS card
    ├── circles.html        # Marketplace + my circles
    ├── circle_detail.html  # Individual circle — contribute, members, cycle
    ├── ncs.html            # Full NCS breakdown, history, tips, loans
    └── organiser.html      # Organiser dashboard — manage members, cycles
```

## Tech stack

- **Backend**: Python 3.10+ / Flask 3.1
- **Database**: SQLite3 (zero-config, file-based, upgradeable to PostgreSQL)
- **Frontend**: Vanilla HTML/CSS/JS — no framework, no build step
- **Design**: SOHANA brand system (Indigo Blue #0D2A5C, Gold #C6A85B, Forest Green #1F3D2B)
- **Auth**: Server-side sessions, PBKDF2 password hashing
- **Hosting**: Any Python host — Railway, Render, Fly.io, VPS

## Upgrading to PostgreSQL (when you scale)

Replace `database.py` with a psycopg2-backed version and update `DATABASE_URL`. The schema in `p1_schema_and_ingestion.sql` (from the NCS module docs) is the PostgreSQL equivalent — run it directly against your Postgres instance.

## Next steps toward production

1. Add proper payment gateway (Stripe or Flutterwave) to replace demo wallet deposits
2. Integrate SMS OTP for phone verification (Twilio or Africa's Talking)
3. Enable the ML-based NCS model from `p3_model_training.py` when you have 500+ users
4. Set up Airflow or cron for the nightly recalculation pipeline (`p6_retraining_pipeline.py`)
5. Add GDPR-compliant data export/deletion endpoint
6. Switch SQLite → PostgreSQL once monthly active users exceed ~5,000
