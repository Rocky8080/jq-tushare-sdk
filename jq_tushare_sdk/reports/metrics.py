def performance_row(
    date: str,
    portfolio,
    initial_cash: float,
    previous_total: float | None,
    peak_value: float,
) -> dict:
    total = float(portfolio.total_value)
    cash = float(portfolio.cash)
    positions_value = float(portfolio.positions_value)
    cumulative = total / initial_cash - 1 if initial_cash else 0.0
    daily = total / previous_total - 1 if previous_total else cumulative
    drawdown = total / peak_value - 1 if peak_value else 0.0
    return {
        "date": date,
        "total_value": f"{total:.2f}",
        "cash": f"{cash:.2f}",
        "positions_value": f"{positions_value:.2f}",
        "daily_return": f"{daily:.6f}",
        "cumulative_return": f"{cumulative:.6f}",
        "benchmark_return": "unsupported_placeholder",
        "excess_return": "unsupported_placeholder",
        "drawdown": f"{drawdown:.6f}",
    }
