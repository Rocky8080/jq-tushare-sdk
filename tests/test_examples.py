import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExamplesTest(unittest.TestCase):
    def test_dual_ma_momentum_baseline_is_local_sdk_compatible(self):
        path = ROOT / "examples" / "joinquant_dual_ma_momentum_baseline.py"
        self.assertTrue(path.is_file(), path)
        source = path.read_text(encoding="utf-8")

        ast.parse(source)

        self.assertIn("def initialize(context):", source)
        self.assertIn("def rebalance(context):", source)
        self.assertIn("attribute_history(", source)
        self.assertIn("order_target_value(", source)
        self.assertNotIn("jqfactor", source)
        self.assertNotIn("calc_factors", source)
        self.assertNotIn("reference_security", source)

    def test_dual_ma_momentum_baseline_does_not_rebalance_existing_long_daily(self):
        module = self._load_dual_ma_template()
        orders = []
        module.dual_ma_signal = lambda _security: {
            "short_ma": 2.0,
            "long_ma": 1.0,
            "invested": True,
        }
        module.order_target_value = lambda security, value: orders.append((security, value))
        module.record = lambda **_kwargs: None
        module.log = types.SimpleNamespace(info=lambda *_args, **_kwargs: None)
        context = types.SimpleNamespace(
            portfolio=types.SimpleNamespace(
                total_value=100000.0,
                positions={
                    module.TARGET_SECURITY: types.SimpleNamespace(total_amount=20300),
                },
            )
        )

        module.rebalance(context)

        self.assertEqual(orders, [])

    def _load_dual_ma_template(self):
        path = ROOT / "examples" / "joinquant_dual_ma_momentum_baseline.py"
        jqdata = types.ModuleType("jqdata")
        jqdata.__all__ = [
            "attribute_history",
            "FixedSlippage",
            "log",
            "OrderCost",
            "order_target_value",
            "record",
            "run_daily",
            "set_benchmark",
            "set_option",
            "set_order_cost",
            "set_slippage",
        ]
        jqdata.attribute_history = lambda *_args, **_kwargs: None
        jqdata.FixedSlippage = lambda value: value
        jqdata.log = types.SimpleNamespace(info=lambda *_args, **_kwargs: None)
        jqdata.OrderCost = lambda **kwargs: kwargs
        jqdata.order_target_value = lambda *_args, **_kwargs: None
        jqdata.record = lambda **_kwargs: None
        jqdata.run_daily = lambda *_args, **_kwargs: None
        jqdata.set_benchmark = lambda *_args, **_kwargs: None
        jqdata.set_option = lambda *_args, **_kwargs: None
        jqdata.set_order_cost = lambda *_args, **_kwargs: None
        jqdata.set_slippage = lambda *_args, **_kwargs: None

        previous = sys.modules.get("jqdata")
        sys.modules["jqdata"] = jqdata
        try:
            spec = importlib.util.spec_from_file_location("dual_ma_template", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            if previous is None:
                sys.modules.pop("jqdata", None)
            else:
                sys.modules["jqdata"] = previous


if __name__ == "__main__":
    unittest.main()
