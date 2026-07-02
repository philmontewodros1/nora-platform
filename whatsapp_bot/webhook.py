"""
LEGACY / OPTIONAL — superseded by backend/whatsapp_router.py (the production path,
a webhook mounted into the main app). This standalone version is kept only for quick
local testing. It does NOT include payment, track, or rider commands — use the router
for anything real. Do not deploy this separately.

Nora WhatsApp bot - built on Meta's WhatsApp Cloud API (free tier available,
but requires business verification and template approval before going live —
see the README for the full setup checklist).

This is a webhook server: Meta sends incoming messages here as HTTP POST requests.
Run:  uvicorn webhook:app --host 0.0.0.0 --port 8001
"""
import os
import logging
import requests
from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nora-whatsapp")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "nora-verify-me")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")            # permanent access token from Meta
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")  # from Meta app dashboard
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
GRAPH_API = "https://graph.facebook.com/v20.0"

app = FastAPI(title="Nora WhatsApp Webhook")

# In-memory session state per WhatsApp number (swap for a real DB/cache in production)
sessions: dict[str, dict] = {}


@app.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta calls this once when you register the webhook URL in the App Dashboard."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("Verification failed", status_code=403)


def send_whatsapp_text(to: str, body: str):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.info("WhatsApp not configured yet — would send to %s: %s", to, body)
        return
    url = f"{GRAPH_API}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException:
        log.exception("Failed to send WhatsApp message")


def send_whatsapp_buttons(to: str, body: str, buttons: list[tuple[str, str]]):
    """buttons: list of (id, title) — WhatsApp allows a max of 3 reply buttons."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.info("WhatsApp not configured yet — would send buttons to %s: %s %s", to, body, buttons)
        return
    url = f"{GRAPH_API}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": bid, "title": title[:20]}} for bid, title in buttons[:3]
            ]},
        },
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException:
        log.exception("Failed to send WhatsApp buttons")


@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        messages = entry.get("messages")
        if not messages:
            return {"ok": True}  # status callbacks (delivered/read) land here too — ignore for now

        msg = messages[0]
        from_number = msg["from"]
        session = sessions.setdefault(from_number, {"stage": "start"})

        if msg["type"] == "text":
            text = msg["text"]["body"].strip().lower()
            await handle_customer_text(from_number, text, session)
        elif msg["type"] == "interactive":
            reply = msg["interactive"]["button_reply"]["id"]
            await handle_customer_button(from_number, reply, session)

    except (KeyError, IndexError):
        log.warning("Unrecognized webhook payload shape: %s", data)

    return {"ok": True}


async def handle_customer_text(from_number: str, text: str, session: dict):
    if session["stage"] == "await_address":
        session["address"] = text
        session["stage"] = "confirm"
        send_whatsapp_buttons(
            from_number,
            f"Confirm: {session['product']} {session['size']}, deliver to {text}. Place order?",
            [("confirm_yes", "Confirm and pay"), ("confirm_no", "Cancel")]
        )
        return

    # default / greeting
    session["stage"] = "product"
    send_whatsapp_buttons(
        from_number,
        "Selam! Welcome to Nora. What do you need today?",
        [("product_gas", "Cooking gas"), ("product_water", "Water jar"), ("product_butane", "Butane")]
    )


async def handle_customer_button(from_number: str, reply_id: str, session: dict):
    if reply_id.startswith("product_"):
        session["product"] = reply_id.split("_", 1)[1]
        session["stage"] = "size"
        sizes = {"gas": ["6kg", "12kg", "22kg"], "water": ["jar"], "butane": ["canister"]}[session["product"]]
        if len(sizes) == 1:
            session["size"] = sizes[0]
            session["stage"] = "exchange"
            send_whatsapp_buttons(from_number, "Do you have an empty one to exchange?",
                                   [("exchange_yes", "Yes"), ("exchange_no", "No, first order")])
        else:
            send_whatsapp_buttons(from_number, "Which size?",
                                   [(f"size_{s}", s) for s in sizes])
        return

    if reply_id.startswith("size_"):
        session["size"] = reply_id.split("_", 1)[1]
        session["stage"] = "exchange"
        send_whatsapp_buttons(from_number, "Do you have an empty one to exchange?",
                               [("exchange_yes", "Yes"), ("exchange_no", "No, first order")])
        return

    if reply_id.startswith("exchange_"):
        session["is_exchange"] = reply_id.endswith("yes")
        session["stage"] = "await_address"
        send_whatsapp_text(from_number, "What's your delivery address?")
        return

    if reply_id == "confirm_yes":
        payload = {
            "customer_phone": from_number,
            "telegram_id": None,
            "whatsapp_id": from_number,
            "address_text": session.get("address"),
            "product": session["product"],
            "brand": None,
            "size": session["size"],
            "is_exchange": session.get("is_exchange", True),
            "quantity": 1,
        }
        try:
            r = requests.post(f"{BACKEND_URL}/orders", json=payload, timeout=10)
            r.raise_for_status()
            order = r.json()
            send_whatsapp_text(from_number, f"Order #{order['id']} placed. Total: {order['total_price']:.0f} ETB.")
        except requests.RequestException:
            send_whatsapp_text(from_number, "Couldn't place the order — please try again shortly.")
        sessions.pop(from_number, None)
        return

    if reply_id == "confirm_no":
        send_whatsapp_text(from_number, "Order cancelled. Send any message to start again.")
        sessions.pop(from_number, None)
        return


@app.get("/")
def root():
    return {"status": "Nora WhatsApp webhook is running"}
