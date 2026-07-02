"""
Order-flow orchestration — the single source of truth for what happens when an
order is placed, accepted, picked up, or delivered.

Both the public HTTP API (main.py) and the in-process channel routers
(telegram_router, whatsapp_router) call these functions, so the business rules
live in exactly one place. The routers never touch the DB directly beyond what
these functions do.
"""
from datetime import datetime
from sqlalchemy.orm import Session

import models
import schemas
import crud
import payments
from ratelimit import order_limiter
from notifications import (
    notify_customer_status, notify_customer_payment_failed,
)


def place_order(db: Session, payload: schemas.OrderCreate, rate_key: str | None = None) -> models.Order:
    """Full place-order flow: rate-limit check, create, take payment, offer to a
    rider, and notify the customer. Returns the created order."""
    if rate_key and not order_limiter.allow(rate_key):
        from fastapi import HTTPException
        raise HTTPException(status_code=429, detail="Too many orders. Please wait a moment and try again.")

    order = crud.create_order(db, payload)

    if payload.payment_method:
        method = _parse_payment_method(payload.payment_method)
        payments.initiate_payment(order, method)
        db.commit()
        db.refresh(order)
        if method != models.PaymentMethod.cash:
            order.paid = True
            db.commit()
            db.refresh(order)

    rider = crud.offer_order_to_rider(db, order)  # also pushes the alert to the rider

    customer = db.query(models.Customer).get(order.customer_id)
    if customer and customer.telegram_id:
        notify_customer_status(customer.telegram_id, order.id, "assigned",
                               rider_name=rider.name if rider else None,
                               eta_minutes=order.eta_minutes)
    return order


def accept_order(db: Session, order: models.Order, rider: models.Rider) -> models.Order:
    order.status = models.OrderStatus.assigned
    order.assigned_at = datetime.utcnow()
    db.commit()
    db.refresh(order)
    customer = db.query(models.Customer).get(order.customer_id)
    if customer and customer.telegram_id:
        notify_customer_status(customer.telegram_id, order.id, "assigned",
                               rider_name=rider.name, eta_minutes=order.eta_minutes)
    return order


def decline_order(db: Session, order: models.Order, rider: models.Rider) -> models.Order:
    crud.re_offer_next_rider(db, order, order.rider_id or rider.id)
    return order


def pickup_order(db: Session, order: models.Order, remaining_stock: int | None = None) -> models.Order:
    order = crud.mark_picked_up(db, order, remaining_stock=remaining_stock)
    customer = db.query(models.Customer).get(order.customer_id)
    if customer and customer.telegram_id:
        notify_customer_status(customer.telegram_id, order.id, "picked_up")
    return order


def deliver_order(db: Session, order: models.Order, empty_returned: bool) -> models.Order:
    order = crud.mark_delivered(db, order, empty_returned=empty_returned)
    # cash-on-delivery orders are paid once delivered
    if order.payment_method == models.PaymentMethod.cash and not order.paid:
        order.paid = True
        db.commit()
        db.refresh(order)
    customer = db.query(models.Customer).get(order.customer_id)
    if customer and customer.telegram_id:
        notify_customer_status(customer.telegram_id, order.id, "delivered")
    return order


def mark_payment_failed(db: Session, order: models.Order) -> models.Order:
    order.paid = False
    db.commit()
    db.refresh(order)
    customer = db.query(models.Customer).get(order.customer_id)
    if customer and customer.telegram_id:
        notify_customer_payment_failed(customer.telegram_id, order.id)
    return order


def _parse_payment_method(value: str) -> models.PaymentMethod:
    try:
        return models.PaymentMethod(value)
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(400, f"Unknown payment method '{value}'. Use telebirr, cbe, or cash.")
