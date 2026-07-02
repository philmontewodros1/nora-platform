from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import models
import schemas
import pricing
from geo import haversine_km
from notifications import notify_rider_new_order, notify_rider_whatsapp


def get_or_create_customer(db: Session, data: schemas.OrderCreate) -> models.Customer:
    customer = db.query(models.Customer).filter(models.Customer.phone == data.customer_phone).first()
    if customer:
        # keep identifiers up to date (e.g. same phone now also used on Telegram)
        if data.telegram_id:
            customer.telegram_id = data.telegram_id
        if data.whatsapp_id:
            customer.whatsapp_id = data.whatsapp_id
        if data.address_text:
            customer.address_text = data.address_text
        if data.latitude is not None:
            customer.latitude = data.latitude
        if data.longitude is not None:
            customer.longitude = data.longitude
        db.commit()
        db.refresh(customer)
        return customer

    customer = models.Customer(
        name=data.customer_name,
        phone=data.customer_phone,
        telegram_id=data.telegram_id,
        whatsapp_id=data.whatsapp_id,
        address_text=data.address_text,
        latitude=data.latitude,
        longitude=data.longitude,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def find_matching_stock(
    db: Session,
    product: str,
    brand: str | None,
    size: str | None,
    customer_lat: float | None = None,
    customer_lng: float | None = None,
):
    """Find the best shop with matching, in-stock product.

    Distance-based when the customer and shops both have lat/lng (nearest first);
    falls back to highest-quantity when location isn't available yet.
    """
    query = db.query(models.StockItem).join(models.Shop).filter(
        models.StockItem.product == product,
        models.StockItem.quantity > 0,
        models.Shop.active == True,  # noqa: E712
    )
    if size:
        query = query.filter(models.StockItem.size == size)
    if brand:
        query = query.filter(models.StockItem.brand == brand)

    items = query.all()
    if not items:
        return None

    has_customer_loc = customer_lat is not None and customer_lng is not None
    if has_customer_loc:
        def dist(item):
            return haversine_km(customer_lat, customer_lng,
                                item.shop.latitude, item.shop.longitude) or float("inf")
        items.sort(key=dist)
        return items[0]

    # fallback: highest quantity first
    return max(items, key=lambda i: i.quantity)


def create_order(db: Session, data: schemas.OrderCreate) -> models.Order:
    customer = get_or_create_customer(db, data)
    stock_item = find_matching_stock(
        db, data.product, data.brand, data.size, customer.latitude, customer.longitude
    )

    product_price = pricing.price_for(data.product, data.size or "")
    deposit = 0 if data.is_exchange else pricing.deposit_for(data.product)
    total = (product_price * data.quantity) + pricing.DELIVERY_FEE + deposit

    order = models.Order(
        customer_id=customer.id,
        shop_id=stock_item.shop_id if stock_item else None,
        product=data.product,
        brand=data.brand,
        size=data.size,
        is_exchange=data.is_exchange,
        quantity=data.quantity,
        product_price=product_price,
        delivery_fee=pricing.DELIVERY_FEE,
        deposit_fee=deposit,
        total_price=total,
        status=models.OrderStatus.pending,
        eta_minutes=pricing.DEFAULT_ETA_MINUTES,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def offer_order_to_rider(db: Session, order: models.Order) -> models.Rider | None:
    """Pick the next on-duty rider to offer the order to and push an alert.
    The order is assigned immediately (so it isn't double-offered to two riders
    at once); the rider then accepts/declines. On decline, re_offer_next_rider
    moves it to the next on-duty rider."""
    rider = db.query(models.Rider).filter(
        models.Rider.active == True,  # noqa: E712
        models.Rider.on_duty == True,  # noqa: E712
    ).first()
    if rider:
        order.rider_id = rider.id
        order.status = models.OrderStatus.assigned
        order.assigned_at = datetime.utcnow()
        db.commit()
        db.refresh(order)
        shop = order.shop
        if shop:
            if rider.telegram_id:
                notify_rider_new_order(
                    rider.telegram_id, order.id, order.product, order.size or "",
                    shop.name, shop.latitude, shop.longitude, order.customer.latitude,
                    order.customer.longitude, order.customer.address_text,
                )
            if rider.whatsapp_id:
                notify_rider_whatsapp(
                    rider.whatsapp_id, order.id, order.product, order.size or "",
                    shop.name, shop.latitude, shop.longitude, order.customer.latitude,
                    order.customer.longitude, order.customer.address_text,
                )
    return rider


def re_offer_next_rider(db: Session, order: models.Order, declined_rider_id: int) -> models.Rider | None:
    """Rider declined — offer to the next on-duty rider (excluding the one who declined)."""
    rider = db.query(models.Rider).filter(
        models.Rider.active == True,  # noqa: E712
        models.Rider.on_duty == True,  # noqa: E712
        models.Rider.id != declined_rider_id,
    ).first()
    if rider:
        order.rider_id = rider.id
        order.assigned_at = datetime.utcnow()
        db.commit()
        db.refresh(order)
        shop = order.shop
        if shop:
            if rider.telegram_id:
                notify_rider_new_order(
                    rider.telegram_id, order.id, order.product, order.size or "",
                    shop.name, shop.latitude, shop.longitude, order.customer.latitude,
                    order.customer.longitude, order.customer.address_text,
                )
            if rider.whatsapp_id:
                notify_rider_whatsapp(
                    rider.whatsapp_id, order.id, order.product, order.size or "",
                    shop.name, shop.latitude, shop.longitude, order.customer.latitude,
                    order.customer.longitude, order.customer.address_text,
                )
        return rider
    # no other rider available — put it back to pending for the admin board
    order.rider_id = None
    order.status = models.OrderStatus.pending
    order.assigned_at = None
    db.commit()
    db.refresh(order)
    return None


def mark_picked_up(db: Session, order: models.Order, remaining_stock: int | None = None):
    order.status = models.OrderStatus.picked_up
    order.picked_up_at = datetime.utcnow()
    # decrement shop stock now that the rider has taken a unit
    if order.shop_id:
        stock_item = db.query(models.StockItem).filter(
            models.StockItem.shop_id == order.shop_id,
            models.StockItem.product == order.product,
            models.StockItem.size == order.size,
        ).first()
        if stock_item:
            if remaining_stock is not None:
                # rider reported the actual remaining count at the shop — trust it
                if remaining_stock != stock_item.quantity - order.quantity:
                    record_stock_mismatch(db, order.shop_id)
                stock_item.quantity = max(0, remaining_stock)
            elif stock_item.quantity > 0:
                stock_item.quantity -= order.quantity
            stock_item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(order)
    return order


def mark_delivered(db: Session, order: models.Order, empty_returned: bool):
    order.status = models.OrderStatus.delivered
    order.empty_returned = empty_returned
    order.delivered_at = datetime.utcnow()
    if not empty_returned and order.deposit_fee == 0:
        # first-time customer kept the cylinder — a deposit should apply
        order.deposit_fee = pricing.deposit_for(order.product)
    db.commit()
    db.refresh(order)
    return order


def record_stock_check(db: Session, shop_id: int, items: list[schemas.StockUpdate]):
    """Used by the admin dashboard and the morning call/WhatsApp stock-check process."""
    for item in items:
        existing = db.query(models.StockItem).filter(
            models.StockItem.shop_id == shop_id,
            models.StockItem.product == item.product,
            models.StockItem.size == item.size,
        ).first()
        if existing:
            existing.quantity = item.quantity
            existing.confidence = item.confidence
            existing.updated_at = datetime.utcnow()
        else:
            db.add(models.StockItem(shop_id=shop_id, **item.model_dump()))
    db.commit()


def record_stock_mismatch(db: Session, shop_id: int):
    """A rider arrived and the shop's real stock didn't match the system's count.
    After 3 consecutive mismatches, fire an admin Telegram alert (spec section 6)."""
    shop = db.query(models.Shop).get(shop_id)
    if not shop:
        return
    shop.mismatch_count = (shop.mismatch_count or 0) + 1
    db.commit()
    if shop.mismatch_count >= 3:
        from notifications import notify_admin_alert
        notify_admin_alert(
            f"⚠️ Stock mismatch\n{shop.name} (id {shop.id}) has reported {shop.mismatch_count} "
            f"consecutive mismatches. Stock data may be stale — check with the shop."
        )


def rider_earnings(db: Session, rider_id: int, days: int = 7) -> dict:
    """Running earnings from delivered orders. Rider gets the delivery fee per order."""
    since = datetime.utcnow() - timedelta(days=days)
    orders = db.query(models.Order).filter(
        models.Order.rider_id == rider_id,
        models.Order.status == models.OrderStatus.delivered,
        models.Order.delivered_at >= since,
    ).all()
    total = sum(o.delivery_fee for o in orders)
    today = datetime.utcnow().date()
    today_total = sum(o.delivery_fee for o in orders if o.delivered_at and o.delivered_at.date() == today)
    return {
        "rider_id": rider_id,
        "period_days": days,
        "delivered_count": len(orders),
        "period_earnings_etb": total,
        "today_earnings_etb": today_total,
    }
