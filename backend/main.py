import os
import secrets
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

import models
import schemas
import crud
import payments
import services
from database import engine, get_db, Base
from notifications import notify_customer_status, check_stalled_deliveries
import telegram_router
import whatsapp_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Nora API")
app.include_router(telegram_router.router)
app.include_router(whatsapp_router.router)

# CORS: tighten to your real domains before going live. Set ALLOWED_ORIGINS env
# var as a comma-separated list, e.g. "https://nora.onrender.com,https://app.nora.et"
allowed = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "changeme")


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(status_code=401, detail="Invalid admin credentials",
                            headers={"WWW-Authenticate": "Basic"})
    return True


# ---------------- Orders (used by Telegram/WhatsApp bots and the web app) ----------------

@app.post("/orders", response_model=schemas.OrderOut)
def create_order(payload: schemas.OrderCreate, request: Request, db: Session = Depends(get_db)):
    rate_key = f"{payload.customer_phone}:{request.client.host if request.client else ''}"
    return services.place_order(db, payload, rate_key=rate_key)


@app.get("/orders/{order_id}", response_model=schemas.OrderOut)
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.query(models.Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return order


@app.post("/orders/{order_id}/status", response_model=schemas.OrderOut)
def update_order_status(order_id: int, payload: schemas.OrderStatusUpdate, db: Session = Depends(get_db)):
    order = db.query(models.Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    if payload.paid is not None:
        order.paid = payload.paid
    if payload.rider_id is not None:
        order.rider_id = payload.rider_id

    if payload.status == "picked_up":
        order = services.pickup_order(db, order, remaining_stock=payload.remaining_stock)
    elif payload.status == "delivered":
        order = services.deliver_order(db, order, empty_returned=bool(payload.empty_returned))
    elif payload.status:
        order.status = models.OrderStatus(payload.status)
        db.commit()
        db.refresh(order)

    customer = db.query(models.Customer).get(order.customer_id)
    if customer and customer.telegram_id and payload.status:
        rider = db.query(models.Rider).get(order.rider_id) if order.rider_id else None
        notify_customer_status(customer.telegram_id, order.id, payload.status,
                               rider_name=rider.name if rider else None,
                               eta_minutes=order.eta_minutes)

    return order


# ---------------- Rider accept / decline ----------------

@app.post("/orders/{order_id}/accept", response_model=schemas.OrderOut)
def rider_accept(order_id: int, payload: schemas.AcceptDeclineRequest, db: Session = Depends(get_db)):
    order = db.query(models.Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    rider = db.query(models.Rider).filter(models.Rider.telegram_id == payload.rider_telegram_id).first()
    if not rider:
        raise HTTPException(404, "Rider not found")
    if order.rider_id != rider.id:
        raise HTTPException(409, "This order was offered to another rider.")
    return services.accept_order(db, order, rider)


@app.post("/orders/{order_id}/decline", response_model=schemas.OrderOut)
def rider_decline(order_id: int, payload: schemas.AcceptDeclineRequest, db: Session = Depends(get_db)):
    order = db.query(models.Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    rider = db.query(models.Rider).filter(models.Rider.telegram_id == payload.rider_telegram_id).first()
    if not rider:
        raise HTTPException(404, "Rider not found")
    return services.decline_order(db, order, rider)


# ---------------- Payment ----------------

def _parse_payment_method(value: str) -> models.PaymentMethod:
    try:
        return models.PaymentMethod(value)
    except ValueError:
        raise HTTPException(400, f"Unknown payment method '{value}'. Use telebirr, cbe, or cash.")


@app.post("/orders/{order_id}/pay", response_model=schemas.PaymentResponse)
def initiate_payment(order_id: int, payload: schemas.PaymentRequest, db: Session = Depends(get_db)):
    order = db.query(models.Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    method = _parse_payment_method(payload.method)
    result = payments.initiate_payment(order, method)
    if method != models.PaymentMethod.cash:
        order.paid = True
    db.commit()
    db.refresh(order)
    return schemas.PaymentResponse(order_id=order.id, **result)


@app.post("/payments/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """Provider callback (TeleBirr/CBE Birr) confirming a payment.
    Stub: accepts {order_id, reference, status} and marks the order paid.
    Replace the body with real signature verification from your provider."""
    data = await request.json()
    order_id = data.get("order_id")
    ref = data.get("reference")
    status_val = data.get("status", "success")
    if not order_id:
        return JSONResponse({"ok": False, "error": "order_id required"}, status_code=400)
    order = db.query(models.Order).get(order_id)
    if not order:
        return JSONResponse({"ok": False, "error": "order not found"}, status_code=404)
    if status_val == "success":
        order.paid = True
        order.payment_ref = ref or order.payment_ref
        db.commit()
    elif status_val == "failed":
        services.mark_payment_failed(db, order)
    return {"ok": True}


# ---------------- Shops ----------------

@app.get("/shops", response_model=List[schemas.ShopOut])
def list_shops(db: Session = Depends(get_db)):
    return db.query(models.Shop).all()


@app.post("/shops", response_model=schemas.ShopOut)
def create_shop(payload: schemas.ShopCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    shop = models.Shop(**payload.model_dump())
    db.add(shop)
    db.commit()
    db.refresh(shop)
    return shop


@app.get("/shops/{shop_id}/stock", response_model=List[schemas.StockItemOut])
def get_shop_stock(shop_id: int, db: Session = Depends(get_db)):
    shop = db.query(models.Shop).get(shop_id)
    if not shop:
        raise HTTPException(404, "Shop not found")
    return shop.stock_items


@app.post("/shops/{shop_id}/stock")
def update_stock(shop_id: int, items: List[schemas.StockUpdate], db: Session = Depends(get_db), _=Depends(require_admin)):
    shop = db.query(models.Shop).get(shop_id)
    if not shop:
        raise HTTPException(404, "Shop not found")
    crud.record_stock_check(db, shop_id, items)
    return {"ok": True}


# ---------------- Riders ----------------

@app.get("/riders", response_model=List[schemas.RiderOut])
def list_riders(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(models.Rider).all()


@app.post("/riders", response_model=schemas.RiderOut)
def create_rider(payload: schemas.RiderCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    rider = models.Rider(**payload.model_dump())
    db.add(rider)
    db.commit()
    db.refresh(rider)
    return rider


@app.post("/riders/{rider_id}/duty")
def toggle_duty(rider_id: int, on_duty: bool, db: Session = Depends(get_db)):
    rider = db.query(models.Rider).get(rider_id)
    if not rider:
        raise HTTPException(404, "Rider not found")
    rider.on_duty = on_duty
    db.commit()
    return {"ok": True, "on_duty": rider.on_duty}


@app.get("/riders/by_telegram/{telegram_id}", response_model=schemas.RiderOut)
def get_rider_by_telegram(telegram_id: str, db: Session = Depends(get_db)):
    rider = db.query(models.Rider).filter(models.Rider.telegram_id == telegram_id).first()
    if not rider:
        raise HTTPException(404, "Rider not found")
    return rider


@app.get("/riders/{rider_id}/earnings", response_model=schemas.EarningsOut)
def rider_earnings(rider_id: int, days: int = 7, db: Session = Depends(get_db)):
    rider = db.query(models.Rider).get(rider_id)
    if not rider:
        raise HTTPException(404, "Rider not found")
    return crud.rider_earnings(db, rider_id, days)


@app.get("/riders/by_telegram/{telegram_id}/earnings", response_model=schemas.EarningsOut)
def rider_earnings_by_telegram(telegram_id: str, days: int = 7, db: Session = Depends(get_db)):
    rider = db.query(models.Rider).filter(models.Rider.telegram_id == telegram_id).first()
    if not rider:
        raise HTTPException(404, "Rider not found")
    return crud.rider_earnings(db, rider.id, days)


# ---------------- Admin dashboard data ----------------

@app.post("/admin/orders/{order_id}/reoffer", response_model=schemas.OrderOut)
def admin_reoffer_order(order_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Re-dispatch a pending/stalled order to an on-duty rider."""
    order = db.query(models.Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    rider = crud.offer_order_to_rider(db, order)
    if not rider:
        raise HTTPException(409, "No on-duty rider available.")
    return order


@app.get("/admin/orders", response_model=List[schemas.OrderOut])
def admin_list_orders(
    status: Optional[str] = None,
    rider_id: Optional[int] = None,
    shop_id: Optional[int] = None,
    product: Optional[str] = None,
    db: Session = Depends(get_db), _=Depends(require_admin),
):
    q = db.query(models.Order).order_by(models.Order.created_at.desc())
    if status:
        q = q.filter(models.Order.status == status)
    if rider_id:
        q = q.filter(models.Order.rider_id == rider_id)
    if shop_id:
        q = q.filter(models.Order.shop_id == shop_id)
    if product:
        q = q.filter(models.Order.product == product)
    return q.limit(200).all()


@app.get("/admin/stats")
def admin_stats(db: Session = Depends(get_db), _=Depends(require_admin)):
    total_orders = db.query(func.count(models.Order.id)).scalar()
    active_riders = db.query(func.count(models.Rider.id)).filter(models.Rider.on_duty == True).scalar()  # noqa: E712
    pending = db.query(func.count(models.Order.id)).filter(models.Order.status == models.OrderStatus.pending).scalar()
    # also surface stalled deliveries on each stats read (cheap on a pilot)
    check_stalled_deliveries(db)
    revenue = db.query(func.coalesce(func.sum(models.Order.total_price), 0)).filter(
        models.Order.paid == True  # noqa: E712
    ).scalar()
    return {
        "total_orders": total_orders,
        "active_riders": active_riders,
        "pending_orders": pending,
        "revenue_etb": float(revenue),
    }


@app.get("/admin/stock")
def admin_stock(db: Session = Depends(get_db), _=Depends(require_admin)):
    """All stock across shops, for the dashboard's stock table."""
    rows = db.query(models.StockItem).join(models.Shop).order_by(models.Shop.id).all()
    return [
        {
            "shop_id": s.shop_id, "shop_name": s.shop.name if s.shop else None,
            "product": s.product, "brand": s.brand, "size": s.size,
            "quantity": s.quantity, "confidence": s.confidence,
        }
        for s in rows
    ]


@app.get("/admin/deposits")
def admin_deposits(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Deposit reconciliation — orders holding a refundable deposit (spec section 5)."""
    rows = db.query(models.Order).filter(models.Order.deposit_fee > 0).order_by(
        models.Order.created_at.desc()
    ).limit(200).all()
    return [
        {
            "order_id": o.id, "product": o.product, "size": o.size,
            "deposit_fee": o.deposit_fee, "empty_returned": o.empty_returned,
            "status": o.status, "customer_id": o.customer_id,
            "shop_id": o.shop_id, "rider_id": o.rider_id,
        }
        for o in rows
    ]


@app.get("/admin/report")
def admin_report(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Reporting: revenue by category, delivery time trends, shop/rider performance."""
    by_product = db.query(
        models.Order.product,
        func.coalesce(func.sum(models.Order.total_price), 0),
        func.count(models.Order.id),
    ).group_by(models.Order.product).all()
    shops = db.query(
        models.Shop.id, models.Shop.name,
        func.count(models.Order.id),
        func.coalesce(func.sum(models.Order.total_price), 0),
    ).join(models.Order, models.Order.shop_id == models.Shop.id, isouter=True).group_by(models.Shop.id).all()
    riders = db.query(
        models.Rider.id, models.Rider.name,
        func.count(models.Order.id),
        func.coalesce(func.sum(models.Order.delivery_fee), 0),
    ).join(models.Order, models.Order.rider_id == models.Rider.id, isouter=True).filter(
        models.Order.status == models.OrderStatus.delivered
    ).group_by(models.Rider.id).all()
    delivered = db.query(models.Order).filter(
        models.Order.status == models.OrderStatus.delivered,
        models.Order.assigned_at.isnot(None),
        models.Order.delivered_at.isnot(None),
    ).all()
    avg_mins = None
    if delivered:
        deltas = [(o.delivered_at - o.assigned_at).total_seconds() / 60 for o in delivered]
        avg_mins = round(sum(deltas) / len(deltas), 1)
    return {
        "revenue_by_product": [
            {"product": p, "revenue_etb": float(r), "orders": int(c)} for p, r, c in by_product
        ],
        "shop_performance": [
            {"shop_id": i, "name": n, "orders": int(c), "revenue_etb": float(r)} for i, n, c, r in shops
        ],
        "rider_performance": [
            {"rider_id": i, "name": n, "delivered": int(c), "earnings_etb": float(r)} for i, n, c, r in riders
        ],
        "avg_delivery_minutes": avg_mins,
        "delivered_count": len(delivered),
    }


# ---------------- Serve the admin web dashboard as static files ----------------
admin_dir = os.path.join(os.path.dirname(__file__), "..", "admin_web")
if os.path.isdir(admin_dir):
    app.mount("/admin", StaticFiles(directory=admin_dir, html=True), name="admin")


landing_path = os.path.join(os.path.dirname(__file__), "..", "landing.html")


@app.get("/")
def landing():
    """Public marketing landing page (the apex URL). Edit landing.html in the
    project root — replace the WhatsApp number and confirm prices match
    pricing.py before going live."""
    if os.path.isfile(landing_path):
        return FileResponse(landing_path)
    return JSONResponse({"status": "Nora API is running", "admin_dashboard": "/admin"})


@app.get("/health")
def health():
    """JSON health/status check for monitoring and the deploy verify step."""
    return {"status": "Nora API is running", "admin_dashboard": "/admin"}


@app.get("/telegram/set-webhook")
def set_telegram_webhook(url: str, _=Depends(require_admin)):
    """One-time setup: after deploying, visit
    https://your-deployed-url/telegram/set-webhook?url=https://your-deployed-url/telegram/webhook
    (with admin basic auth) to point Telegram at this app."""
    return telegram_router.set_webhook(url)
