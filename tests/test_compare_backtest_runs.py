import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load_compare_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_backtest_runs.py"
    spec = importlib.util.spec_from_file_location("compare_backtest_runs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompareBacktestRunsProvenanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compare = _load_compare_module()

    def _run_dir(self, root: Path, provenance: dict | None, artifacts: dict | None = None) -> Path:
        run_dir = root / "run"
        (run_dir / "trades").mkdir(parents=True)
        (run_dir / "signals").mkdir(parents=True)
        (run_dir / "reports").mkdir(parents=True)
        if provenance is None:
            manifest = {"run_id": "run"}
        else:
            manifest = {"run_id": "run", "industry_provenance": provenance}
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        artifacts = artifacts or {}
        (run_dir / "trades" / "transactions.csv").write_text(
            artifacts.get("transactions", "symbol,price\n"), encoding="utf-8"
        )
        (run_dir / "trades" / "orders.csv").write_text(
            artifacts.get("orders", "symbol,price\n"), encoding="utf-8"
        )
        (run_dir / "signals" / "target_portfolio_signals.jsonl").write_text(
            artifacts.get("signals", ""), encoding="utf-8"
        )
        (run_dir / "reports" / "performance.csv").write_text(
            artifacts.get("performance", "date,value\n"), encoding="utf-8"
        )
        (run_dir / "reports" / "summary.json").write_text(
            artifacts.get("summary", json.dumps({})), encoding="utf-8"
        )
        return run_dir

    def test_same_provenance_compares_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = {"provider": "tushare", "compat": ""}
            baseline = self._run_dir(root / "base", provenance)
            candidate = self._run_dir(root / "cand", provenance)
            result = self.compare.main([str(baseline), str(candidate)])
            self.assertEqual(result, 0)

    def test_different_provenance_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = self._run_dir(
                root / "base", {"provider": "tushare", "compat": ""}
            )
            candidate = self._run_dir(
                root / "cand", {"provider": "joinquant_full", "compat": ""}
            )
            with self.assertRaises(SystemExit) as ctx:
                self.compare._check_provenance_comparable(baseline, candidate)
            self.assertEqual(ctx.exception.code, 1)

    def test_missing_provenance_on_one_side_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = self._run_dir(root / "base", {"provider": "tushare", "compat": ""})
            candidate = self._run_dir(root / "cand", None)
            with self.assertRaises(SystemExit) as ctx:
                self.compare._check_provenance_comparable(baseline, candidate)
            self.assertEqual(ctx.exception.code, 1)

    def test_both_missing_provenance_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = self._run_dir(root / "base", None)
            candidate = self._run_dir(root / "cand", None)
            self.compare._check_provenance_comparable(baseline, candidate)

    def test_provenance_signature_rejects_missing_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = self._run_dir(
                root / "base",
                {
                    "provider": "tushare",
                    "compat": "",
                    "sw_member_all_rows": 7893,
                    "jq_classification_sha256": None,
                },
            )
            candidate = self._run_dir(
                root / "cand",
                {
                    "provider": "tushare",
                    "compat": "",
                    "sw_member_all_rows": 0,
                    "jq_classification_sha256": None,
                },
            )
            with self.assertRaises(SystemExit):
                self.compare._check_provenance_comparable(baseline, candidate)

    def test_compat_flag_difference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = self._run_dir(
                root / "base", {"provider": "tushare", "compat": ""}
            )
            candidate = self._run_dir(
                root / "cand", {"provider": "tushare", "compat": "stock_basic_as_sw_l1"}
            )
            with self.assertRaises(SystemExit) as ctx:
                self.compare._check_provenance_comparable(baseline, candidate)
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
