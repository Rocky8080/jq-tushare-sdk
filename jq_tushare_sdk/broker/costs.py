from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    open_commission: float = 0.0003
    close_commission: float = 0.0003
    close_tax: float = 0.001
    min_commission: float = 5.0
    slippage_rate: float = 0.0
    slippage_fixed: float = 0.0

    def commission(self, value: float, side: str) -> float:
        if side not in {"buy", "sell"}:
            raise NotImplementedError(f"Unsupported order side: {side}")
        if abs(value) <= 0:
            return 0.0
        rate = self.open_commission if side == "buy" else self.close_commission
        return max(abs(value) * rate, self.min_commission)

    def tax(self, value: float, side: str) -> float:
        if side not in {"buy", "sell"}:
            raise NotImplementedError(f"Unsupported order side: {side}")
        return abs(value) * self.close_tax if side == "sell" else 0.0

    def transfer_fee(self, value: float, side: str, security: str) -> float:
        _ = (value, side, security)
        return 0.0
