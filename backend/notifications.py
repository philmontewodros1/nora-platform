import os
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Admin alert channel: a Telegram chat id (group or user) that receives urgent
# push alerts (stock mismatches, stalled deliveries). Set up a channel, add the
# bot as admin, and put the chat id here.
ADMIN_TELEGRAM_CHAT_ID = os.getenv("ADMIN_TELEGRAM_CHAT_ID", "")


def send_telegram_message(chat_id: str, text: str, reply_markup: dict | None = None):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return  # silently skip if not configured (e.g. local dev without a bot token)
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=5)
    except requests.RequestException:
        pass  # notification failures should never break the order flow


def _inline_keyboard(rows):
    """rows: list of [(text, callback_data), ...] — turns into inline buttons."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def notify_rider_new_order(
    rider_telegram_id: str, order_id: int, product: str, size: str,
    shop_name: str, shop_lat=None, shop_lng=None,
    customer_lat=None, customer_lng=None, customer_address=None,
):
    from geo import maps_directions_url
    shop_map = maps_directions_url(shop_lat, shop_lng, shop_name)
    cust_map = maps_directions_url(customer_lat, customer_lng, customer_address)
    text = (
        f"New order #{order_id}\n"
        f"{product} {size or ''}\n"
        f"Pickup: {shop_name}\n"
        f"Deliver to: {customer_address or 'see map'}\n\n"
        f"Pickup directions: {shop_map}\n"
        f"Delivery directions: {cust_map}"
    )
    send_telegram_message(
        rider_telegram_id, text,
        reply_markup=_inline_keyboard([
            [(f"Accept #{order_id}", f"accept:{order_id}"),
             (f"Decline #{order_id}", f"decline:{order_id}")],
        ]),
    )


def notify_customer_status(customer_telegram_id: str, order_id: int, status: str,
                           rider_name: str | None = None, eta_minutes: int | None = None):
    eta_str = f" (ETA ~{eta_minutes} min)" if eta_minutes else ""
    rider_str = f" Rider {rider_name} is on the way." if rider_name else ""
    messages = {
        "assigned": f"Order #{order_id}: paid. {rider_name or 'A rider'} is on the way to pick up your order.{eta_str}",
        "picked_up": f"Order #{order_id}: your rider has picked up your order and is on the way.{rider_str}",
        "delivered": f"Order #{order_id}: delivered. Thanks for using Nora! Rate your rider.",
        "cancelled": f"Order #{order_id}: cancelled.",
    }
    text = messages.get(status)
    if text:
        send_telegram_message(customer_telegram_id, text)


def notify_customer_payment_failed(customer_telegram_id: str, order_id: int):
    send_telegram_message(
        customer_telegram_id,
        f"Order #{order_id}: payment didn't go through. Tap to retry.",
        reply_markup=_inline_keyboard([[(f"Retry payment", f"retrypay:{order_id}")]]),
    )


def notify_admin_alert(text: str):
    """Push to the admin alert channel for time-sensitive issues (spec section 6)."""
    if ADMIN_TELEGRAM_CHAT_ID:
        send_telegram_message(ADMIN_TELEGRAM_CHAT_ID, f"🔔 Nora admin alert\n{text}")


# ---------------- WhatsApp (Cloud API) ----------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_API = "https://graph.facebook.com/v20.0"


def _wa_post(payload: dict):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return  # WhatsApp not configured — skip silently
    try:
        requests.post(
            f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json=payload, timeout=10,
        )
    except requests.RequestException:
        pass


def notify_rider_whatsapp(
    rider_whatsapp_id: str, order_id: int, product: str, size: str,
    shop_name: str, shop_lat=None, shop_lng=None,
    customer_lat=None, customer_lng=None, customer_address=None,
):
    """Send a new-order alert to a rider on WhatsApp with Accept/Decline buttons.
    Only works within 24h of the rider's last message to the bot (session window);
    outside that, replace with a pre-approved template (spec section 3.4)."""
    if not rider_whatsapp_id:
        return
    from geo import maps_directions_url
    body = (
        f"New order #{order_id}\n{product} {size or ''}\n"
        f"Pickup: {shop_name}\nDeliver to: {customer_address or 'see map'}\n"
        f"Map: {maps_directions_url(customer_lat, customer_lng, customer_address)}"
    )
    _wa_post({
        "messaging_product": "whatsapp", "to": rider_whatsapp_id, "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": body[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": f"accept_{order_id}", "title": "Accept"}},
                {"type": "reply", "reply": {"id": f"decline_{order_id}", "title": "Decline"}},
            ]},
        },
    })


def check_stalled_deliveries(db):
    """Called periodically (or on each status read). Flags orders assigned more
    than eta + 20 min ago that still aren't delivered (spec section 6)."""
    from datetime import datetime, timedelta
    from models import Order, OrderStatus
    threshold = datetime.utcnow() - timedelta(minutes=65)  # eta 45 + 20 grace
    stalled = db.query(Order).filter(
        Order.status.in_([OrderStatus.assigned, OrderStatus.picked_up]),
        Order.assigned_at.isnot(None),
        Order.assigned_at < threshold,
    ).all()
    for o in stalled:
        shop = o.shop.name if o.shop else "?"
        notify_admin_alert(
            f"Stalled delivery — order #{o.id} ({o.product} {o.size or ''}) "
            f"from {shop} has been in '{o.status}' since {o.assigned_at.isoformat()}."
        )
