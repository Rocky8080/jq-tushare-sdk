from dataclasses import dataclass
from datetime import datetime


@dataclass
class Order:
    order_id: str
    security: str
    amount: int
    side: str
    price: float
    status: str
    filled: int = 0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    dividend_tax: float = 0.0
    created_at: datetime | None = None
    reason: str = ""


@dataclass
class Trade:
    trade_id: str
    order_id: str
    security: str
    side: str
    amount: int
    price: float
    value: float
    commission: float
    stamp_tax: float
    transfer_fee: float = 0.0
    dividend_tax: float = 0.0
    traded_at: datetime | None = None
    reason: str = ""
    realized_pnl: float = 0.0
