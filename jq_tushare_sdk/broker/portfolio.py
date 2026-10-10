def is_star_security(security: str) -> bool:
    return str(security).split(".", 1)[0].startswith(("688", "689"))


def minimum_trade_amount(security: str, side: str) -> int:
    _ = side
    return 200 if is_star_security(security) else 100


def trade_lot_size(security: str, side: str) -> int:
    """Quantity increment; this is separate from the minimum order quantity."""
    _ = side
    return 1 if is_star_security(security) else 100


def round_lot_amount(security: str, amount: int, side: str) -> int:
    amount = int(amount)
    quantity = abs(amount)
    if quantity < minimum_trade_amount(security, side):
        return 0
    step = trade_lot_size(security, side)
    rounded = quantity // step * step
    return rounded if amount >= 0 else -rounded
