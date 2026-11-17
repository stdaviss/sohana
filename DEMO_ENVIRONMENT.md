# SOHANA demo environment (demo.sohana.app)

The real SOHANA app, running as a **second Railway service** from the `demo`
branch, with its **own database**. Nothing done there touches sohana.app.

Everything demo-specific switches on only when the service has `DEMO_MODE=1`
(see `demo_mode.py`). Without that variable the code behaves exactly like the
live site, so the branch can later be merged without side effects.

## What DEMO_MODE adds
- `/` and `/demo`: welcome page (English/French). Name + optional email creates a
  real account in the demo database, signed in, with a @hanatag, verified KYC and
  simulated balances (EUR 500, RWF 300,000).
- Payment codes are shown on screen and pre-filled (no SMS or email is sent).
- A "Demo · simulated money" tag on every page.
- Live updates: signed-in pages refresh when the visitor's balance, NCS, circles or
  notifications change.
- Booth mode: open `https://demo.sohana.app/demo?booth=1` once on a booth device;
  it signs out after 2.5 minutes untouched and returns to the welcome page.
- App look: `static/css/demo-theme.css` and `static/js/demo-ui.js` restyle the signed-in
  screens (dark theme, one accent, line icons instead of emoji, plain labels, "Limits & fees"
  panel, in-app banner when money or circle activity arrives). Loaded only in DEMO_MODE.
- `/demo/live`: booth screen for a laptop or TV facing the crowd. Live counters and a feed of
  what visitors are doing (first name + initial only), with the QR code to join.
- `/admin/demo`: control room for the team (live sign-ups, balances, NCS, "Credit"
  top-ups, CSV export of names, emails and news consent).

## Railway variables for the demo service
| Variable | Value |
| --- | --- |
| `DEMO_MODE` | `1` |
| `DATABASE_PATH` | `/data/sohana.db` (on the demo service's own volume) |
| `SECRET_KEY` | a new random string (not the live one) |
| `ADMIN_SEED_PASSWORD` | the password for the demo admin logins |

Do **not** add SendGrid, Twilio or Google sign-in variables to the demo service.

Admin login: `https://demo.sohana.app/admin/login`, phone `+00000000001`, the
`ADMIN_SEED_PASSWORD` above. Then open `/admin/demo`.
