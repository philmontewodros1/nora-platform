# Deploying Nora online for free (2026)

This replaces the deployment section in the original README — Render changed their free tier
partway through this build (free background workers were removed), so the architecture below
is different from earlier drafts. Read this version.

## What changed and why

Render's free tier no longer includes free background workers (they're $7/month minimum now),
and Render's free Postgres database auto-deletes after 30 days. Both of those broke the original
"backend + separate Telegram worker + Render Postgres" plan.

**Fix:** the Telegram bot and WhatsApp bot were rebuilt as webhooks (`backend/telegram_router.py`
and `backend/whatsapp_router.py`) instead of a separate always-running process. They're mounted
directly into the main FastAPI app (`backend/main.py`), so the entire platform — order API, admin
dashboard, Telegram bot, WhatsApp bot — now deploys as **one single free web service**. For the
database, use Neon instead of Render Postgres — Neon's free tier is permanent (no 30-day
deletion) and scales to zero when idle, so it costs nothing even if the app goes quiet overnight.

## The stack (all free)

| Piece | Where | Cost |
|---|---|---|
| Backend + admin dashboard + Telegram bot + WhatsApp bot | Render, 1 free web service | $0 |
| Database | Neon, free tier | $0 |
| Telegram bot | Free forever, no approval needed | $0 |
| WhatsApp bot | Free within Meta's monthly conversation allowance | $0 to start |

## Step 1 — Database on Neon

1. Go to https://neon.tech, sign up (no credit card needed).
2. Create a new project. Copy the connection string it gives you — it looks like
   `postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require`.
3. Keep this open, you'll paste it into Render in step 2.

## Step 2 — Deploy the backend to Render

1. Push this whole project to a GitHub repository if you haven't already.
2. Go to https://render.com, sign up (no credit card needed for the free tier).
3. **New > Web Service**, connect your GitHub repo.
4. Set the root directory to `backend`.
5. Build command: `pip install -r requirements.txt`
6. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
7. Under **Environment**, add these variables:
   - `DATABASE_URL` — the Neon connection string from step 1
   - `TELEGRAM_BOT_TOKEN` — from @BotFather (see step 3)
   - `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` — from Meta (see step 4, can leave blank until you set WhatsApp up)
   - `ADMIN_USER`, `ADMIN_PASS` — pick real credentials, don't leave the defaults
8. Deploy. Render gives you a URL like `https://nora-abc123.onrender.com`.

**Free tier behavior to expect:** the service spins down after 15 minutes with no traffic and
takes 30-60 seconds to wake up on the next request. For a pilot with occasional orders, that's a
fine tradeoff for $0/month. If it becomes a real problem once you have live customers, that's the
signal to move to Render's $7/month Starter plan, which removes the spin-down — not before.

## Step 3 — Connect Telegram

1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts, get your token.
2. Add it as `TELEGRAM_BOT_TOKEN` in Render's environment variables (step 2.7), redeploy if needed.
3. Register the webhook by visiting this URL once in your browser (it'll prompt for the admin
   username/password you set in step 2.7):
   ```
   https://your-render-url.onrender.com/telegram/set-webhook?url=https://your-render-url.onrender.com/telegram/webhook
   ```
4. Message your bot on Telegram and send `/start`. You should see the ordering flow — this now
   runs entirely on Render, no local machine involved.

**To register a rider**, the admin dashboard now has a "Riders > Add a rider" form — open
`/admin`, log in, and add them there (name, phone, Telegram ID, WhatsApp ID). Find a Telegram
user's numeric ID by having them message **@userinfobot**. Then the rider sends `/onduty` to your
bot to start receiving order alerts.

## Step 4 — Connect WhatsApp (do this after Telegram is working)

WhatsApp needs a public HTTPS URL to register a webhook against, which you now have from step 2.

1. Create a Meta Developer account at https://developers.facebook.com, create an app, add the
   WhatsApp product.
2. In WhatsApp > API Setup, get a temporary access token and test phone number ID.
3. Add `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` to Render's environment variables, redeploy.
4. In the Meta app dashboard, set the webhook URL to
   `https://your-render-url.onrender.com/whatsapp/webhook` and the verify token to match
   `WHATSAPP_VERIFY_TOKEN`.
5. For real customers (not just test numbers), submit for business verification in the Meta
   dashboard — budget a few days for approval.

## What you no longer need

The `telegram_bot/` and `whatsapp_bot/` folders from the original build are now optional —
they're standalone polling/webhook versions useful only if you want to run the bot locally on
your own machine without a public URL, for quick testing. The production path is entirely inside
`backend/` now (`telegram_router.py` and `whatsapp_router.py`), deployed as the single Render
service above. You can delete the old folders once you've confirmed the merged version works, or
just leave them — they don't cost anything sitting unused in the repo.

## Confirming everything actually works after deploy

```bash
curl https://your-render-url.onrender.com/health
curl https://your-render-url.onrender.com/shops
```
Both should return JSON immediately (after the cold-start delay if the service was asleep). The
apex URL (`https://your-render-url.onrender.com/`) serves the marketing landing page in a browser;
the admin dashboard is at `/admin`. Then message your Telegram bot and confirm an order appears at
`https://your-render-url.onrender.com/admin`.
