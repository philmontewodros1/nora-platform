"""
WhatsApp bot, mounted into the same backend app as the Telegram router and the
order API (one free web service total).

Same customer flow as Telegram, adapted to WhatsApp's interactive message types:
reply buttons (max 3) for short option sets, and list messages when there are
more than 3 options (the main menu has 4: gas / water / butane / track).

Phone number is the customer's WhatsApp number (`from`), which is the shared
identifier across channels — no separate phone-capture step needed on WhatsApp.

Setup: see DEPLOY-FREE-ONLINE.md. Meta must verify the business before this
works for real customers (not just test numbers).
"""
import os
import requests
from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

import models
import schemas
import services
import crud
import pricing
from database import SessionLocal

router = APIRouter()

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "nora-verify-me")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_API = "https://graph.facebook.com/v20.0"

SIZES = {"gas": ["6kg", "12kg", "22kg"], "water": ["jar"], "butane": ["canister"]}
PRODUCT_LABELS = {"gas": "Cooking gas", "water": "Water jar", "butane": "Butane canister"}

# In-memory session state per WhatsApp number.
sessions: dict[str, dict] = {}


def _wa_post(payload: dict):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        return  # not configured — skip silently (local dev)
    try:
        requests.post(f"{GRAPH_API}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, json=payload, timeout=10)
    except requests.RequestException:
        pass


def send_text(to: str, body: str):
    _wa_post({"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}})


def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]):
    """buttons: list of (id, title) — WhatsApp allows a max of 3, titles <= 20 chars."""
    _wa_post({"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body[:1024]},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": bid, "title": t[:20]}} for bid, t in buttons[:3]
        ]},
    }})


def send_list(to: str, body: str, header: str, rows: list[tuple[str, str, str]]):
    """rows: list of (id, title, description). Use when there are more than 3 options."""
    _wa_post({"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": {
        "type": "list", "header": {"type": "text", "text": header[:60]},
        "body": {"text": body[:1024]},
        "action": {"button": "Choose", "sections": [{
            "title": "Options",
            "rows": [{"id": rid, "title": t[:24], "description": d[:72]} for rid, t, d in rows[:10]],
        }]},
    }})


@router.get("/whatsapp/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/whatsapp/webhook")
async def receive_message(request: Request):
    data = await request.json()
    db = SessionLocal()
    try:
        try:
            entry = data["entry"][0]["changes"][0]["value"]
            messages = entry.get("messages")
            if not messages:
                return {"ok": True}  # status callbacks (delivered/read) land here too
            msg = messages[0]
            from_number = msg["from"]
            session = sessions.setdefault(from_number, {"stage": "start"})

            if msg["type"] == "text":
                text = msg["text"]["body"].strip()
                await handle_text(from_number, text, session, db)
            elif msg["type"] == "interactive":
                interactive = msg["interactive"]
                if "list_reply" in interactive:
                    await handle_reply(from_number, interactive["list_reply"]["id"], session, db)
                elif "button_reply" in interactive:
                    await handle_reply(from_number, interactive["button_reply"]["id"], session, db)
        except (KeyError, IndexError):
            pass
    finally:
        db.close()
    return {"ok": True}


def welcome(to: str):
    """Main menu as a list message (4 options exceeds the 3-button limit)."""
    send_list(to, "Selam! Welcome to Nora. What do you need today?", "Nora",
              [("product_gas", "Cooking gas", "12kg / 6kg / 22kg refill"),
               ("product_water", "Water jar", "20L water delivery"),
               ("product_butane", "Butane canister", "Portable butane"),
               ("track", "Track my order", "Check your last order")])


async def handle_text(from_number: str, text: str, session: dict, db: Session):
    # --- rider text commands (a rider's WhatsApp number is their id) ---
    low = text.lower()
    if low in ("/onduty", "/offduty"):
        await rider_duty(from_number, low, db)
        return
    if low.startswith("/pickedup"):
        await rider_pickedup(from_number, text, db)
        return
    if low.startswith("/delivered"):
        await rider_delivered_prompt(from_number, text, db)
        return
    if low == "/earnings":
        await rider_earnings_cmd(from_number, db)
        return
    if low == "/myorders":
        await rider_my_orders(from_number, db)
        return
    if low.startswith("/accept"):
        await rider_accept_decline_text(from_number, text, db, accept=True)
        return
    if low.startswith("/decline"):
        await rider_accept_decline_text(from_number, text, db, accept=False)
        return

    # --- customer flow: free text is the delivery address ---
    if session.get("stage") == "await_address":
        session["address"] = text
        session["stage"] = "payment"
        _ask_payment(from_number, session)
        return

    session["stage"] = "product"
    welcome(from_number)


async def handle_reply(from_number: str, reply_id: str, session: dict, db: Session):
    # --- rider accept / decline buttons (sent by notify_rider_whatsapp) ---
    if reply_id.startswith("accept_") or reply_id.startswith("decline_"):
        accept = reply_id.startswith("accept_")
        order_id = int(reply_id.split("_")[1])
        order = db.query(models.Order).get(order_id)
        rider = db.query(models.Rider).filter(models.Rider.whatsapp_id == from_number).first()
        if order and rider:
            if accept:
                if order.rider_id == rider.id:
                    services.accept_order(db, order, rider)
                    send_text(from_number, f"Order #{order.id} accepted. Reply /pickedup{order.id} once collected.")
                else:
                    send_text(from_number, "This order was offered to another rider.")
            else:
                services.decline_order(db, order, rider)
                send_text(from_number, f"Order #{order.id} declined. Re-offered to the next rider.")
        else:
            send_text(from_number, "Order or rider not found.")
        return

    # --- delivery swap buttons ---
    if reply_id.startswith("deliver_"):
        _, order_id, empty_returned = reply_id.split("_")
        order = db.query(models.Order).get(int(order_id))
        if order:
            services.deliver_order(db, order, empty_returned=(empty_returned == "yes"))
            send_text(from_number, f"Order #{order_id} completed. Nice work!")
        return

    # --- customer ordering ---
    if reply_id == "track":
        await track_order(from_number, db)
        return

    if reply_id.startswith("product_"):
        product = reply_id.split("_", 1)[1]
        session["product"] = product
        opts = SIZES[product]
        if len(opts) == 1:
            session["size"] = opts[0]
            session["stage"] = "exchange"
            send_buttons(from_number, "Do you have an empty one to exchange?",
                         [("exchange_yes", "Yes"), ("exchange_no", "No, first order")])
        else:
            session["stage"] = "size"
            send_buttons(from_number, "Which size?", [(f"size_{s}", s) for s in opts])
        return

    if reply_id.startswith("size_"):
        session["size"] = reply_id.split("_", 1)[1]
        session["stage"] = "exchange"
        send_buttons(from_number, "Do you have an empty one to exchange?",
                     [("exchange_yes", "Yes"), ("exchange_no", "No, first order")])
        return

    if reply_id.startswith("exchange_"):
        session["is_exchange"] = reply_id.endswith("yes")
        session["stage"] = "await_address"
        send_text(from_number, "What's your delivery address? (neighborhood + landmark)")
        return

    if reply_id.startswith("pay_"):
        session["payment_method"] = reply_id.split("_", 1)[1]
        await place_order(from_number, session, db)
        return


def _ask_payment(to, session):
    send_buttons(to, f"{price_summary(session)}\n\nHow would you like to pay?", [
        ("pay_telebirr", "TeleBirr"), ("pay_cbe", "CBE Birr"), ("pay_cash", "Cash on delivery"),
    ])


def price_summary(d: dict) -> str:
    product_price = pricing.price_for(d["product"], d.get("size") or "")
    delivery = pricing.DELIVERY_FEE
    deposit = 0 if d.get("is_exchange", True) else pricing.deposit_for(d["product"])
    total = product_price + delivery + deposit
    lines = [f"{PRODUCT_LABELS[d['product']]} {d.get('size') or ''} - {product_price:.0f} ETB.",
             f"Delivery - {delivery:.0f} ETB."]
    if deposit:
        lines.append(f"Deposit (first-time) - {deposit:.0f} ETB.")
    lines.append(f"Total - {total:.0f} ETB.")
    return "\n".join(lines)


async def place_order(to, session, db):
    payload = schemas.OrderCreate(
        customer_phone=to, whatsapp_id=to,
        address_text=session.get("address"),
        product=session["product"], size=session.get("size"),
        is_exchange=session.get("is_exchange", True), quantity=1,
        payment_method=session.get("payment_method"),
    )
    try:
        order = services.place_order(db, payload, rate_key=f"wa:{to}")
    except Exception as e:
        send_text(to, f"Couldn't place the order ({e}). Try again shortly.")
        return
    session["last_order_id"] = order.id
    rider = db.query(models.Rider).get(order.rider_id) if order.rider_id else None
    rider_name = rider.name if rider else "A rider"
    if order.payment_method == models.PaymentMethod.cash:
        send_text(to, f"Order #{order.id} placed. Pay {order.total_price:.0f} ETB cash on delivery. "
                      f"{rider_name} is on the way to pick up your order.")
    else:
        send_text(to, f"Paid. {rider_name} is on the way to pick up your cylinder. "
                      f"Order #{order.id} - total {order.total_price:.0f} ETB.")
    sessions.pop(to, None)


async def track_order(from_number, db):
    session = sessions.get(from_number, {})
    order_id = session.get("last_order_id")
    if not order_id:
        cust = db.query(models.Customer).filter(models.Customer.whatsapp_id == from_number).first()
        if cust:
            last = db.query(models.Order).filter(models.Order.customer_id == cust.id)\
                .order_by(models.Order.created_at.desc()).first()
            order_id = last.id if last else None
    if not order_id:
        send_text(from_number, "No recent order found. Send a message to start one.")
        return
    o = db.query(models.Order).get(order_id)
    send_text(from_number, f"Order #{o.id} - status: {o.status}, total {o.total_price:.0f} ETB, "
                            f"paid: {'yes' if o.paid else 'no'}.")


# ---------------- Rider flow (text commands) ----------------

def _rider_for(db: Session, wa_number: str):
    return db.query(models.Rider).filter(models.Rider.whatsapp_id == wa_number).first()


async def rider_duty(from_number, text, db):
    rider = _rider_for(db, from_number)
    if not rider:
        send_text(from_number, "You're not registered as a rider yet. Ask an admin to add you.")
        return
    rider.on_duty = (text == "/onduty")
    db.commit()
    send_text(from_number, "You're now on duty." if rider.on_duty else "You're now off duty.")


async def rider_accept_decline_text(from_number, text, db, accept: bool):
    import re
    nums = re.findall(r"\d+", text)
    if not nums:
        send_text(from_number, f"Use /{'accept' if accept else 'decline'}<order id>  e.g. /{'accept' if accept else 'decline'}12")
        return
    order_id = int(nums[0])
    order = db.query(models.Order).get(order_id)
    rider = _rider_for(db, from_number)
    if not order or not rider:
        send_text(from_number, "Order or rider not found.")
        return
    if accept:
        if order.rider_id != rider.id:
            send_text(from_number, "This order was offered to another rider.")
            return
        services.accept_order(db, order, rider)
        send_text(from_number, f"Order #{order_id} accepted. Reply /pickedup{order_id} once collected.")
    else:
        services.decline_order(db, order, rider)
        send_text(from_number, f"Order #{order_id} declined. Re-offered to the next rider.")


async def rider_pickedup(from_number, text, db):
    import re
    parts = text.replace("/pickedup", "").strip().split()
    nums = re.findall(r"\d+", parts[0]) if parts else []
    if not nums:
        send_text(from_number, "Use /pickedup<order id>  (optionally /pickedup12 13 to log remaining stock).")
        return
    order_id = int(nums[0])
    remaining = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    order = db.query(models.Order).get(order_id)
    if not order:
        send_text(from_number, f"Order #{order_id} not found.")
        return
    services.pickup_order(db, order, remaining_stock=remaining)
    send_text(from_number, f"Order #{order_id} picked up. Reply /delivered{order_id} when swapped.")


async def rider_delivered_prompt(from_number, text, db):
    import re
    nums = re.findall(r"\d+", text)
    if not nums:
        send_text(from_number, "Use /delivered<order id>  e.g. /delivered12")
        return
    order_id = nums[0]
    send_buttons(from_number, "Did the customer have an empty to exchange?", [
        (f"deliver_{order_id}_yes", "Yes, swap complete"), (f"deliver_{order_id}_no", "No, deposit")])
    return


async def rider_earnings_cmd(from_number, db):
    rider = _rider_for(db, from_number)
    if not rider:
        send_text(from_number, "You're not registered as a rider yet.")
        return
    e = crud.rider_earnings(db, rider.id, days=7)
    send_text(from_number, f"Earnings (last {e['period_days']} days): {e['period_earnings_etb']:.0f} ETB "
                            f"from {e['delivered_count']} deliveries. Today: {e['today_earnings_etb']:.0f} ETB.")


async def rider_my_orders(from_number, db):
    rider = _rider_for(db, from_number)
    if not rider:
        send_text(from_number, "You're not registered as a rider yet.")
        return
    orders = db.query(models.Order).filter(
        models.Order.rider_id == rider.id,
        models.Order.status.in_([models.OrderStatus.assigned, models.OrderStatus.picked_up]),
    ).order_by(models.Order.created_at.desc()).all()
    if not orders:
        send_text(from_number, "No active orders. You're all caught up.")
        return
    send_text(from_number, "Your active orders:\n" + "\n".join(
        f"#{o.id} - {o.product} {o.size or ''} - {o.status}" for o in orders))
