def trade_lot_size(security: str, side: str) -> int:
    _ = side
    if str(security).startswith("688"):
        return 200
    return 100


def round_lot_amount(security: str, amount: int, side: str) -> int:
    lot = trade_lot_size(security, side)
    sign = 1 if amount >= 0 else -1
    rounded = (abs(int(amount)) // lot) * lot
    return sign * rounded
