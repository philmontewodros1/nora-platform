"""
Telegram bot logic, as a webhook mounted into the main FastAPI app (one free
web service total). Handles customers AND riders in the same bot.

Customer flow: /start -> (share phone OR type it) -> product -> size -> exchange
               -> address/location -> itemized summary + shop + no-stock check
               -> [Pay with TeleBirr]/[CBE]/[Cash] -> track/refresh.
Rider flow:    /onduty /offduty, accept:/decline: buttons, /pickedup<id> [<stock>],
               /delivered<id>, /earnings, /myorders, /help.

The router only handles presentation (buttons, message copy). All order logic
lives in services.py / crud.py, which the HTTP API also uses.

UX: "typing..." indicator before any work, HTML formatting with escaping of all
dynamic text, an honest no-stock-nearby check before payment (so nobody pays for
an order that can't be fulfilled), a refresh button on tracking, and friendly
fallbacks if anything throws.
"""
import os
import html as html_lib
import requests
from fastapi import APIRouter, Request
from sqlalchemy.orm import Session
from sqlalchemy import desc

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
PRODUCT_EMOJI = {"gas": "🔥", "water": "💧", "butane": "🔥"}
PAYMENT_LABELS = {"telebirr": "Pay with TeleBirr", "cbe": "CBE Birr", "cash": "Cash on delivery"}

# In-memory session state per Telegram chat ID. Fine for a single-instance free
# web service; if you ever scale to multiple instances, move this to the DB.
sessions: dict[str, dict] = {}


# ---------------- low-level Telegram API helpers ----------------

def tg_call(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10)
        return r.json()
    except requests.RequestException:
        return None


def send_typing(chat_id):
    """Call first, before any processing that takes more than an instant, so the
    customer sees 'Nora is typing...' immediately instead of silence."""
    tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def send_text(chat_id, text, buttons=None, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    elif keyboard:
        payload["reply_markup"] = keyboard
    return tg_call("sendMessage", payload)


def edit_text(chat_id, message_id, text, buttons=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    tg_call("editMessageText", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    tg_call("answerCallbackQuery", payload)


def esc(s) -> str:
    """HTML-escape any user-provided or dynamic text before putting it in a message.
    parse_mode HTML will silently drop a whole message on an unescaped <, >, or &."""
    return html_lib.escape(str(s)) if s is not None else ""


def btn(text, data):
    return {"text": text, "callback_data": data}


def contact_keyboard():
    return {"keyboard": [[{"text": "Share my number", "request_contact": True}]],
            "one_time_keyboard": True, "resize_keyboard": True}


def location_keyboard():
    return {"keyboard": [[{"text": "Share my location", "request_location": True}]],
            "one_time_keyboard": True, "resize_keyboard": True}


def set_commands():
    """Call once so Telegram shows a command menu in the chat UI."""
    tg_call("setMyCommands", {"commands": [
        {"command": "start", "description": "Order gas, water, or butane"},
        {"command": "myorders", "description": "See your recent orders"},
        {"command": "help", "description": "What this bot can do"},
        {"command": "track", "description": "Track my last order"},
        {"command": "onduty", "description": "Riders: start receiving orders"},
        {"command": "offduty", "description": "Riders: stop receiving orders"},
        {"command": "earnings", "description": "Riders: my earnings"},
    ]})


# ---------------- webhook entry point (with friendly error fallback) ----------------

@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    db = SessionLocal()
    try:
        if "message" in update:
            await handle_message(update["message"], db)
        elif "callback_query" in update:
            await handle_callback(update["callback_query"], db)
    except Exception:
        chat_id = None
        try:
            if "message" in update:
                chat_id = str(update["message"]["chat"]["id"])
            elif "callback_query" in update:
                chat_id = str(update["callback_query"]["message"]["chat"]["id"])
        except Exception:
            pass
        if chat_id:
            send_text(chat_id, "⚠️ Something went wrong on our end. Please try again — if it keeps "
                               "happening, send /start to reset.")
        raise
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
        send_typing(chat_id)
        await rider_duty(chat_id, text, db)
        return
    if text.startswith("/pickedup"):
        send_typing(chat_id)
        await rider_pickedup(chat_id, text, db)
        return
    if text.startswith("/delivered"):
        await rider_delivered_prompt(chat_id, text, db)
        return
    if text.startswith("/stock"):
        send_typing(chat_id)
        await rider_stock(chat_id, text, db)
        return
    if text == "/earnings":
        send_typing(chat_id)
        await rider_earnings_cmd(chat_id, db)
        return
    if text == "/myorders":
        send_typing(chat_id)
        await show_my_orders(chat_id, db)
        return
    if text == "/help":
        send_text(chat_id,
            "<b>Here's what I can do:</b>\n\n"
            "/start — order gas, water, or butane\n"
            "/myorders — see your recent orders and their status\n"
            "/track — check your last order\n"
            "/help — this message\n\n"
            "If you're a delivery rider:\n"
            "/onduty — start receiving new orders\n"
            "/offduty — stop receiving orders\n"
            "/earnings — your earnings\n\n"
            "Once you've placed an order, I'll message you here as it moves — "
            "assigned, picked up, and delivered.")
        return

    # --- customer flow ---
    if text in ("/start", "/menu"):
        send_typing(chat_id)
        sessions[chat_id] = {"stage": "await_phone", "user": user}
        send_text(chat_id,
            "👋 <b>Selam! Welcome to Nora.</b>\nTo order, please share your phone number "
            "(we use it to identify your orders across Telegram, WhatsApp, and the web).",
            keyboard=contact_keyboard())
        return

    if text == "/track":
        send_typing(chat_id)
        await track_order(chat_id, db)
        return

    # contact shared (phone capture)
    if "contact" in message:
        send_typing(chat_id)
        phone = message["contact"].get("phone_number")
        if phone:
            session["phone"] = _normalize_phone(phone)
            session["user"] = user
        session["stage"] = "product"
        _send_product_menu(chat_id)
        return

    # typed phone number during the phone step (not everyone taps the button)
    if session.get("stage") == "await_phone" and text:
        digits = "".join(c for c in text if c.isdigit() or c == "+")
        if len(digits) >= 9:
            send_typing(chat_id)
            session["phone"] = _normalize_phone(digits)
            session["user"] = user
            session["stage"] = "product"
            _send_product_menu(chat_id, prefix="Got it.")
            return

    # location shared (address pin)
    if "location" in message and session.get("stage") == "await_address":
        send_typing(chat_id)
        session["lat"] = message["location"]["latitude"]
        session["lng"] = message["location"]["longitude"]
        session["address"] = f"pin {session['lat']:.5f},{session['lng']:.5f}"
        session["stage"] = "payment"
        await _ask_payment(chat_id, session, db)
        return

    # free text mid-flow: the delivery address
    if session.get("stage") == "await_address":
        send_typing(chat_id)
        session["address"] = text
        session["stage"] = "payment"
        await _ask_payment(chat_id, session, db)
        return

    if not session.get("phone"):
        sessions[chat_id] = {"stage": "await_phone", "user": user}
        send_text(chat_id, "Please share your phone number to get started.", keyboard=contact_keyboard())
        return

    send_text(chat_id, "Not sure what you mean — send /start to order, or /help to see everything I can do.")


def _send_product_menu(chat_id, prefix="What do you need today?"):
    send_text(chat_id, f"{prefix}\n\n<b>What do you need today?</b>", [
        [btn("🔥 Cooking gas", "product:gas")],
        [btn("💧 Water jar", "product:water")],
        [btn("🔥 Butane canister", "product:butane")],
        [btn("📦 Track my last order", "track")],
    ])


async def _ask_payment(chat_id, session, db):
    """Itemized summary with the matched shop name + an honest no-stock check
    BEFORE offering payment (so nobody pays for an unfulfillable order)."""
    d = session
    product = d["product"]
    size = d.get("size")
    stock_item = crud.find_matching_stock(db, product, None, size, d.get("lat"), d.get("lng"))
    if not stock_item:
        send_text(chat_id,
            f"😕 <b>No shop nearby has {esc(size or '')} {esc(PRODUCT_LABELS[product].lower())} "
            f"in stock right now.</b>\nTry again shortly, or pick a different size.",
            [[btn("⬅️ Back to menu", f"product:{product}")]])
        return
    shop = stock_item.shop
    d["matched_shop_id"] = shop.id  # hint; place_order re-checks authoritatively

    product_price = pricing.price_for(product, size or "")
    delivery = pricing.DELIVERY_FEE
    deposit = 0 if d.get("is_exchange", True) else pricing.deposit_for(product)
    total = product_price + delivery + deposit

    lines = [
        "<b>Confirm your order</b>", "",
        f"{PRODUCT_EMOJI.get(product, '')} {esc(PRODUCT_LABELS[product])} · {esc(size)}",
        f"🏪 From: <b>{esc(shop.name)}</b>",
        f"📍 Deliver to: {esc(d.get('address') or '')}", "",
        f"<code>{esc(PRODUCT_LABELS[product])}</code>  {product_price:.0f} ETB",
        f"<code>Delivery</code>  {delivery:.0f} ETB",
    ]
    if deposit:
        lines.append(f"<code>Deposit</code>  {deposit:.0f} ETB <i>(refundable)</i>")
    lines.append(f"<b>Total: {total:.0f} ETB</b>")
    lines.append("")
    lines.append("⏱ Estimated pickup: 15–25 minutes once confirmed")
    lines.append("\n<b>How would you like to pay?</b>")

    send_text(chat_id, "\n".join(lines), [
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
        send_typing(chat_id)
        await rider_accept_decline(chat_id, message_id, data, db, accept=True)
        return
    if data.startswith("decline:"):
        send_typing(chat_id)
        await rider_accept_decline(chat_id, message_id, data, db, accept=False)
        return
    if data.startswith("deliver:"):
        send_typing(chat_id)
        _, order_id, empty_returned = data.split(":")
        order = db.query(models.Order).get(int(order_id))
        if not order:
            edit_text(chat_id, message_id, f"Couldn't find order #{esc(order_id)}.")
            return
        services.deliver_order(db, order, empty_returned=(empty_returned == "yes"))
        edit_text(chat_id, message_id, f"✅ Order <b>#{order_id}</b> completed. Nice work!")
        return

    # --- customer ordering callbacks ---
    # defensive: size/exchange/pay need a product chosen first. An old inline
    # button tapped after the session was cleared (e.g. post-order) would otherwise
    # 500 — redirect to a fresh product menu instead.
    if data.startswith(("size:", "exchange:", "pay:")) and "product" not in session:
        send_text(chat_id, "Let's start fresh — what do you need today?", [
            [btn("🔥 Cooking gas", "product:gas")],
            [btn("💧 Water jar", "product:water")],
            [btn("🔥 Butane canister", "product:butane")],
        ])
        return

    if data == "track":
        send_typing(chat_id)
        await track_order(chat_id, db, message_id=message_id)
        return

    if data.startswith("refresh:"):
        send_typing(chat_id)
        order_id = int(data.split(":")[1])
        order = db.query(models.Order).get(order_id)
        if not order:
            edit_text(chat_id, message_id, "Order not found.")
            return
        edit_text(chat_id, message_id, format_tracking_message(order, db),
                  [[btn("🔄 Refresh status", f"refresh:{order.id}")]])
        return

    if data.startswith("product:"):
        send_typing(chat_id)
        product = data.split(":")[1]
        session["product"] = product
        sizes = SIZES[product]
        if len(sizes) == 1:
            session["size"] = sizes[0]
            session["stage"] = "exchange"
            edit_text(chat_id, message_id,
                f"{PRODUCT_EMOJI[product]} <b>{PRODUCT_LABELS[product]}</b>\n\nDo you have an empty one ready to exchange?",
                [[btn("✅ Yes, exchange", "exchange:yes"), btn("🆕 No, first order", "exchange:no")]])
        else:
            edit_text(chat_id, message_id, f"{PRODUCT_EMOJI[product]} <b>{PRODUCT_LABELS[product]}</b> — which size?",
                      [[btn(s, f"size:{s}") for s in sizes]])
        return

    if data.startswith("size:"):
        session["size"] = data.split(":")[1]
        session["stage"] = "exchange"
        edit_text(chat_id, message_id,
            f"<b>{esc(session['size'])} {esc(PRODUCT_LABELS[session['product']])}</b>\n\n"
            f"Do you have an empty one ready to exchange?",
            [[btn("✅ Yes, exchange", "exchange:yes"), btn("🆕 No, first order", "exchange:no")]])
        return

    if data.startswith("exchange:"):
        session["is_exchange"] = data.endswith("yes")
        session["stage"] = "await_address"
        note = "" if session["is_exchange"] else "\n\n<i>Since this is your first order, a refundable deposit applies.</i>"
        edit_text(chat_id, message_id, f"📍 What's your delivery address?{note}\n(type your neighborhood + landmark, or share your location)",
                  [[btn("📍 Share my location", "loc")]])
        return

    if data == "loc":
        send_text(chat_id, "Tap the button to share your location.", keyboard=location_keyboard())
        return

    if data.startswith("pay:"):
        session["payment_method"] = data.split(":")[1]
        send_typing(chat_id)
        await place_order(chat_id, message_id, session, db)
        return


async def place_order(chat_id, message_id, session, db):
    # defensive re-check right before charging: stock could have changed since the summary
    stock = crud.find_matching_stock(db, session["product"], None, session.get("size"),
                                     session.get("lat"), session.get("lng"))
    if not stock:
        edit_text(chat_id, message_id,
            "😕 No shop nearby had stock by the time we confirmed. <b>You haven't been charged</b> — "
            "send /start to try a different size.")
        sessions.pop(chat_id, None)
        return

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
        edit_text(chat_id, message_id, "⚠️ Couldn't place the order — please try again in a moment.")
        return

    if not order.shop_id:
        edit_text(chat_id, message_id,
            "😕 No shop nearby had stock by the time we confirmed. <b>You haven't been charged</b> — "
            "send /start to try again.")
        sessions.pop(chat_id, None)
        return

    session["last_order_id"] = order.id
    rider = db.query(models.Rider).get(order.rider_id) if order.rider_id else None
    rider_name = rider.name if rider else "A rider"
    if order.payment_method == models.PaymentMethod.cash:
        copy = (f"✅ <b>Order #{order.id} placed.</b>\nPay <b>{order.total_price:.0f} ETB</b> cash to the rider on delivery.\n"
                f"{esc(rider_name)} is on the way to pick up your order.")
    else:
        copy = (f"✅ <b>Paid.</b> {esc(rider_name)} is on the way to pick up your cylinder.\n"
                f"Order <b>#{order.id}</b> — total <b>{order.total_price:.0f} ETB</b>.")
    edit_text(chat_id, message_id, copy, [
        [btn("🔄 Refresh status", f"refresh:{order.id}")],
        [btn("📦 My orders", "track")],
    ])
    sessions.pop(chat_id, None)


def format_tracking_message(order, db) -> str:
    status_labels = {
        "pending": "⏳ Waiting for a rider",
        "assigned": "🛵 Rider assigned, heading to shop",
        "picked_up": "🛴 On the way to you",
        "delivered": "✅ Delivered",
        "cancelled": "❌ Cancelled",
    }
    status_key = order.status.value if hasattr(order.status, "value") else order.status
    status = status_labels.get(status_key, str(status_key))
    rider_line = ""
    if order.rider_id:
        rider = db.query(models.Rider).get(order.rider_id)
        if rider:
            rider_line = f"🛵 Rider: {esc(rider.name)}\n"
    paid_line = "✅ Paid" if order.paid else "💵 Cash on delivery"
    return (
        f"<b>Order #{order.id}</b>\n"
        f"{PRODUCT_EMOJI.get(order.product, '')} {esc(order.product)} {esc(order.size or '')}\n"
        f"{rider_line}"
        f"Status: <b>{status}</b>\n"
        f"{paid_line}"
    )


async def track_order(chat_id, db, message_id=None):
    session = sessions.get(chat_id, {})
    order_id = session.get("last_order_id")
    if not order_id:
        cust = db.query(models.Customer).filter(models.Customer.telegram_id == chat_id).first()
        if cust:
            last = db.query(models.Order).filter(models.Order.customer_id == cust.id)\
                .order_by(desc(models.Order.created_at)).first()
            order_id = last.id if last else None
    if not order_id:
        msg = "No recent order found. Send /start to place one."
        btns = None
    else:
        o = db.query(models.Order).get(order_id)
        if not o:
            msg = "Order not found."
            btns = None
        else:
            msg = format_tracking_message(o, db)
            btns = [[btn("🔄 Refresh status", f"refresh:{o.id}")]]
    if message_id:
        edit_text(chat_id, message_id, msg, btns)
    else:
        send_text(chat_id, msg, btns)


async def show_my_orders(chat_id, db: Session):
    """Customer's last 5 orders with status (also useful to a rider who's also a customer)."""
    customer = db.query(models.Customer).filter(models.Customer.telegram_id == chat_id).first()
    if not customer:
        send_text(chat_id, "You haven't placed any orders yet. Send /start to order.")
        return
    orders = (db.query(models.Order).filter(models.Order.customer_id == customer.id)
              .order_by(desc(models.Order.created_at)).limit(5).all())
    if not orders:
        send_text(chat_id, "You haven't placed any orders yet. Send /start to order.")
        return
    status_labels = {
        "pending": "⏳ Pending", "assigned": "🛵 Assigned", "picked_up": "🛴 On the way",
        "delivered": "✅ Delivered", "cancelled": "❌ Cancelled",
    }
    lines = ["<b>Your recent orders</b>", ""]
    for o in orders:
        sk = o.status.value if hasattr(o.status, "value") else o.status
        lines.append(f"#{o.id} · {esc(o.product)} {esc(o.size or '')} · {status_labels.get(sk, esc(sk))} · {o.total_price:.0f} ETB")
    send_text(chat_id, "\n".join(lines))


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
    if rider.on_duty:
        send_text(chat_id, "✅ <b>You're on duty.</b> You'll get a message here the moment a new order comes in.")
    else:
        send_text(chat_id, "You're now off duty. Send /onduty when you're ready for more orders.")


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
                  f"✅ Order <b>#{order.id}</b> accepted.\n🏪 Pickup: <b>{esc(shop.name if shop else 'shop')}</b>\n"
                  f"🗺 <a href=\"{link}\">Directions to shop</a>\n"
                  f"Reply /pickedup{order.id} once collected (optionally /pickedup{order.id} &lt;remaining stock&gt;).")
    else:
        services.decline_order(db, order, rider)
        edit_text(chat_id, message_id, f"Order #{order.id} declined. Re-offered to the next rider.")


async def rider_pickedup(chat_id, text, db):
    parts = text.replace("/pickedup", "").strip().split()
    order_id = "".join(c for c in (parts[0] if parts else "") if c.isdigit())
    if not order_id:
        send_text(chat_id, "Use /pickedup&lt;order id&gt;  e.g. /pickedup12  (optionally /pickedup12 13 to log remaining stock).")
        return
    remaining = None
    if len(parts) > 1 and parts[1].isdigit():
        remaining = int(parts[1])
    order = db.query(models.Order).get(int(order_id))
    if not order:
        send_text(chat_id, f"Order #{esc(order_id)} not found.")
        return
    services.pickup_order(db, order, remaining_stock=remaining)
    send_text(chat_id, f"✅ Order <b>#{order_id}</b> marked picked up. Head to the customer — /delivered{order_id} when swapped.")


async def rider_stock(chat_id, text, db):
    """Standalone remaining-stock log (if the rider didn't pass it with /pickedup)."""
    parts = text.replace("/stock", "").strip().split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        send_text(chat_id, "Use /stock&lt;order id&gt; &lt;remaining&gt;  e.g. /stock12 13")
        return
    order = db.query(models.Order).get(int(parts[0]))
    if not order or not order.shop_id:
        send_text(chat_id, "Order/shop not found.")
        return
    crud.mark_picked_up(db, order, remaining_stock=int(parts[1]))
    db.commit()
    send_text(chat_id, f"Logged remaining stock for order #{parts[0]}.")


async def rider_delivered_prompt(chat_id, text, db):
    order_id = "".join(c for c in text if c.isdigit())
    if not order_id:
        send_text(chat_id, "Use /delivered&lt;order id&gt;  e.g. /delivered12")
        return
    send_text(chat_id, "Did the customer have an empty to exchange?", [[
        btn("✅ Yes, swap complete", f"deliver:{order_id}:yes"),
        btn("💳 No, charged deposit", f"deliver:{order_id}:no"),
    ]])


async def rider_earnings_cmd(chat_id, db):
    rider = _rider_for(db, chat_id)
    if not rider:
        send_text(chat_id, "You're not registered as a rider yet.")
        return
    e = crud.rider_earnings(db, rider.id, days=7)
    send_text(chat_id,
              f"<b>Earnings (last {e['period_days']} days):</b> {e['period_earnings_etb']:.0f} ETB "
              f"from {e['delivered_count']} deliveries.\nToday: <b>{e['today_earnings_etb']:.0f} ETB</b>.")


def _normalize_phone(phone: str) -> str:
    p = "".join(c for c in phone if c.isdigit() or c == "+")
    if p.startswith("+"):
        return p
    if p.startswith("251"):
        return "+" + p
    if p.startswith("0") and len(p) == 10:        # Ethiopian local: 0909... -> +251909...
        return "+251" + p[1:]
    if len(p) == 9:                               # 909... -> +251909...
        return "+251" + p
    return p


def set_webhook(public_url: str) -> dict:
    """Call once after deploying, with your live HTTPS URL, e.g.
    https://your-app.onrender.com/telegram/webhook"""
    resp = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": public_url}, timeout=10)
    return resp.json()
