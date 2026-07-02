# Nora Platform — Final System Report

*Compiled after full build, deployment, and safety hardening. No secrets, tokens, passwords, or connection strings appear below.*

---

## Executive summary

A multi-channel gas/water refill delivery platform for Addis Ababa, built end-to-end, deployed live on free-tier infrastructure, and safety-hardened. All three customer channels (web, Telegram, WhatsApp) share one backend and one persistent database. Tested locally and in production at every stage. **All pre-launch safety blockers closed and verified live.**

---

## 1. Architecture

Single deployable FastAPI service + external Postgres. Three front-ends (web admin, Telegram webhook, WhatsApp webhook) all call the same backend; the backend is the single source of truth. Order-flow orchestration lives in one shared `services.py` module so the HTTP API and both bot routers never duplicate business rules.

```
nora-platform/
├── backend/
│   ├── main.py              HTTP API + routers + static serving + /admin/security-check
│   ├── services.py          Order-flow orchestration (single source of truth)
│   ├── crud.py              DB ops: distance matching, stock, rider re-offer, earnings
│   ├── models.py            SQLAlchemy models (Shop, StockItem, Customer, Rider, Order)
│   ├── schemas.py           Pydantic schemas
│   ├── pricing.py           ETB prices (env-configurable)
│   ├── payments.py          Payment provider plumbing (stub swap point)
│   ├── payments_guard.py    Safety guard: cash-only until PAYMENTS_LIVE + secrets set
│   ├── security_checks.py   Startup warnings + admin-visible safety status endpoint
│   ├── notifications.py     Telegram + WhatsApp push + admin alerts
│   ├── ratelimit.py         In-memory order rate limiter
│   ├── geo.py               Haversine + Maps deep-link helpers
│   ├── database.py          Engine (SQLite/Postgres via DATABASE_URL)
│   ├── seed.py              Seeds sample shops + riders
│   ├── telegram_router.py   Telegram webhook (customers + riders, enhanced UX)
│   ├── whatsapp_router.py   WhatsApp webhook (customers + riders)
│   ├── .python-version      Pins 3.11.9
│   └── requirements.txt     (incl. psycopg2-binary for Postgres)
├── admin_web/index.html     Single-file admin dashboard
├── landing.html             Marketing landing (3D Three.js scene)
├── README.md, DEPLOY-FREE-ONLINE.md, PROMPT_FOR_YOUR_AI.md, SYSTEM-REPORT.md
└── telegram_bot/, whatsapp_bot/   (legacy standalone, optional)
```

---

## 2. Live deployment

| Component | Provider | Plan | Status |
|---|---|---|---|
| App (API + admin + bots + landing) | Render | Free web service | Live, auto-deploys on push to `main` |
| Database | Neon | Free Postgres (US-East) | Live, persistent across redeploys (proven) |
| Telegram bot | BotFather | Free | `@Noraeth_bot`, webhook → Render |
| Landing (CDN) | Surge | Free | Live on `*.surge.sh` |
| Source | GitHub | Public repo | `philmontewodros1/nora-platform` |

**Deploy mechanism:** push to `main` → Render auto-builds → starts with `seed.py` (idempotent) + `uvicorn`. Python pinned to 3.11.9 (avoids a pydantic-core source-build failure on 3.14). Cold-start ~30–60s after 15 min idle (free tier).

---

## 3. Security posture (post-hardening)

| Control | State | Verified |
|---|---|---|
| Admin credentials | **Real, non-default** (set in Render env) | Old defaults → 401; new creds → 200 |
| Payment false-paid guard | **Closed** — online methods only offered/marked paid when `PAYMENTS_LIVE=true` + provider webhook secret set | TeleBirr order on live → `paid=False` |
| Admin alert channel | **Live** — `ADMIN_TELEGRAM_CHAT_ID` set | Security-check warning cleared |
| HTTP Basic Auth on admin | Enforced on all `/admin/*` routes | 401 without creds |
| CORS | Configurable via `ALLOWED_ORIGINS` env | Defaults to `*` (tighten for production) |
| Order rate limit | 5/min per phone+IP | Threshold verified: 5 succeed, 6th → 429 |
| Secrets in git | None — `.env` gitignored, never committed | Token literal in 0 `.py` files; `.env` absent from remote tree |
| `/admin/security-check` | Admin-only endpoint returning live safety status | Returns correct warnings (currently: 1 info note = cash-only mode) |

**Security-check output (current live state):**
- ~~CRITICAL: default admin creds~~ → resolved
- ~~WARNING: SQLite resets~~ → resolved (Neon)
- ~~WARNING: alert chat ID unset~~ → resolved
- `INFO: cash-only mode active` → correct, stays until real payment integration

---

## 4. Feature status

### Implemented and verified (local + production)
- **Order lifecycle**: create → distance-based shop match → price → auto-offer to on-duty rider → accept/decline (decline re-offers) → pickup (decrements stock, optional remaining-stock log + mismatch flag) → deliver (swap vs. charge-deposit). Cash-on-delivery marks paid on delivery; first-time orders apply refundable deposit.
- **Distance-based matching**: nearest shop by haversine when lat/lng available; fallback to highest-quantity.
- **Payment guard**: cash-only offered until `PAYMENTS_LIVE` + provider secrets set; `mark_paid_is_safe()` hard-stops any code path marking online methods paid prematurely.
- **Phone capture**: Telegram requests contact (button) or accepts a typed number (Ethiopian-format normalized); WhatsApp uses customer number directly. Phone is the cross-channel identifier.
- **Location pin**: Telegram customers can share location → feeds distance matching.
- **Channels**: Telegram (full customer + rider flows, enhanced UX) and WhatsApp (full customer + rider flows, list messages for >3 options, Meta verification handshake).
- **Admin dashboard** (browser-verified): stats, filterable live orders + re-offer, create shop/rider, stock table + update, rider duty toggle, deposit reconciliation, reports (revenue by category, shop/rider performance, avg delivery time).
- **Rider tools**: `/onduty` `/offduty`, Accept/Decline buttons + Google Maps deep links, `/pickedup<id> [<stock>]`, `/delivered<id>`, `/earnings`, `/myorders`.
- **Notifications**: customer status updates; rider new-order alerts with Maps links; admin alert channel for 3+ stock mismatches and 20+ min stalled deliveries.
- **UX polish**: typing indicators, HTML formatting with escaping, itemized order summary with shop name, honest no-stock-nearby check before payment (verified: 22kg → 0 phantom orders), `/help`, refresh-status button, friendly error fallback.
- **Landing page**: 3D scene, responsive, reduced-motion aware, served at `/` and on Surge.
- **Database persistence**: proven across redeploys (rider + order survived full rebuild on Neon).

### Placeholders (clearly flagged, not hidden)
- **Real payment collection**: `payments.py` is the swap point — returns a fake reference; the guard ensures no order is marked paid for online methods until a real provider is wired. Cash-on-delivery is real and safe. **Wiring TeleBirr/CBE requires their merchant API docs** (not publicly available). Until then, set `PAYMENTS_LIVE=true` + `TELEBIRR_WEBHOOK_SECRET`/`CBE_WEBHOOK_SECRET` only after real integration.
- **Prices**: illustrative figures from the business plan (gas 12kg = 1,700 ETB; delivery = 150; total = 1,850 ETB). Replace with pilot prices; keep spec/landing in sync.
- **WhatsApp production**: code-complete, but Meta business verification + pre-approved message templates (for >24h notifications) required before real customers.
- **Sessions**: in-memory per instance (fine for single free instance; move to DB if scaled).

---

## 5. Verification performed

**Local:**
- Full order lifecycle via API, Telegram webhook simulation, and WhatsApp webhook simulation.
- No-stock edge case (22kg) → 0 phantom orders.
- Rate limiter threshold (5/5 then 429).
- Payment guard: telebirr → `paid=False`; cash-only methods offered.
- Security-check endpoint returns correct warnings.
- Secrets: 0 token literals in `.py`; `.env` not in git.
- Admin dashboard rendered in a real browser with live data.

**Production (live URL):**
- `/health` 200, `/shops` from Neon, `/admin` loads, admin auth works (old rejected, new accepted).
- Live order created (1,850 ETB, matched to nearest shop).
- Telegram webhook pointed at Render; `@Noraeth_bot` replied to a real account end-to-end (typed phone captured, order created, rider accept/deliver).
- **Persistence proven**: rider + order survived a full redeploy on Neon.
- **Payment hard stop proven live**: telebirr order → `paid=False`.
- **Security-check**: only the correct cash-only info note remains.

---

## 6. Configuration (Render env vars)

| Variable | Set? | Purpose |
|---|---|---|
| `DATABASE_URL` | ✅ | Neon Postgres connection string |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot API access |
| `ADMIN_USER` | ✅ | Non-default admin username |
| `ADMIN_PASS` | ✅ | Non-default admin password |
| `ADMIN_TELEGRAM_CHAT_ID` | ✅ | Admin alert destination |
| `ALLOWED_ORIGINS` | Default (`*`) | Tighten to real domain for production |
| `PAYMENTS_LIVE` | Unset (false) | Set to `true` only after real provider wired |
| `TELEBIRR_WEBHOOK_SECRET` | Unset | Provider callback secret (later) |
| `CBE_WEBHOOK_SECRET` | Unset | Provider callback secret (later) |
| `WHATSAPP_*` | Unset | Meta WhatsApp Cloud API (later) |
| `NORA_DELIVERY_FEE` | Default (150) | Configurable delivery fee |
| `NORA_DEFAULT_ETA_MINUTES` | Default (45) | Configurable ETA |

No secrets appear in the repo, in logs, or in this report. All credentials live only as Render env vars / local gitignored `.env`.

---

## 7. Known operational limits

- **Free-tier cold start**: Render sleeps after 15 min idle; first request takes ~30–60s. $7/mo Starter removes it.
- **Single instance**: in-memory sessions + rate limiter are per-process; acceptable at pilot scale.
- **Payment is cash-only**: safe and real, but online payment isn't available until provider integration.
- **WhatsApp not customer-ready**: needs Meta business verification + template approval.
- **Prices are illustrative**: replace before real orders.

---

## 8. How to check status anytime

**Admin dashboard:** `https://nora-platform.onrender.com/admin` (HTTP Basic Auth with the real credentials set in Render).

**Safety status:** `https://nora-platform.onrender.com/admin/security-check` (same auth) — returns a plain JSON list of what's still unsafe. Currently: one INFO note confirming cash-only mode (correct).

**Health:** `https://nora-platform.onrender.com/health` — no auth, returns `{"status": "Nora API is running"}`.

---

**Bottom line:** a complete, tested, free-tier-deployed delivery platform with a persistent database and all pre-launch safety blockers structurally closed — not just documented. Safe to be reachable by people other than the owner (cash-only, real admin auth, alert channel live). The two remaining steps before real money flows are wiring a real TeleBirr/CBE merchant API (needs their docs) and swapping in real pilot prices.
