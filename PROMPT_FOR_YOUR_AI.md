# Prompt for your AI coding assistant

Copy everything below this line into your AI coding assistant (Claude Code, Cursor, or similar),
along with this entire project folder. It gives the assistant full context to pick up the build,
finish any rough edges, and help you deploy.

---

## Project context

I'm building **Nora**, a gas and water refill delivery platform for Addis Ababa, Ethiopia. The
business model: small independent gas/water refill shops already exist all over the city but are
disconnected from each other. Nora connects customers to the nearest shop with stock, uses riders
to fetch a full cylinder/jar and swap it for the customer's empty one at the doorstep (an exchange
model, not manufacture-and-deliver), and returns the empty to the shop afterward.

The project is a **working, tested platform**, deployable entirely for free as a **single web
service** (see `DEPLOY-FREE-ONLINE.md` — read it before deploying anything; Render's free tier
changed and no longer includes free background workers):

- A FastAPI backend (SQLite locally, swappable to free permanent Postgres via Neon) that is the
  single source of truth for orders, shops, stock, riders, customers, pricing, and payments.
- Telegram bot logic implemented as a **webhook** (`backend/telegram_router.py`), mounted directly
  into the main app — not a separate always-running process.
- WhatsApp Cloud API logic also implemented as a webhook (`backend/whatsapp_router.py`), mounted
  the same way.
- A plain HTML/JS admin dashboard (no build step) served by the backend at `/admin`.
- A marketing landing page (`landing.html`) served by the backend at `/` (apex URL).

All of this is **ONE deployable app** (`backend/main.py` mounts both routers and serves the
landing + admin). One free Render web service covers the whole platform. The `telegram_bot/` and
`whatsapp_bot/` folders still contain the original standalone polling/webhook versions — those are
now optional, useful only for local testing without a public URL. Don't deploy those separately;
the merged versions in `backend/` are the production path.

## Project structure

```
backend/
  main.py            FastAPI app — all HTTP routes (orders, shops, riders, admin, payment,
                     accept/decline) + mounts the two routers + serves landing/admin.
  telegram_router.py Telegram bot as a webhook (customer + rider flows).
  whatsapp_router.py WhatsApp bot as a webhook (customer + rider flows).
  services.py        Order-flow orchestration (place/accept/decline/pickup/deliver) — the single
                     source of truth shared by the HTTP API and both routers.
  crud.py            DB operations: distance-based shop matching, stock decrement + mismatch
                     tracking, rider re-offer, earnings.
  models.py          SQLAlchemy models (Shop, StockItem, Customer, Rider, Order) incl. payment
                     and lifecycle timestamps.
  schemas.py         Pydantic request/response schemas.
  pricing.py         ETB prices (match the spec: gas 12kg = 1,700, delivery = 150, total = 1,850).
  payments.py        Payment plumbing — STUB swap point for TeleBirr/CBE Birr (see below).
  notifications.py   Telegram + WhatsApp push: rider new-order alerts (with Maps deep links +
                     Accept/Decline), customer status, admin alert channel, stalled-delivery check.
  ratelimit.py       Tiny in-memory rate limiter for order creation.
  geo.py             Haversine distance + Google Maps directions URL helpers.
  database.py        DB connection (SQLite by default, swappable to Postgres via DATABASE_URL).
  seed.py            Seeds sample shops (with lat/lng) + riders for local testing.
  requirements.txt
  .env.example

admin_web/
  index.html         Single-file admin dashboard: orders w/ filters, shops + stock, riders,
                     deposits, reports. HTTP Basic Auth.

landing.html         Marketing landing page (3D Three.js scene). Served at /.

telegram_bot/        OPTIONAL legacy standalone polling bot (local testing only).
whatsapp_bot/        OPTIONAL legacy standalone WhatsApp webhook (local testing only).

README.md                       Original setup guide (deployment section superseded — see DEPLOY).
DEPLOY-FREE-ONLINE.md           Current, accurate free deployment guide — READ THIS FIRST.
PROMPT_FOR_YOUR_AI.md           This file.
```

## What's verified working (all tested end to end)

- **Order lifecycle** via the API, the Telegram webhook, and the WhatsApp webhook: create →
  matched to the nearest shop with stock (distance-based via lat/lng) → priced (1,850 ETB for gas
  12kg, matching the spec) → auto-offered to an on-duty rider → rider **accept/decline** (decline
  re-offers to the next rider) → **pickup** (decrements stock, optionally logs remaining count and
  flags a mismatch) → **deliver** (swap-complete vs. no-empty-charge-deposit). Cash-on-delivery
  orders are marked paid on delivery; first-time orders apply the refundable deposit.
- **Payment plumbing**: TeleBirr/CBE mark an order paid on initiation; cash stays unpaid until
  delivery; a `/payments/webhook` endpoint receives provider callbacks (success/fail). This is a
  clearly-marked **stub** — see "still placeholder" below.
- **Phone-number capture**: the Telegram bot requests the user's contact (`request_contact`) and
  stores the real phone (the shared cross-channel identifier); WhatsApp uses the customer's number
  automatically.
- **Location capture**: Telegram customers can share their location (pin), which feeds the
  distance-based shop matching.
- **Admin dashboard** (browser-verified): login, stats (orders/riders/pending/revenue), live
  orders with status/product/rider/shop filters + re-offer, create shop, update stock, all-stock
  table, create rider, toggle duty, deposit reconciliation, and reports (revenue by category,
  shop/rider performance, avg delivery time).
- **Rider tools**: `/onduty` `/offduty`, Accept/Decline buttons, `/pickedup<id> [<stock>]`,
  `/delivered<id>`, `/earnings`, `/myorders` — on both Telegram and WhatsApp.
- **Notifications**: rider new-order alerts include Accept/Decline buttons + Google Maps deep
  links to shop and customer; customer gets status updates; admin alert channel fires on 3+
  consecutive stock mismatches and on stalled (20+ min over ETA) deliveries.
- **Security**: admin endpoints behind HTTP Basic Auth (401 without, 200 with); CORS configurable
  via `ALLOWED_ORIGINS`; order creation rate-limited per phone (5/min).
- **WhatsApp webhook verification** echoes Meta's challenge token; wrong token → 403.

## What's still placeholder (be honest about these before going live)

1. **Real payment collection.** `payments.py` is the single swap point: `initiate_payment` /
   `verify_payment` currently return a fake reference and mark orders paid without an actual
   TeleBirr/CBE Birr charge. Wire the real merchant API there (the rest of the flow keeps working
   unchanged). You need merchant-account docs from your provider rep — they're not public.
2. **Prices.** `pricing.py` still uses the illustrative figures from the business plan (gas 12kg
   1,700, etc.). Replace with real shop prices from the pilot. If you change one here, update the
   spec doc's example copy and the landing page cards to match.
3. **Admin alert channel.** `ADMIN_TELEGRAM_CHAT_ID` is unset — create a Telegram channel, add the
   bot as admin, and set the chat id for stock-mismatch / stalled-delivery alerts to fire.
4. **WhatsApp production readiness.** WhatsApp works end to end in code, but Meta must verify the
   business before it works for anyone beyond test numbers, and notifications sent >24h after the
   customer's last message need pre-approved templates (spec section 3.4). Telegram has none of
   these limits — launch Telegram first.
5. **Multi-instance sessions.** The Telegram/WhatsApp in-memory session dicts are fine for a
   single free-tier instance; if you scale to multiple workers, move sessions to the DB.
6. **Landing page contact details.** `landing.html` has the real Telegram bot username wired in
   (`@Noraeth_bot`), but the WhatsApp number (`wa.me/251900000000`) is still a placeholder — set
   your real WhatsApp business number there before launch.

## How I want you to work

- Keep the existing file structure and naming — don't restructure into a different framework
  unless I ask.
- The backend is the single source of truth; the routers and admin dashboard should only ever talk
  to it via `services.py` / the HTTP API, never touch the DB directly (the routers already use
  `services.py` — keep it that way).
- Everything should keep working on free hosting tiers (Render free web service + Neon free
  Postgres) — flag it clearly if a change you're proposing would require paid infrastructure.
- After any change to `backend/`, re-run the test flow to confirm nothing broke:
  ```bash
  cd backend
  set -a; source .env; set +a   # or export the env vars manually
  rm -f nora.db && python3 seed.py
  uvicorn main:app --port 8000 &
  sleep 4
  curl -X POST http://localhost:8000/orders -H "Content-Type: application/json" -d '{
    "customer_phone": "+251911223344", "customer_name": "Test", "product": "gas",
    "size": "12kg", "is_exchange": true, "quantity": 1, "payment_method": "telebirr",
    "latitude": 9.01, "longitude": 38.76
  }'
  curl http://localhost:8000/orders/1
  ```
  A successful response includes a `shop_id`, `total_price: 1850.0`, `paid: true`, and
  `status: "assigned"`.
- Read `DEPLOY-FREE-ONLINE.md` for the deployment steps (Render + Neon, all free tier) before
  suggesting alternative hosting — I want to launch for free first and upgrade only once there's
  real order volume.

Please start by reviewing the codebase, confirming you understand the order flow end to end, then
ask me which of the "still placeholder" items I want to tackle first.
