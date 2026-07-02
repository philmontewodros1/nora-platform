"""
Telegram bot logic, as a webhook mounted into the main FastAPI app (one free
web service total). Handles customers AND riders in the same bot.

Customer flow: /start -> (share phone) -> product -> size -> exchange -> address
               -> price summary + [Pay with TeleBirr]/[CBE]/[Cash] -> track.
Rider flow:    /onduty /offduty, accept:/decline: buttons, /pickedup<id> [<stock>],
               /delivered<id>, /earnings, /myorders.

The router only handles presentation (buttons, message copy). All order logic
lives in services.py / crud.py, which the HTTP API also uses.
"""
import os
import requests
from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

import models
import schemas
import services
import crud
import pricing
from database import SessionLocal

router = APIRouter()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SIZES = {"gas": ["6kg", "12kg", "22kg"], "water": ["jar"], "butane": ["canister"]}
PRODUCT_LABELS = {"gas": "Cooking gas", "water": "Water jar", "butane": "Butane canister"}
PAYMENT_LABELS = {"telebirr": "Pay with TeleBirr", "cbe": "CBE Birr", "cash": "Cash on delivery"}

# In-memory session state per Telegram chat ID. Fine for a single-instance free
# web service; if you ever scale to multiple instances, move this to the DB.
sessions: dict[str, dict] = {}


def tg_call(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10)
    except requests.RequestException:
        pass


def send_text(chat_id, text, buttons=None, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    elif keyboard:
        payload["reply_markup"] = keyboard
    tg_call("sendMessage", payload)


def edit_text(chat_id, message_id, text, buttons=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    tg_call("editMessageText", payload)


def answer_callback(callback_id):
    tg_call("answerCallbackQuery", {"callback_query_id": callback_id})


def btn(text, data):
    return {"text": text, "callback_data": data}


def contact_keyboard():
    return {"keyboard": [[{"text": "Share my number", "request_contact": True}]],
            "one_time_keyboard": True, "resize_keyboard": True}


def location_keyboard():
    return {"keyboard": [[{"text": "Share my location", "request_location": True}]],
            "one_time_keyboard": True, "resize_keyboard": True}


def price_summary(d: dict) -> str:
    product_price = pricing.price_for(d["product"], d.get("size") or "")
    delivery = pricing.DELIVERY_FEE
    deposit = 0 if d.get("is_exchange", True) else pricing.deposit_for(d["product"])
    total = product_price + delivery + deposit
    size_str = d.get("size") or ""
    lines = [f"{PRODUCT_LABELS[d['product']]} {size_str} — {product_price:.0f} ETB.",
             f"Delivery — {delivery:.0f} ETB."]
    if deposit:
        lines.append(f"Deposit (first-time) — {deposit:.0f} ETB.")
    lines.append(f"Total — {total:.0f} ETB.")
    return "\n".join(lines)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    db = SessionLocal()
    try:
        if "message" in update:
            await handle_message(update["message"], db)
        elif "callback_query" in update:
            await handle_callback(update["callback_query"], db)
    finally:
        db.close()
    return {"ok": True}


# ---------------- Customer flow ----------------

async def handle_message(message: dict, db: Session):
    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    user = message.get("from", {})
    session = sessions.setdefault(chat_id, {"stage": "product", "user": user})

    # --- rider commands (work regardless of customer session stage) ---
    if text in ("/onduty", "/offduty"):
        await rider_duty(chat_id, text, db)
        return
    if text.startswith("/pickedup"):
        await rider_pickedup(chat_id, text, db)
        return
    if text.startswith("/delivered"):
        await rider_delivered_prompt(chat_id, text, db)
        return
    if text.startswith("/stock"):
        await rider_stock(chat_id, text, db)
        return
    if text == "/earnings":
        await rider_earnings_cmd(chat_id, db)
        return
    if text == "/myorders":
        await rider_my_orders(chat_id, db)
        return

    # --- customer flow ---
    if text == "/start":
        sessions[chat_id] = {"stage": "await_phone", "user": user}
        send_text(chat_id, "Selam! Welcome to Nora. To order, please share your phone number "
                           "(we use it to identify your orders across Telegram, WhatsApp, and the web).",
                  keyboard=contact_keyboard())
        return

    if text == "/track":
        await track_order(chat_id, db)
        return

    # contact shared (phone capture)
    if "contact" in message:
        phone = message["contact"].get("phone_number")
        if phone:
            session["phone"] = _normalize_phone(phone)
            session["user"] = user
        session["stage"] = "product"
        send_text(chat_id, "What do you need today?", [
            [btn("Cooking gas", "product:gas")],
            [btn("Water jar", "product:water")],
            [btn("Butane canister", "product:butane")],
            [btn("Track my last order", "track")],
        ])
        return

    # location shared (address pin)
    if "location" in message and session.get("stage") == "await_address":
        session["lat"] = message["location"]["latitude"]
        session["lng"] = message["location"]["longitude"]
        session["address"] = f"pin {session['lat']:.5f},{session['lng']:.5f}"
        session["stage"] = "payment"
        _ask_payment(chat_id, session)
        return

    # free text mid-flow: the delivery address
    if session.get("stage") == "await_address":
        session["address"] = text
        session["stage"] = "payment"
        _ask_payment(chat_id, session)
        return

    if not session.get("phone"):
        sessions[chat_id] = {"stage": "await_phone", "user": user}
        send_text(chat_id, "Please share your phone number to get started.", keyboard=contact_keyboard())
        return

    send_text(chat_id, "Send /start to place an order, or /track to check your last order.")


def _ask_payment(chat_id, session):
    send_text(chat_id, f"{price_summary(session)}\n\nHow would you like to pay?", [
        [btn(PAYMENT_LABELS["telebirr"], "pay:telebirr")],
        [btn(PAYMENT_LABELS["cbe"], "pay:cbe")],
        [btn(PAYMENT_LABELS["cash"], "pay:cash")],
    ])


async def handle_callback(callback: dict, db: Session):
    chat_id = str(callback["message"]["chat"]["id"])
    message_id = callback["message"]["message_id"]
    data = callback["data"]
    answer_callback(callback["id"])
    session = sessions.setdefault(chat_id, {"stage": "product"})

    # --- rider accept / decline (buttons come from the new-order alert) ---
    if data.startswith("accept:"):
        await rider_accept_decline(chat_id, message_id, data, db, accept=True)
        return
    if data.startswith("decline:"):
        await rider_accept_decline(chat_id, message_id, data, db, accept=False)
        return
    if data.startswith("deliver:"):
        _, order_id, empty_returned = data.split(":")
        order = db.query(models.Order).get(int(order_id))
        if order:
            services.deliver_order(db, order, empty_returned=(empty_returned == "yes"))
        edit_text(chat_id, message_id, f"Order #{order_id} completed. Nice work!")
        return

    # --- customer ordering callbacks ---
    if data == "track":
        await track_order(chat_id, db, message_id=message_id)
        return

    if data.startswith("product:"):
        product = data.split(":")[1]
        session["product"] = product
        sizes = SIZES[product]
        if len(sizes) == 1:
            session["size"] = sizes[0]
            session["stage"] = "exchange"
            edit_text(chat_id, message_id, "Do you have an empty one to exchange?",
                      [[btn("Yes, exchange", "exchange:yes"), btn("No, first order", "exchange:no")]])
        else:
            edit_text(chat_id, message_id, f"{PRODUCT_LABELS[product]} — which size?",
                      [[btn(s, f"size:{s}") for s in sizes]])
        return

    if data.startswith("size:"):
        session["size"] = data.split(":")[1]
        session["stage"] = "exchange"
        edit_text(chat_id, message_id, "Do you have an empty one to exchange?",
                  [[btn("Yes, exchange", "exchange:yes"), btn("No, first order", "exchange:no")]])
        return

    if data.startswith("exchange:"):
        session["is_exchange"] = data.endswith("yes")
        session["stage"] = "await_address"
        edit_text(chat_id, message_id,
                  "What's your delivery address? (type your neighborhood + landmark, or share your location.)",
                  [[btn("Share my location", "loc")]])
        return

    if data == "loc":
        # prompt the location reply keyboard (the actual location comes as a message)
        send_text(chat_id, "Tap the button to share your live location.", keyboard=location_keyboard())
        return

    if data.startswith("pay:"):
        method = data.split(":")[1]
        session["payment_method"] = method
        await place_order(chat_id, message_id, session, db)
        return


async def place_order(chat_id, message_id, session, db):
    user = session.get("user", {})
    payload = schemas.OrderCreate(
        customer_phone=session.get("phone") or f"tg-{chat_id}",
        customer_name=user.get("first_name"),
        telegram_id=chat_id,
        address_text=session.get("address"),
        latitude=session.get("lat"),
        longitude=session.get("lng"),
        product=session["product"],
        size=session.get("size"),
        is_exchange=session.get("is_exchange", True),
        quantity=1,
        payment_method=session.get("payment_method"),
    )
    try:
        order = services.place_order(db, payload, rate_key=f"tg:{chat_id}")
    except Exception as e:
        edit_text(chat_id, message_id, f"Couldn't place the order ({e}). Try again shortly.")
        return
    session["last_order_id"] = order.id
    rider = db.query(models.Rider).get(order.rider_id) if order.rider_id else None
    rider_name = rider.name if rider else "A rider"
    if order.payment_method == models.PaymentMethod.cash:
        copy = (f"Order #{order.id} placed. Pay {order.total_price:.0f} ETB cash to the rider on delivery.\n"
                f"{rider_name} is on the way to pick up your order.")
    else:
        copy = (f"Paid. {rider_name} is on the way to pick up your cylinder.\n"
                f"Order #{order.id} — total {order.total_price:.0f} ETB.")
    edit_text(chat_id, message_id, copy + "\n\n[Track live]", [[btn("Track my order", "track")]])
    sessions.pop(chat_id, None)


async def track_order(chat_id, db, message_id=None):
    session = sessions.get(chat_id, {})
    order_id = session.get("last_order_id")
    if not order_id:
        # fall back to the most recent order for this telegram id
        cust = db.query(models.Customer).filter(models.Customer.telegram_id == chat_id).first()
        if cust:
            last = db.query(models.Order).filter(models.Order.customer_id == cust.id)\
                .order_by(models.Order.created_at.desc()).first()
            order_id = last.id if last else None
    if not order_id:
        msg = "No recent order found. Send /start to place one."
    else:
        o = db.query(models.Order).get(order_id)
        if not o:
            msg = "Order not found."
        else:
            msg = f"Order #{o.id} — status: {o.status}, total {o.total_price:.0f} ETB, paid: {'yes' if o.paid else 'no'}."
    if message_id:
        edit_text(chat_id, message_id, msg)
    else:
        send_text(chat_id, msg)


# ---------------- Rider flow ----------------

def _rider_for(db: Session, chat_id: str):
    return db.query(models.Rider).filter(models.Rider.telegram_id == chat_id).first()


async def rider_duty(chat_id, text, db):
    rider = _rider_for(db, chat_id)
    if not rider:
        send_text(chat_id, "You're not registered as a rider yet. Ask an admin to add you.")
        return
    rider.on_duty = (text == "/onduty")
    db.commit()
    send_text(chat_id, "You're now on duty. New orders will come to you here." if rider.on_duty
              else "You're now off duty.")


async def rider_accept_decline(chat_id, message_id, data, db, accept: bool):
    order_id = int(data.split(":")[1])
    order = db.query(models.Order).get(order_id)
    rider = _rider_for(db, chat_id)
    if not order or not rider:
        edit_text(chat_id, message_id, "Order or rider not found.")
        return
    if accept:
        if order.rider_id != rider.id:
            edit_text(chat_id, message_id, "This order was offered to another rider.")
            return
        services.accept_order(db, order, rider)
        shop = order.shop
        from geo import maps_directions_url
        link = maps_directions_url(shop.latitude, shop.longitude, shop.name if shop else None)
        edit_text(chat_id, message_id,
                  f"Order #{order.id} accepted. Pickup: {shop.name if shop else 'shop'}\n{link}\n"
                  f"Reply /pickedup{order.id} once collected (optionally /pickedup{order.id} <remaining stock>).")
    else:
        services.decline_order(db, order, rider)
        edit_text(chat_id, message_id, f"Order #{order.id} declined. Re-offered to the next rider.")


async def rider_pickedup(chat_id, text, db):
    parts = text.replace("/pickedup", "").strip().split()
    order_id = "".join(c for c in (parts[0] if parts else "") if c.isdigit())
    if not order_id:
        send_text(chat_id, "Use /pickedup<order id>  e.g. /pickedup12  (optionally /pickedup12 13 to log remaining stock).")
        return
    remaining = None
    if len(parts) > 1 and parts[1].isdigit():
        remaining = int(parts[1])
    order = db.query(models.Order).get(int(order_id))
    if not order:
        send_text(chat_id, f"Order #{order_id} not found.")
        return
    services.pickup_order(db, order, remaining_stock=remaining)
    send_text(chat_id, f"Order #{order_id} marked picked up. Head to the customer — /delivered{order_id} when swapped.")


async def rider_stock(chat_id, text, db):
    """Standalone remaining-stock log (if the rider didn't pass it with /pickedup)."""
    parts = text.replace("/stock", "").strip().split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        send_text(chat_id, "Use /stock<order id> <remaining>  e.g. /stock12 13")
        return
    order = db.query(models.Order).get(int(parts[0]))
    if not order or not order.shop_id:
        send_text(chat_id, "Order/shop not found.")
        return
    crud.mark_picked_up(db, order, remaining_stock=int(parts[1]))  # updates stock + mismatch check
    db.commit()
    send_text(chat_id, f"Logged remaining stock for order #{parts[0]}.")


async def rider_delivered_prompt(chat_id, text, db):
    order_id = "".join(c for c in text if c.isdigit())
    if not order_id:
        send_text(chat_id, "Use /delivered<order id>  e.g. /delivered12")
        return
    send_text(chat_id, "Did the customer have an empty to exchange?", [[
        btn("Yes, swap complete", f"deliver:{order_id}:yes"),
        btn("No, charged deposit", f"deliver:{order_id}:no"),
    ]])


async def rider_earnings_cmd(chat_id, db):
    rider = _rider_for(db, chat_id)
    if not rider:
        send_text(chat_id, "You're not registered as a rider yet.")
        return
    e = crud.rider_earnings(db, rider.id, days=7)
    send_text(chat_id,
              f"Earnings (last {e['period_days']} days): {e['period_earnings_etb']:.0f} ETB "
              f"from {e['delivered_count']} deliveries.\nToday: {e['today_earnings_etb']:.0f} ETB.")


async def rider_my_orders(chat_id, db):
    rider = _rider_for(db, chat_id)
    if not rider:
        send_text(chat_id, "You're not registered as a rider yet.")
        return
    orders = db.query(models.Order).filter(
        models.Order.rider_id == rider.id,
        models.Order.status.in_([models.OrderStatus.assigned, models.OrderStatus.picked_up]),
    ).order_by(models.Order.created_at.desc()).all()
    if not orders:
        send_text(chat_id, "No active orders. You're all caught up.")
        return
    lines = [f"#{o.id} — {o.product} {o.size or ''} — {o.status}" for o in orders]
    send_text(chat_id, "Your active orders:\n" + "\n".join(lines))


def _normalize_phone(phone: str) -> str:
    p = "".join(c for c in phone if c.isdigit() or c == "+")
    if not p.startswith("+") and p.startswith("251"):
        p = "+" + p
    if not p.startswith("+") and len(p) == 9:
        p = "+251" + p
    return p


def set_webhook(public_url: str) -> dict:
    """Call once after deploying, with your live HTTPS URL, e.g.
    https://your-app.onrender.com/telegram/webhook"""
    resp = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": public_url}, timeout=10)
    return resp.json()
