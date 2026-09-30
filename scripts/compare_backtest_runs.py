import argparse
import csv
import json
from pathlib import Path


DYNAMIC_ORDER_FIELDS = {"entrust_id"}
DYNAMIC_SIGNAL_FIELDS = {"portfolio_seq", "run_id", "run_dir"}


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


def _industry_provenance(run_dir: Path) -> dict | None:
    """Read industry provenance from a run, falling back to config.json.

    Returns ``None`` when the run predates provenance recording, in which case
    the semantic cannot be reconstructed from the artifacts alone.
    """
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        provenance = payload.get("industry_provenance")
        if isinstance(provenance, dict):
            return provenance
    config_path = run_dir / "config.json"
    if config_path.is_file():
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        provenance = payload.get("industry_provenance")
        if isinstance(provenance, dict):
            return provenance
    return None


def _provenance_signature(provenance: dict | None) -> tuple | None:
    if provenance is None:
        return None
    provider = provenance.get("provider")
    compat = provenance.get("compat", "")
    if compat == "stock_basic_as_sw_l1":
        return (compat,)
    if provider == "joinquant_full":
        return (provider, compat, provenance.get("jq_classification_sha256"), provenance.get("jq_member_sha256"))
    # Counts may grow without changing the provider, but an empty member table
    # switches the portal's lookup path and must not compare as the same semantic.
    membership = tuple(
        bool(provenance.get(field)) if provenance.get(field) is not None else None
        for field in ("sw_member_all_rows", "sw_member_rows")
    )
    taxonomy = provenance.get("jq_classification_sha256") if provider == "joinquant_taxonomy" else None
    return (provider, compat, membership, taxonomy)


def _check_provenance_comparable(baseline: Path, candidate: Path) -> None:
    """Refuse cross-semantic comparisons before comparing any artifacts."""
    baseline_prov = _industry_provenance(baseline)
    candidate_prov = _industry_provenance(candidate)
    if _provenance_signature(baseline_prov) is None and _provenance_signature(candidate_prov) is None:
        print(
            json.dumps(
                {
                    "warning": (
                        "Neither run records industry provenance (pre-0.10.33 runs); "
                        "industry semantic is not comparable."
                    )
                },
                ensure_ascii=False,
            )
        )
        return
    if _provenance_signature(baseline_prov) is None or _provenance_signature(candidate_prov) is None:
        print(
            json.dumps(
                {
                    "equivalent": False,
                    "file": "manifest.json",
                    "reason": (
                        "industry provenance missing on one run; "
                        "cannot establish comparable industry semantics"
                    ),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
    if _provenance_signature(baseline_prov) != _provenance_signature(candidate_prov):
        print(
            json.dumps(
                {
                    "equivalent": False,
                    "file": "manifest.json",
                    "reason": "industry provenance differs",
                    "baseline": baseline_prov,
                    "candidate": candidate_prov,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare transparent backtest run artifacts.")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)

    _check_provenance_comparable(args.baseline, args.candidate)

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
