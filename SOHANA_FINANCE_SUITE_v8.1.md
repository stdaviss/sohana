# SOHANA Finance Suite — v8.1 Handover

**Date:** 2026-07-17
**Scope:** Frontend rebuild + admin-panel entry point. **No backend changes.**
**Author context:** Continuation of v7.11 (gated dashboards) → v8.0 (portfolio CRUD backend) → **v8.1 (this release: correct sidebar wiring + full SOHANA palette migration + Micro Mutual redesign + Treasury Engine).**

---

## 1. What shipped

Four ready-to-`cp` files. Every file is complete and copy-pasteable — no hand-editing required.

| File | Destination | Status |
|---|---|---|
| `base.html` | `templates/base.html` | Edited — added gated Finance Suite sidebar section only |
| `finance_treasury_engine.html` | `templates/finance_treasury_engine.html` | **Built fresh** (see §5) |
| `finance_micro_mutual.html` | `templates/finance_micro_mutual.html` | Redesigned from your 1,771-line upload; all JS preserved |
| `finance_portfolios.html` | `templates/finance_portfolios.html` | Palette/font migration only; all CRUD preserved |
| `SOHANA_FINANCE_SUITE_v8.1.md` | (docs, anywhere) | This file |

**`app.py` is unchanged** — the backend contract (routes, API shapes, decorator, DB tables) was already correct in v8.0 and was not touched.

---

## 2. The three problems fixed

**(1) Missing sidebar entry point.** The previous agent baked navigation *into each dashboard* but never added an entry point to the admin sidebar, so there was no way to reach the suite from the admin panel. Fixed by adding a single gated block to `base.html` (lines ~272–281):

```jinja
{% if session.get('admin_role') in ['ceo','cfo','cto','compliance'] %}
  ... Treasury Engine / Micro Mutual / Client Portfolios ...
{% endif %}
```

The gate matches `FINANCE_SUITE_ROLES = {"ceo","cfo","cto","compliance"}` exactly. Active state is derived from `request.path` (via the existing `{% set p = request.path %}` pattern), consistent with every other link in the sidebar. Icons use the same monochrome geometric glyph style as the rest of the admin nav (◈ ◇ ⬢) — no emoji.

**(2) Wrong palette.** All three finance templates were navy (`#07111D` / `#060B12`) + gold (`#D4AF37`) + Inter / IBM Plex. Migrated to SOHANA design tokens:

- bg `#0E120F`, surfaces `#161B17` / `#1C231D`
- text `#F4F2EC` (warm cream — never pure white), muted `#B7BAB4` / `#8A8E87`
- accent mint `#9EE493`, accent-2 `#CEF870`
- danger `#FF6A55`, warn `#FFB350`, info `#67E8F9`
- fonts Geist / Geist Mono / **Instrument Serif italic** for display accents
- **Gold `#D4AF37` retained in exactly one place** — the XAU / metal-reserve context in the Treasury Engine (documented inline as a reserved token). No other gold survives anywhere.

**(3) Micro Mutual redesign.** Rebuilt visuals from your uploaded `Sohana_Micro_Mutual_Fund.html` while preserving **all 8 views** (dashboard / config / planner / strategy / simulator / scenario / members / reports / settings) and **all 36 JavaScript functions** verbatim — including `fetchRate`, `fetchFXPanel`, `fetchMarketSnapshot`, `runEngine`, and `switchView`. Only color literals, fonts, chrome glyphs (emoji → geometric), the risk legend (🟢🟡🔴 → colored `●` spans using `--positive` / `--warning` / `--negative`), and the theme toggle (🌙/☀️ → ☾/☼) changed. Logic untouched.

---

## 3. Architecture — why two navigations, both correct

The finance templates **render standalone** (they do *not* `{% extends "base.html" %}`) — they are self-contained terminals with their own chrome. That is by design and is respected here. So the suite needs two distinct navigation surfaces:

- **(a) Entry point** — the gated section added to `base.html`. This is how an authorised admin *enters* the suite from the main admin panel. This is the piece that was missing.
- **(b) In-suite cross-nav** — each finance page keeps a restyled top/cross nav to move between the three tools and exit back to `/admin/dashboard`.

The previous agent built only (b), with the wrong palette, and skipped (a). v8.1 delivers both, correctly themed.

---

## 4. Backend contract — untouched and verified

Confirmed present and unchanged (do not modify):

- **Routes:** `/admin/finance/treasury`, `/admin/finance/micro-mutual`, `/admin/finance/portfolios` — all behind `@_finance_suite_required`, all render standalone templates.
- **API:** `GET/POST /api/finance/portfolios`; `GET/PUT/DELETE /api/finance/portfolios/<pid>`; `POST /api/finance/portfolios/<pid>/refresh`; `POST /api/finance/portfolios/<pid>/holdings`; `DELETE /api/finance/holdings/<hid>`; `GET /api/finance/risk-profiles`.
- **Decorator:** `_finance_suite_required` (uses `session.get("admin_role")`, `FINANCE_SUITE_ROLES`).
- **Tables:** `finance_portfolios`, `finance_portfolio_holdings`, `finance_portfolio_snapshots`.

`finance_portfolios.html` still references all nine API paths, preserves the `{{ session.user_id }}` Jinja injection in its JS, and keeps the full create/read/update/delete/refresh/holdings flow intact.

---

## 5. Note on the Treasury Engine

The source for `finance_treasury_engine.html` was **not in the upload set** for this session. It was rebuilt from the documented "Capital State Engine" spec (capital-state flow: incoming / reserved / idle / deployed / returning; liquidity buckets T+0/7/30/90; lending book with provision coverage and expected loss; reserves including the XAU gold line; deployment summary linking to Micro Mutual; live event-bus feed with count-up animation). It is self-contained, uses SOHANA tokens, carries a clearly labelled *simulated / internal projection* banner, and has no external JS dependencies.

**If you still have the original Treasury JS**, send it and it will be ported 1:1 into this shell so the live wiring matches your prior implementation exactly — the visual layer will not need to change.

---

## 6. Validation performed

- `base.html`: Jinja `if/endif`, `for/endfor`, `block/endblock` all balanced; finance gate + all three links present.
- No colored emoji in any finance template (only intentional monochrome glyphs: ◈ ◇ ⬢ ⬡ ☰ ☾ ☼ ✕ ★).
- Zero `IBM Plex`, zero `Inter`, zero navy hexes across all three templates.
- Gold `#D4AF37` appears **only** as the XAU reserve token in Treasury.
- SOHANA bg `#0E120F` and Instrument Serif present in all three.
- All three templates are standalone (no `extends`).
- Micro Mutual: 36/36 JS functions preserved; `fetchRate` / `fetchFXPanel` / `fetchMarketSnapshot` / `runEngine` / `switchView` confirmed.

---

## 7. Deploy

See the single copy-paste block provided with this handover (`cp` the four templates into `templates/`, then `git add` / `commit` / `push`). No migration and no `app.py` change are required.
