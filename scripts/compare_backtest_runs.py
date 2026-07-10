import argparse
import csv
import json
from pathlib import Path


DYNAMIC_ORDER_FIELDS = {"entrust_id"}
DYNAMIC_SIGNAL_FIELDS = {"run_id", "run_dir"}


def normalized_csv(path: Path, ignored: set[str] | None = None) -> list[dict]:
    ignored = ignored or set()
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            {key: value for key, value in row.items() if key not in ignored}
            for row in csv.DictReader(handle)
        ]


def drop_keys(value, ignored: set[str]):
    if isinstance(value, dict):
        return {
            key: drop_keys(item, ignored)
            for key, item in value.items()
            if key not in ignored
        }
    if isinstance(value, list):
        return [drop_keys(item, ignored) for item in value]
    return value


def normalized_jsonl(path: Path, ignored: set[str]) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(drop_keys(json.loads(line), ignored))
    return rows


def normalized_summary(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("performance_profile", None)
    return payload


def _comparisons(run_dir: Path):
    return (
        ("transactions.csv", run_dir / "trades" / "transactions.csv", lambda path: normalized_csv(path, DYNAMIC_ORDER_FIELDS)),
        ("orders.csv", run_dir / "trades" / "orders.csv", lambda path: normalized_csv(path, DYNAMIC_ORDER_FIELDS)),
        (
            "target_portfolio_signals.jsonl",
            run_dir / "signals" / "target_portfolio_signals.jsonl",
            lambda path: normalized_jsonl(path, DYNAMIC_SIGNAL_FIELDS),
        ),
        ("performance.csv", run_dir / "reports" / "performance.csv", normalized_csv),
        ("summary.json", run_dir / "reports" / "summary.json", normalized_summary),
    )


def _first_difference(expected, actual):
    if type(expected) is not type(actual):
        return {"expected": expected, "actual": actual}
    if isinstance(expected, list):
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            difference = _first_difference(expected_item, actual_item)
            if difference is not None:
                return {"index": index, **difference}
        if len(expected) != len(actual):
            return {"expected_length": len(expected), "actual_length": len(actual)}
        return None
    if isinstance(expected, dict):
        keys = list(expected)
        keys.extend(key for key in actual if key not in expected)
        for key in keys:
            if key not in expected or key not in actual:
                return {"key": key, "expected": expected.get(key), "actual": actual.get(key)}
            difference = _first_difference(expected[key], actual[key])
            if difference is not None:
                return {"key": key, **difference}
        return None
    if expected != actual:
        return {"expected": expected, "actual": actual}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare transparent backtest run artifacts.")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()

    for name, baseline_path, loader in _comparisons(args.baseline):
        candidate_path = args.candidate / baseline_path.relative_to(args.baseline)
        try:
            baseline = loader(baseline_path)
            candidate = loader(candidate_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"equivalent": False, "file": name, "difference": str(exc)}))
            return 1
        difference = _first_difference(baseline, candidate)
        if difference is not None:
            print(json.dumps({"equivalent": False, "file": name, "difference": difference}, ensure_ascii=False))
            return 1
    print(json.dumps({"equivalent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
