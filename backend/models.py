import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Enum, Text
)
from sqlalchemy.orm import relationship
from database import Base


class OrderStatus(str, enum.Enum):
    pending = "pending"          # placed, waiting for rider assignment
    assigned = "assigned"        # rider accepted, heading to shop
    picked_up = "picked_up"      # rider has the full cylinder/jar
    delivered = "delivered"      # swap complete, order finished
    cancelled = "cancelled"


class PaymentMethod(str, enum.Enum):
    telebirr = "telebirr"
    cbe = "cbe"
    cash = "cash"                # cash on delivery


class Shop(Base):
    __tablename__ = "shops"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    area = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    mismatch_count = Column(Integer, default=0)  # consecutive stock mismatches
    active = Column(Boolean, default=True)

    stock_items = relationship("StockItem", back_populates="shop", cascade="all, delete-orphan")


class StockItem(Base):
    __tablename__ = "stock_items"
    id = Column(Integer, primary_key=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    product = Column(String, nullable=False)   # gas | water | butane
    brand = Column(String, nullable=True)       # NOC, Ghion, etc. (null for water)
    size = Column(String, nullable=True)        # 6kg, 12kg, 22kg, jar
    quantity = Column(Integer, default=0)
    confidence = Column(String, default="confirmed")  # confirmed | estimated
    updated_at = Column(DateTime, default=datetime.utcnow)

    shop = relationship("Shop", back_populates="stock_items")


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=True)
    phone = Column(String, unique=True, nullable=False)
    telegram_id = Column(String, unique=True, nullable=True)
    whatsapp_id = Column(String, unique=True, nullable=True)
    address_text = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    orders = relationship("Order", back_populates="customer")


class Rider(Base):
    __tablename__ = "riders"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, unique=True, nullable=False)
    telegram_id = Column(String, unique=True, nullable=True)
    whatsapp_id = Column(String, unique=True, nullable=True)
    active = Column(Boolean, default=True)
    on_duty = Column(Boolean, default=False)

    orders = relationship("Order", back_populates="rider")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=True)
    rider_id = Column(Integer, ForeignKey("riders.id"), nullable=True)

    product = Column(String, nullable=False)   # gas | water | butane
    brand = Column(String, nullable=True)
    size = Column(String, nullable=True)
    is_exchange = Column(Boolean, default=True)   # False = first-time order, needs deposit
    quantity = Column(Integer, default=1)

    product_price = Column(Float, default=0)
    delivery_fee = Column(Float, default=150)
    deposit_fee = Column(Float, default=0)
    total_price = Column(Float, default=0)

    status = Column(Enum(OrderStatus), default=OrderStatus.pending)
    paid = Column(Boolean, default=False)
    empty_returned = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)

    # Payment + lifecycle metadata (spec sections 3.1, 6)
    payment_method = Column(Enum(PaymentMethod), nullable=True)
    payment_ref = Column(String, nullable=True)        # provider transaction reference
    eta_minutes = Column(Integer, nullable=True)       # estimated delivery window
    assigned_at = Column(DateTime, nullable=True)      # when a rider accepted
    picked_up_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="orders")
    shop = relationship("Shop")
    rider = relationship("Rider", back_populates="orders")
