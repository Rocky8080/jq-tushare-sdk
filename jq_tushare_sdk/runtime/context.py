from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RunParams:
    type: str = "backtest"


@dataclass
class Position:
    code: str
    total_amount: int = 0
    closeable_amount: int = 0
    avg_cost: float = 0.0
    price: float = 0.0

    @property
    def value(self) -> float:
        return float(self.total_amount) * float(self.price)


class Positions(dict[str, Position]):
    def __iter__(self):
        return iter(list(super().keys()))

    def items(self):
        return list(super().items())

    def values(self):
        return list(super().values())


@dataclass
class Portfolio:
    initial_cash: float
    available_cash: float | None = None
    positions: Positions = field(default_factory=Positions)

    def __post_init__(self):
        if self.available_cash is None:
            self.available_cash = float(self.initial_cash)
        if not isinstance(self.positions, Positions):
            self.positions = Positions(self.positions)

    @property
    def cash(self) -> float:
        return float(self.available_cash or 0.0)

    @property
    def positions_value(self) -> float:
        return sum(position.value for position in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.positions_value

    @property
    def portfolio_value(self) -> float:
        return self.total_value


@dataclass
class Context:
    portfolio: Portfolio
    current_dt: datetime | None = None
    previous_date: datetime | None = None
    run_params: RunParams = field(default_factory=RunParams)
