from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel


class ShopCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    area: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ShopOut(ShopCreate):
    id: int
    mismatch_count: int
    active: bool
    class Config:
        from_attributes = True


class StockUpdate(BaseModel):
    product: str
    brand: Optional[str] = None
    size: Optional[str] = None
    quantity: int
    confidence: str = "confirmed"


class StockItemOut(BaseModel):
    id: int
    shop_id: int
    product: str
    brand: Optional[str]
    size: Optional[str]
    quantity: int
    confidence: str
    class Config:
        from_attributes = True


class CustomerCreate(BaseModel):
    name: Optional[str] = None
    phone: str
    telegram_id: Optional[str] = None
    whatsapp_id: Optional[str] = None
    address_text: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class CustomerOut(CustomerCreate):
    id: int
    class Config:
        from_attributes = True


class RiderCreate(BaseModel):
    name: str
    phone: str
    telegram_id: Optional[str] = None
    whatsapp_id: Optional[str] = None


class RiderOut(RiderCreate):
    id: int
    active: bool
    on_duty: bool
    class Config:
        from_attributes = True


class OrderCreate(BaseModel):
    customer_phone: str
    customer_name: Optional[str] = None
    telegram_id: Optional[str] = None
    whatsapp_id: Optional[str] = None
    address_text: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    product: str          # gas | water | butane
    brand: Optional[str] = None
    size: Optional[str] = None
    is_exchange: bool = True
    quantity: int = 1
    payment_method: Optional[str] = None   # telebirr | cbe | cash


class OrderStatusUpdate(BaseModel):
    status: Optional[str] = None
    paid: Optional[bool] = None
    empty_returned: Optional[bool] = None
    rider_id: Optional[int] = None
    remaining_stock: Optional[int] = None   # rider's on-site stock report at pickup


class OrderOut(BaseModel):
    id: int
    product: str
    brand: Optional[str]
    size: Optional[str]
    is_exchange: bool
    quantity: int
    product_price: float
    delivery_fee: float
    deposit_fee: float
    total_price: float
    status: str
    paid: bool
    empty_returned: bool
    shop_id: Optional[int]
    rider_id: Optional[int]
    customer_id: int
    payment_method: Optional[str]
    payment_ref: Optional[str]
    eta_minutes: Optional[int]
    created_at: datetime
    assigned_at: Optional[datetime]
    delivered_at: Optional[datetime]
    class Config:
        from_attributes = True


class PaymentRequest(BaseModel):
    method: str            # telebirr | cbe | cash
    rider_telegram_id: Optional[str] = None   # who is paying/collecting context


class PaymentResponse(BaseModel):
    order_id: int
    method: str
    reference: str
    amount_etb: float
    checkout_url: Optional[str] = None
    instructions: Optional[str] = None
    requires_redirect: bool


class AcceptDeclineRequest(BaseModel):
    rider_telegram_id: str


class EarningsOut(BaseModel):
    rider_id: int
    period_days: int
    delivered_count: int
    period_earnings_etb: float
    today_earnings_etb: float
