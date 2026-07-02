"""
LEGACY / OPTIONAL — superseded by backend/telegram_router.py (the production path,
a webhook mounted into the main app). This standalone polling version is kept only for
quick local testing without a public URL. It does NOT include phone capture, payment,
or accept/decline — use the router for anything real. Do not deploy this separately.

Nora Telegram bot - handles both customers and riders.

Customer flow: /start -> pick product -> pick size -> exchange or first-time -> confirm -> pay -> track.
Rider flow: /start with a rider account already registered by admin -> receives order alerts ->
            /accept<id> or /decline<id> -> /pickedup<id> -> /delivered<id>.

Run:  python bot.py     (reads TELEGRAM_BOT_TOKEN and BACKEND_URL from environment)
"""
import os
import logging
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nora-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Conversation states
PRODUCT, SIZE, EXCHANGE, ADDRESS, CONFIRM = range(5)

SIZES = {
    "gas": ["6kg", "12kg", "22kg"],
    "water": ["jar"],
    "butane": ["canister"],
}

PRODUCT_LABELS = {"gas": "Cooking gas", "water": "Water jar", "butane": "Butane canister"}


def kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


# ---------------- Customer flow ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Selam! Welcome to Nora. What do you need today?",
        reply_markup=kb([
            [("Cooking gas", "product:gas")],
            [("Water jar", "product:water")],
            [("Butane canister", "product:butane")],
            [("Track my last order", "track")],
        ])
    )
    return PRODUCT


async def track_last_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data.get("last_order_id")
    if not order_id:
        await query.edit_message_text("No recent order found. Send /start to place one.")
        return ConversationHandler.END
    try:
        r = requests.get(f"{BACKEND_URL}/orders/{order_id}", timeout=5)
        r.raise_for_status()
        order = r.json()
        await query.edit_message_text(f"Order #{order['id']} status: {order['status']}")
    except requests.RequestException:
        await query.edit_message_text("Couldn't reach the server, try again shortly.")
    return ConversationHandler.END


async def choose_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "track":
        return await track_last_order(update, context)

    product = query.data.split(":")[1]
    context.user_data["product"] = product
    sizes = SIZES[product]
    if len(sizes) == 1:
        context.user_data["size"] = sizes[0]
        return await ask_exchange(update, context)

    await query.edit_message_text(
        f"{PRODUCT_LABELS[product]} — which size?",
        reply_markup=kb([[(s, f"size:{s}") for s in sizes]])
    )
    return SIZE


async def choose_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    size = query.data.split(":")[1]
    context.user_data["size"] = size
    return await ask_exchange(update, context)


async def ask_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    product = context.user_data["product"]
    size = context.user_data["size"]
    await query.edit_message_text(
        f"Do you have an empty {size} {PRODUCT_LABELS[product].lower()} ready to exchange?",
        reply_markup=kb([[("Yes, exchange", "exchange:yes"), ("No, first order", "exchange:no")]])
    )
    return EXCHANGE


async def choose_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["is_exchange"] = query.data.endswith("yes")
    await query.edit_message_text("What's your delivery address? (e.g. neighborhood + landmark)")
    return ADDRESS


async def receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["address"] = update.message.text
    d = context.user_data
    await update.message.reply_text(
        f"Confirm order:\n"
        f"{PRODUCT_LABELS[d['product']]} {d['size']}\n"
        f"{'Exchange' if d['is_exchange'] else 'First-time order (deposit applies)'}\n"
        f"Deliver to: {d['address']}\n\n"
        f"Place this order?",
        reply_markup=kb([[("Confirm and pay", "confirm:yes"), ("Cancel", "confirm:no")]])
    )
    return CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.endswith("no"):
        await query.edit_message_text("Order cancelled. Send /start to try again.")
        return ConversationHandler.END

    d = context.user_data
    user = update.effective_user
    payload = {
        "customer_phone": f"tg-{user.id}",  # replace with real phone capture in production
        "customer_name": user.first_name,
        "telegram_id": str(user.id),
        "address_text": d["address"],
        "product": d["product"],
        "brand": None,
        "size": d["size"],
        "is_exchange": d["is_exchange"],
        "quantity": 1,
    }
    try:
        r = requests.post(f"{BACKEND_URL}/orders", json=payload, timeout=10)
        r.raise_for_status()
        order = r.json()
        context.user_data["last_order_id"] = order["id"]
        await query.edit_message_text(
            f"Order #{order['id']} placed.\n"
            f"Total: {order['total_price']:.0f} ETB "
            f"(product {order['product_price']:.0f} + delivery {order['delivery_fee']:.0f}"
            + (f" + deposit {order['deposit_fee']:.0f}" if order['deposit_fee'] else "") + ")\n\n"
            f"We'll notify you here as your order moves. Send /start for a new order."
        )
    except requests.RequestException:
        await query.edit_message_text("Couldn't place the order — server unreachable. Please try again shortly.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    return ConversationHandler.END


# ---------------- Rider flow ----------------
# Riders must already be registered in the backend (via admin dashboard or /riders API)
# with their Telegram numeric ID saved as telegram_id.

async def rider_on_duty(update: Update, context: ContextTypes.DEFAULT_TYPE, on: bool):
    user = update.effective_user
    try:
        r = requests.get(f"{BACKEND_URL}/riders/by_telegram/{user.id}", timeout=5)
        if r.status_code == 404:
            await update.message.reply_text("You're not registered as a rider yet. Ask an admin to add you.")
            return
        rider = r.json()
        requests.post(f"{BACKEND_URL}/riders/{rider['id']}/duty", params={"on_duty": on}, timeout=5)
        await update.message.reply_text("You're now on duty." if on else "You're now off duty.")
    except requests.RequestException:
        await update.message.reply_text("Couldn't reach the server, try again shortly.")


async def rider_start_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rider_on_duty(update, context, True)


async def rider_end_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rider_on_duty(update, context, False)


async def rider_picked_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text  # e.g. /pickedup12
    order_id = "".join(ch for ch in text if ch.isdigit())
    if not order_id:
        await update.message.reply_text("Use the format /pickedup<order id>, e.g. /pickedup12")
        return
    try:
        r = requests.post(f"{BACKEND_URL}/orders/{order_id}/status", json={"status": "picked_up"}, timeout=5)
        r.raise_for_status()
        await update.message.reply_text(f"Order #{order_id} marked picked up. Head to the customer.")
    except requests.RequestException:
        await update.message.reply_text("Couldn't update the order — try again shortly.")


async def rider_delivered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text  # e.g. /delivered12
    order_id = "".join(ch for ch in text if ch.isdigit())
    if not order_id:
        await update.message.reply_text("Use the format /delivered<order id>, e.g. /delivered12")
        return
    await update.message.reply_text(
        "Did the customer have an empty to exchange?",
        reply_markup=kb([[("Yes, swap complete", f"deliver:{order_id}:yes"),
                           ("No, charged deposit", f"deliver:{order_id}:no")]])
    )


async def rider_delivered_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, order_id, empty_returned = query.data.split(":")
    try:
        requests.post(f"{BACKEND_URL}/orders/{order_id}/status", json={
            "status": "delivered",
            "empty_returned": empty_returned == "yes",
            "paid": True,
        }, timeout=5)
        await query.edit_message_text(f"Order #{order_id} completed. Nice work!")
    except requests.RequestException:
        await query.edit_message_text("Couldn't update the order — try again shortly.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment before running the bot.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PRODUCT: [CallbackQueryHandler(choose_product)],
            SIZE: [CallbackQueryHandler(choose_size)],
            EXCHANGE: [CallbackQueryHandler(choose_exchange)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address)],
            CONFIRM: [CallbackQueryHandler(confirm_order)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("onduty", rider_start_shift))
    app.add_handler(CommandHandler("offduty", rider_end_shift))
    app.add_handler(MessageHandler(filters.Regex(r"^/pickedup\d+$"), rider_picked_up))
    app.add_handler(MessageHandler(filters.Regex(r"^/delivered\d+$"), rider_delivered))
    app.add_handler(CallbackQueryHandler(rider_delivered_confirm, pattern=r"^deliver:"))

    log.info("Nora bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
