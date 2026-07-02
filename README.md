# Nora — gas & water refill delivery platform

Nora connects customers in Addis Ababa to the nearest partner shop with stock, uses riders to
fetch a full cylinder/jar and swap it for the customer's empty one at the doorstep, and returns
the empty to the shop. Customers and riders can order, track, and manage deliveries on **Telegram**
or **WhatsApp**; admins work from a **web dashboard**. All three channels talk to one backend.

## What's in this folder

```
backend/        FastAPI app — the single source of truth. Serves the order API, the admin
                dashboard, the Telegram webhook, the WhatsApp webhook, and the landing page.
                ONE deployable service.
admin_web/      Admin dashboard — plain HTML/JS, no build step. Served at /admin.
landing.html    Marketing landing page (3D scene). Served at / (apex URL).
telegram_bot/   OPTIONAL legacy standalone polling bot — local testing only, not deployed.
whatsapp_bot/   OPTIONAL legacy standalone WhatsApp webhook — local testing only, not deployed.
DEPLOY-FREE-ONLINE.md   READ FIRST — current, accurate free deployment guide.
PROMPT_FOR_YOUR_AI.md   Briefs another AI coding assistant on the project (handoff doc).
```

## Run it locally (5 minutes)

You need Python 3.10+.

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add your TELEGRAM_BOT_TOKEN from @BotFather
python3 seed.py                   # creates the DB with sample shops + riders
uvicorn main:app --reload --port 8000
```

Then open:
- http://localhost:8000/         — landing page
- http://localhost:8000/admin    — admin dashboard (log in with `ADMIN_USER`/`ADMIN_PASS` from `.env`, defaults `admin`/`changeme`)
- http://localhost:8000/health   — JSON health check

Test the API directly:
```bash
curl -X POST http://localhost:8000/orders -H "Content-Type: application/json" -d '{
  "customer_phone":"+251911223344","customer_name":"Test","product":"gas","size":"12kg",
  "is_exchange":true,"quantity":1,"payment_method":"telebirr","latitude":9.01,"longitude":38.76
}'
```

## Features

- **Ordering** on Telegram and WhatsApp (button/list flows): product → size → exchange or
  first-time → address or location pin → pay (TeleBirr / CBE Birr / cash on delivery).
- **Distance-based shop matching** (nearest shop with stock, via lat/lng) with fallback to
  highest-quantity when no location is shared.
- **Rider flow**: new-order alert with Accept/Decline + Google Maps deep links, pickup (with
  optional remaining-stock logging), delivery (swap-complete vs. charge-deposit), `/earnings`,
  `/myorders`, `/onduty` `/offduty`.
- **Payments**: TeleBirr/CBE mark orders paid on initiation; cash on delivery marks paid on
  delivery; `/payments/webhook` for provider callbacks. *(Stub — see below.)*
- **Admin dashboard**: live orders with filters + re-offer, shop/stock management, rider
  management, deposit reconciliation, and reports (revenue by category, shop/rider performance,
  average delivery time).
- **Notifications**: customer status updates, rider alerts with Maps links, and an admin Telegram
  alert channel for 3+ stock mismatches and stalled (20+ min over ETA) deliveries.
- **Security**: HTTP Basic Auth on admin, configurable CORS, rate-limited order creation.

## Deploy online for free

**Live now:** the platform is deployed at **https://nora-platform.onrender.com**
(API + admin dashboard at `/admin` + landing page at `/`), with the Telegram bot
`@Noraeth_bot` wired via webhook, backed by a permanent free **Neon** Postgres
database (so data survives redeploys).

**Read [`DEPLOY-FREE-ONLINE.md`](DEPLOY-FREE-ONLINE.md)** — it's the accurate, current guide
(Render changed their free tier partway through this build, so the original three-service plan no
longer works for free). Summary: deploy `backend/` as one Render free web service, use Neon for
the free permanent Postgres database, set the Telegram webhook via `/telegram/set-webhook`, then
connect WhatsApp afterward. The whole platform — API, admin, both bots, landing page — runs as
that one service.

## Before real customers use this

- Change `ADMIN_USER` / `ADMIN_PASS` from the defaults.
- Wire real TeleBirr/CBE Birr collection into `backend/payments.py` (the stub swap point) — right
  now orders are marked paid without an actual charge going through.
- Replace the placeholder prices in `backend/pricing.py` with real shop prices (and keep the spec
  doc's example copy and the landing page cards in sync).
- Set `ADMIN_TELEGRAM_CHAT_ID` (create a Telegram channel, add the bot as admin) for admin alerts.
- Replace the placeholder WhatsApp number in `landing.html` (`wa.me/251900000000`).
- Tighten `ALLOWED_ORIGINS` to your real domain(s).
- Confirm LPG delivery licensing requirements with Ethiopia's Petroleum and Energy Authority
  before operating at real volume.

## Hand off to an AI assistant

See [`PROMPT_FOR_YOUR_AI.md`](PROMPT_FOR_YOUR_AI.md) — it briefs another AI on the full project so
it can pick up exactly where this build left off.
