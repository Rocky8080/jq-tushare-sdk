import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock
import sys

import pandas as pd

from jq_tushare_sdk.api import jqdata
from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.globals_state import RuntimeState
from jq_tushare_sdk.runtime.loader import BacktestRuntimeSafety, StrategyLoader
from jq_tushare_sdk.runtime.logger import RuntimeLogger
from jq_tushare_sdk.runtime.scheduler import Scheduler


class BrokerStub:
    def __init__(self):
        self.target_portfolio_signals = []

    def order(self, *_args, **_kwargs):
        return None

    def order_value(self, *_args, **_kwargs):
        return None

    def order_target(self, *_args, **_kwargs):
        return None

    def order_target_value(self, *_args, **_kwargs):
        return None

    def capture_target_portfolio(self, signal):
        self.target_portfolio_signals.append(dict(signal))


class _SliceFailingList(list):
    def __init__(self, owner, values=()):
        super().__init__(values)
        self._owner = owner

    def __setitem__(self, key, value):
        if self._owner.fail_entries_restore and isinstance(key, slice):
            raise RuntimeError("entries slice restore failed")
        return super().__setitem__(key, value)


class SchedulerRestoreFailureStub(Scheduler):
    def __init__(self):
        self.fail_entries_restore = False
        self._entries = _SliceFailingList(self)
        self._callback_wrapper = lambda func: func

    @property
    def entries(self):
        return self._entries

    @entries.setter
    def entries(self, value):
        if self.fail_entries_restore:
            raise RuntimeError("entries setattr restore failed")
        self._entries = _SliceFailingList(self, value)


class PricePortalStub:
    def __init__(self):
        self.end_dates = []

    def get_price(self, security, **kwargs):
        self.end_dates.append(kwargs.get("end_date"))
        return pd.DataFrame(
            {
                "time": ["2024-01-02", "2024-01-03"],
                "code": [security, security],
                "close": [10.0, 11.0],
            }
        )


class TestRuntime(unittest.TestCase):
    def _make_runtime_state(self, tmp_path: Path, name: str, context=None):
        if context is None:
            context = Context(Portfolio(1000000.0))
        return RuntimeState(
            data_portal=SimpleNamespace(),
            scheduler=Scheduler(),
            broker=BrokerStub(),
            context=context,
            log=RuntimeLogger(tmp_path / f"{name}.log"),
        )

    def test_context_defaults_to_backtest_run_params(self):
        context = Context(Portfolio(1000000.0))

        self.assertEqual(context.run_params.type, "backtest")

    def test_scheduler_filters_weekday_and_time(self):
        scheduler = Scheduler()
        calls = []

        def daily(context):
            calls.append(("daily", context.current_dt.weekday()))

        def weekly(context):
            calls.append(("weekly", context.current_dt.weekday()))

        scheduler.run_daily(daily, time="open")
        scheduler.run_weekly(weekly, weekday=2, time="open")
        context = SimpleNamespace(current_dt=datetime(2024, 1, 2, 9, 30))

        callbacks = scheduler.callbacks_for(context.current_dt, "open")
        for callback in callbacks:
            callback(context)

        self.assertEqual(calls, [("daily", 1), ("weekly", 1)])

    def test_scheduler_rejects_unsupported_kwargs(self):
        scheduler = Scheduler()

        def handle(_context):
            return None

        with self.assertRaisesRegex(
            TypeError,
            "Unsupported scheduler kwargs: reference_security",
        ):
            scheduler.run_daily(handle, time="open", reference_security="000001.XSHE")
        with self.assertRaisesRegex(
            TypeError,
            "Unsupported scheduler kwargs: reference_security",
        ):
            scheduler.run_weekly(
                handle,
                weekday=2,
                time="open",
                reference_security="000001.XSHE",
            )
        with self.assertRaisesRegex(
            TypeError,
            "Unsupported scheduler kwargs: reference_security",
        ):
            scheduler.run_monthly(
                handle,
                monthday=3,
                time="open",
                reference_security="000001.XSHE",
            )

        self.assertEqual(scheduler.entries, [])

    def test_loader_executes_strategy_without_source_changes(self):
        source = """
from jqdata import *

def initialize(context):
    g.security = '000001.XSHE'
    run_daily(handle_open, time='open')
    log.info('initialized')

def handle_open(context):
    record(price=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "demo_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            log_path = Path(tmp) / "backtest.log"
            scheduler = Scheduler()
            logger = RuntimeLogger(log_path)
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=scheduler,
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=logger,
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)

            self.assertEqual(state.g.security, "000001.XSHE")
            self.assertEqual(len(scheduler.entries), 1)
            self.assertIn("initialized", log_path.read_text(encoding="utf-8"))

    def test_loader_injects_globals_without_jqdata_import_and_shares_g(self):
        source = """
def initialize(context):
    g.security = '600000.XSHG'
    log.warning('init %s', g.security)
    run_daily(handle_open, time='close')

def handle_open(context):
    record(price=2)
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "implicit_globals_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            log_path = Path(tmp) / "runtime.log"
            scheduler = Scheduler()
            logger = RuntimeLogger(log_path)
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=scheduler,
                broker=BrokerStub(),
                context=Context(Portfolio(500000.0)),
                log=logger,
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)

            self.assertIs(state.g, loaded.module.g)
            self.assertEqual(state.g.security, "600000.XSHG")
            self.assertEqual(
                [entry.time for entry in scheduler.entries],
                ["close"],
            )
            self.assertIn("init 600000.XSHG", log_path.read_text(encoding="utf-8"))

    def test_loader_provides_legacy_pandas_panel_during_strategy_execution(self):
        source = """
import pandas as pd
from jqdata import *

def initialize(context):
    g.panel_available_in_initialize = hasattr(pd, 'Panel')
    run_daily(handle_open, time='open')

def handle_open(context):
    g.panel_check = isinstance(pd.DataFrame({'x': [1]}), pd.Panel)
    record(panel_check=g.panel_check)
"""
        original_panel_present = hasattr(pd, "Panel")
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "legacy_panel_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)
            state.context.current_dt = datetime(2024, 1, 2, 9, 30)
            state.scheduler.callbacks_for(state.context.current_dt, "open")[0](state.context)

            self.assertTrue(state.g.panel_available_in_initialize)
            self.assertFalse(state.g.panel_check)
            self.assertEqual(state.records, [{"panel_check": False}])
            self.assertEqual(hasattr(pd, "Panel"), original_panel_present)

    def test_loader_restores_preexisting_jqdata_and_strategy_module_slots_after_success(self):
        source = """
from jqdata import *

def initialize(context):
    g.security = '000001.XSHE'
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "restored_slots_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            jqdata_sentinel = ModuleType("jqdata")
            strategy_sentinel = ModuleType(strategy_path.stem)
            original_jqdata_present = "jqdata" in sys.modules
            original_jqdata = sys.modules.get("jqdata")
            original_strategy_present = strategy_path.stem in sys.modules
            original_strategy = sys.modules.get(strategy_path.stem)
            sys.modules["jqdata"] = jqdata_sentinel
            sys.modules[strategy_path.stem] = strategy_sentinel
            try:
                loaded = StrategyLoader().load(strategy_path, state)

                self.assertIs(loaded.module.g, state.g)
                self.assertIs(sys.modules["jqdata"], jqdata_sentinel)
                self.assertIs(sys.modules[strategy_path.stem], strategy_sentinel)
            finally:
                if original_jqdata_present:
                    sys.modules["jqdata"] = original_jqdata
                else:
                    sys.modules.pop("jqdata", None)
                if original_strategy_present:
                    sys.modules[strategy_path.stem] = original_strategy
                else:
                    sys.modules.pop(strategy_path.stem, None)

    def test_loader_leaves_jqdata_absent_after_successful_load_when_initially_absent(self):
        source = """
from jqdata import *

def initialize(context):
    g.security = '000001.XSHE'
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "jqdata_absent_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            original_jqdata_present = "jqdata" in sys.modules
            original_jqdata = sys.modules.get("jqdata")
            original_strategy_present = strategy_path.stem in sys.modules
            original_strategy = sys.modules.get(strategy_path.stem)
            sys.modules.pop("jqdata", None)
            sys.modules.pop(strategy_path.stem, None)
            try:
                loaded = StrategyLoader().load(strategy_path, state)

                self.assertIs(loaded.module.g, state.g)
                self.assertNotIn("jqdata", sys.modules)
                self.assertNotIn(strategy_path.stem, sys.modules)
            finally:
                if original_jqdata_present:
                    sys.modules["jqdata"] = original_jqdata
                else:
                    sys.modules.pop("jqdata", None)
                if original_strategy_present:
                    sys.modules[strategy_path.stem] = original_strategy
                else:
                    sys.modules.pop(strategy_path.stem, None)

    def test_loader_failed_strategy_import_restores_module_slots_and_leaves_no_partial_module(self):
        source = """
from jqdata import *

raise RuntimeError('boom during import')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "failing_import_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            jqdata_sentinel = ModuleType("jqdata")
            original_jqdata_present = "jqdata" in sys.modules
            original_jqdata = sys.modules.get("jqdata")
            original_strategy_present = strategy_path.stem in sys.modules
            original_strategy = sys.modules.get(strategy_path.stem)
            sys.modules["jqdata"] = jqdata_sentinel
            sys.modules.pop(strategy_path.stem, None)
            try:
                with self.assertRaisesRegex(RuntimeError, "boom during import"):
                    StrategyLoader().load(strategy_path, state)

                self.assertIs(sys.modules["jqdata"], jqdata_sentinel)
                self.assertNotIn(strategy_path.stem, sys.modules)
            finally:
                if original_jqdata_present:
                    sys.modules["jqdata"] = original_jqdata
                else:
                    sys.modules.pop("jqdata", None)
                if original_strategy_present:
                    sys.modules[strategy_path.stem] = original_strategy
                else:
                    sys.modules.pop(strategy_path.stem, None)

    def test_loader_failed_strategy_import_restores_runtime_state_side_effects(self):
        source = """
from jqdata import *

def handle_open(context):
    g.callback_ran = True

g.existing = 'mutated'
g.position_cost['keep'] = 99
g.position_cost['added'] = {'nested': ['new']}
g.symbols.append('new')
g.flags.add('new')
g.tupled[0]['keep'].append('new')
g.tupled[1].add('new')
g.partial = True
run_daily(handle_open, time='open')
raise RuntimeError('boom during import')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "failing_import_side_effects_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            for run_type in ("backtest", "sim_trade"):
                with self.subTest(run_type=run_type):
                    context = Context(Portfolio(1000000.0))
                    context.run_params.type = run_type
                    state = RuntimeState(
                        data_portal=SimpleNamespace(),
                        scheduler=Scheduler(),
                        broker=BrokerStub(),
                        context=context,
                        log=RuntimeLogger(Path(tmp) / f"{run_type}.log"),
                    )
                    state.g.existing = "keep"
                    state.g.position_cost = {"keep": 1}
                    state.g.symbols = ["old"]
                    state.g.flags = {"old"}
                    state.g.tupled = ({"keep": ["old"]}, {"old"})

                    original_entries = list(state.scheduler.entries)
                    original_wrapper_calls = []

                    def original_wrapper(func):
                        def _wrapped(*args, **kwargs):
                            original_wrapper_calls.append(func.__name__)
                            return func(*args, **kwargs)

                        return _wrapped

                    state.scheduler.set_callback_wrapper(original_wrapper)
                    original_callback_wrapper = state.scheduler._callback_wrapper

                    with self.assertRaisesRegex(RuntimeError, "boom during import"):
                        StrategyLoader().load(strategy_path, state)

                    self.assertEqual(vars(state.g), {
                        "existing": "keep",
                        "position_cost": {"keep": 1},
                        "symbols": ["old"],
                        "flags": {"old"},
                        "tupled": ({"keep": ["old"]}, {"old"}),
                    })
                    self.assertEqual(state.scheduler.entries, original_entries)
                    self.assertIs(state.scheduler._callback_wrapper, original_callback_wrapper)

                    callback_hits = []

                    def after_failure_callback(_context):
                        callback_hits.append("ran")

                    state.scheduler.run_daily(after_failure_callback, time="open")
                    callbacks = state.scheduler.callbacks_for(context.current_dt, "open")
                    self.assertEqual(len(callbacks), 1)
                    callbacks[0](context)
                    self.assertEqual(callback_hits, ["ran"])
                    self.assertEqual(original_wrapper_calls, ["after_failure_callback"])

    def test_loader_failed_strategy_import_restores_records(self):
        source = """
from jqdata import *

record(owner='leaked')
raise RuntimeError('boom during import')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "failing_import_records_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            state.records.append({"owner": "existing"})

            with self.assertRaisesRegex(RuntimeError, "boom during import"):
                StrategyLoader().load(strategy_path, state)

            self.assertEqual(state.records, [{"owner": "existing"}])

    def test_loader_failed_strategy_import_surfaces_scheduler_restore_failures(self):
        source = """
from jqdata import *

def handle_open(context):
    return None

run_daily(handle_open, time='open')
raise RuntimeError('boom during import')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "failing_import_scheduler_restore_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            scheduler = SchedulerRestoreFailureStub()
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=scheduler,
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            original_callback_wrapper = scheduler._callback_wrapper
            scheduler.fail_entries_restore = True

            with self.assertRaises(ExceptionGroup) as raised:
                StrategyLoader().load(strategy_path, state)

            child_messages = [str(exc) for exc in raised.exception.exceptions]
            self.assertIn("boom during import", child_messages)
            self.assertTrue(
                any(
                    "Cannot restore scheduler.entries after failed strategy load" in message
                    for message in child_messages
                )
            )
            self.assertIs(state.scheduler._callback_wrapper, original_callback_wrapper)

    def test_loader_failed_strategy_import_with_undeepcopyable_g_raises_rollback_runtime_error(self):
        source = """
from jqdata import *

def handle_open(context):
    g.callback_ran = True

g.existing = 'mutated'
g.partial = True
run_daily(handle_open, time='open')
raise RuntimeError('boom during import')
"""

        class NonDeepcopyable:
            def __deepcopy__(self, _memo):
                raise TypeError("cannot deepcopy guard")

        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "failing_import_undeepcopyable_g_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            state.g.existing = "keep"
            state.g.guard = NonDeepcopyable()
            original_entries = list(state.scheduler.entries)

            original_wrapper_calls = []

            def original_wrapper(func):
                def _wrapped(*args, **kwargs):
                    original_wrapper_calls.append(func.__name__)
                    return func(*args, **kwargs)

                return _wrapped

            state.scheduler.set_callback_wrapper(original_wrapper)
            original_callback_wrapper = state.scheduler._callback_wrapper

            with self.assertRaises(ExceptionGroup) as raised:
                StrategyLoader().load(strategy_path, state)

            child_messages = [str(exc) for exc in raised.exception.exceptions]
            self.assertIn("boom during import", child_messages)
            g_rollback_error = next(
                exc
                for exc in raised.exception.exceptions
                if "Cannot safely roll back state.g after failed strategy load" in str(exc)
            )
            self.assertIsInstance(g_rollback_error.__cause__, TypeError)
            self.assertEqual(str(g_rollback_error.__cause__), "cannot deepcopy guard")
            self.assertEqual(state.scheduler.entries, original_entries)
            self.assertIs(state.scheduler._callback_wrapper, original_callback_wrapper)

            callback_hits = []

            def after_failure_callback(_context):
                callback_hits.append("ran")

            state.scheduler.run_daily(after_failure_callback, time="open")
            callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")
            self.assertEqual(len(callbacks), 1)
            callbacks[0](state.context)
            self.assertEqual(callback_hits, ["ran"])
            self.assertEqual(original_wrapper_calls, ["after_failure_callback"])

    def test_loader_failed_patch_module_restores_runtime_state_side_effects(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

def handle_open(context):
    g.callback_ran = True

g.existing = 'mutated'
g.position_cost['keep'] = 99
g.position_cost['added'] = {'nested': ['new']}
g.symbols.append('new')
g.flags.add('new')
g.tupled[0]['keep'].append('new')
g.tupled[1].add('new')
run_daily(handle_open, time='open')
g.graph = {'level_0': {'level_1': {'level_2': {'level_3': {'level_4': JQOrderWrapper()}}}}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "failing_patch_module_side_effects_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            state.g.existing = "keep"
            state.g.position_cost = {"keep": 1}
            state.g.symbols = ["old"]
            state.g.flags = {"old"}
            state.g.tupled = ({"keep": ["old"]}, {"old"})

            original_entries = list(state.scheduler.entries)
            original_wrapper_calls = []

            def original_wrapper(func):
                def _wrapped(*args, **kwargs):
                    original_wrapper_calls.append(func.__name__)
                    return func(*args, **kwargs)

                return _wrapped

            state.scheduler.set_callback_wrapper(original_wrapper)
            original_callback_wrapper = state.scheduler._callback_wrapper

            with mock.patch.object(BacktestRuntimeSafety, "_MAX_WALK_OBJECTS", 8):
                with self.assertRaisesRegex(RuntimeError, r"walk budget exceeded"):
                    StrategyLoader().load(strategy_path, state)

            self.assertEqual(vars(state.g), {
                "existing": "keep",
                "position_cost": {"keep": 1},
                "symbols": ["old"],
                "flags": {"old"},
                "tupled": ({"keep": ["old"]}, {"old"}),
            })
            self.assertEqual(state.scheduler.entries, original_entries)
            self.assertIs(state.scheduler._callback_wrapper, original_callback_wrapper)

            callback_hits = []

            def after_failure_callback(_context):
                callback_hits.append("ran")

            state.scheduler.run_daily(after_failure_callback, time="open")
            callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")
            self.assertEqual(len(callbacks), 1)
            callbacks[0](state.context)
            self.assertEqual(callback_hits, ["ran"])
            self.assertEqual(original_wrapper_calls, ["after_failure_callback"])

    def test_loader_scopes_jqdata_module_for_initialize_import_and_restores_absent_slots_in_live_mode(self):
        source = """
from jqdata import *

def initialize(context):
    import jqdata
    import sys

    g.initialize_import_works = jqdata.g is g
    g.initialize_module_visible = __name__ in sys.modules
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "live_initialize_import_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            context = Context(Portfolio(1000000.0))
            context.run_params.type = "sim_trade"
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=context,
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            original_jqdata_present = "jqdata" in sys.modules
            original_jqdata = sys.modules.get("jqdata")
            original_strategy_present = strategy_path.stem in sys.modules
            original_strategy = sys.modules.get(strategy_path.stem)
            sys.modules.pop("jqdata", None)
            sys.modules.pop(strategy_path.stem, None)
            try:
                loaded = StrategyLoader().load(strategy_path, state)

                self.assertNotIn("jqdata", sys.modules)
                self.assertNotIn(strategy_path.stem, sys.modules)

                loaded.initialize(state.context)

                self.assertTrue(state.g.initialize_import_works)
                self.assertTrue(state.g.initialize_module_visible)
                self.assertNotIn("jqdata", sys.modules)
                self.assertNotIn(strategy_path.stem, sys.modules)
            finally:
                if original_jqdata_present:
                    sys.modules["jqdata"] = original_jqdata
                else:
                    sys.modules.pop("jqdata", None)
                if original_strategy_present:
                    sys.modules[strategy_path.stem] = original_strategy
                else:
                    sys.modules.pop(strategy_path.stem, None)

    def test_loader_scopes_jqdata_module_for_callback_import_and_restores_absent_slots_in_live_mode(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    import jqdata
    import sys

    g.callback_import_works = jqdata.g is g
    g.callback_module_visible = __name__ in sys.modules
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "live_callback_import_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 2, 9, 30),
            )
            context.run_params.type = "sim_trade"
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=context,
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            original_jqdata_present = "jqdata" in sys.modules
            original_jqdata = sys.modules.get("jqdata")
            original_strategy_present = strategy_path.stem in sys.modules
            original_strategy = sys.modules.get(strategy_path.stem)
            sys.modules.pop("jqdata", None)
            sys.modules.pop(strategy_path.stem, None)
            try:
                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                self.assertNotIn("jqdata", sys.modules)
                self.assertNotIn(strategy_path.stem, sys.modules)

                callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")
                self.assertEqual(len(callbacks), 1)
                callbacks[0](state.context)

                self.assertTrue(state.g.callback_import_works)
                self.assertTrue(state.g.callback_module_visible)
                self.assertNotIn("jqdata", sys.modules)
                self.assertNotIn(strategy_path.stem, sys.modules)
            finally:
                if original_jqdata_present:
                    sys.modules["jqdata"] = original_jqdata
                else:
                    sys.modules.pop("jqdata", None)
                if original_strategy_present:
                    sys.modules[strategy_path.stem] = original_strategy
                else:
                    sys.modules.pop(strategy_path.stem, None)

    def test_loader_restores_preexisting_module_slots_after_initialize_and_callback_backtest_execution(self):
        source = """
from jqdata import *

def initialize(context):
    import jqdata
    import sys

    g.initialize_import_works = jqdata.g is g
    g.initialize_module_visible = __name__ in sys.modules
    run_daily(handle_open, time='open')

def handle_open(context):
    import jqdata
    import sys

    g.callback_import_works = jqdata.g is g
    g.callback_module_visible = __name__ in sys.modules
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "backtest_callback_import_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(
                    Portfolio(1000000.0),
                    current_dt=datetime(2024, 1, 2, 9, 30),
                ),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )
            jqdata_sentinel = ModuleType("jqdata")
            strategy_sentinel = ModuleType(strategy_path.stem)
            original_jqdata_present = "jqdata" in sys.modules
            original_jqdata = sys.modules.get("jqdata")
            original_strategy_present = strategy_path.stem in sys.modules
            original_strategy = sys.modules.get(strategy_path.stem)
            sys.modules["jqdata"] = jqdata_sentinel
            sys.modules[strategy_path.stem] = strategy_sentinel
            try:
                loaded = StrategyLoader().load(strategy_path, state)

                loaded.initialize(state.context)

                self.assertTrue(state.g.initialize_import_works)
                self.assertTrue(state.g.initialize_module_visible)
                self.assertIs(sys.modules["jqdata"], jqdata_sentinel)
                self.assertIs(sys.modules[strategy_path.stem], strategy_sentinel)

                callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")
                self.assertEqual(len(callbacks), 1)
                callbacks[0](state.context)

                self.assertTrue(state.g.callback_import_works)
                self.assertTrue(state.g.callback_module_visible)
                self.assertIs(sys.modules["jqdata"], jqdata_sentinel)
                self.assertIs(sys.modules[strategy_path.stem], strategy_sentinel)
            finally:
                if original_jqdata_present:
                    sys.modules["jqdata"] = original_jqdata
                else:
                    sys.modules.pop("jqdata", None)
                if original_strategy_present:
                    sys.modules[strategy_path.stem] = original_strategy
                else:
                    sys.modules.pop(strategy_path.stem, None)

    def test_loader_initialize_updates_runtime_context_for_api_boundaries(self):
        source = """
from jqdata import *

def initialize(context):
    frame = attribute_history('000001.XSHE', 2, fields=('close',), df=True)
    g.last_times = frame['time'].tolist()
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "context_sync_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            portal = PricePortalStub()
            passed_context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 3, 14, 30),
            )
            stale_context = Context(Portfolio(1000000.0), current_dt=None)
            state = RuntimeState(
                data_portal=portal,
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=stale_context,
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(passed_context)

            self.assertIs(state.context, passed_context)
            self.assertEqual(portal.end_dates, [passed_context.current_dt])
            self.assertEqual(state.g.last_times, ["2024-01-02", "2024-01-03"])

    def test_loader_live_callback_updates_runtime_context_for_api_boundaries(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    frame = attribute_history('000001.XSHE', 2, fields=('close',), df=True)
    g.last_times = frame['time'].tolist()
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "live_callback_context_sync_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            portal = PricePortalStub()
            initial_context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 2, 9, 30),
            )
            initial_context.run_params.type = "sim_trade"
            stale_context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 2, 9, 31),
            )
            stale_context.run_params.type = "sim_trade"
            callback_context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 3, 14, 30),
            )
            callback_context.run_params.type = "sim_trade"
            state = RuntimeState(
                data_portal=portal,
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=initial_context,
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(initial_context)
            state.context = stale_context

            callbacks = state.scheduler.callbacks_for(callback_context.current_dt, "open")
            self.assertEqual(len(callbacks), 1)
            callbacks[0](callback_context)

            self.assertIs(state.context, callback_context)
            self.assertEqual(portal.end_dates, [callback_context.current_dt])
            self.assertEqual(state.g.last_times, ["2024-01-02", "2024-01-03"])

    def test_loader_rejects_initialize_mode_change_from_live_load_to_backtest_context(self):
        source = """
from jqdata import *

def initialize(context):
    g.initialized = True
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "mode_change_initialize_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            load_context = Context(Portfolio(1000000.0))
            load_context.run_params.type = "sim_trade"
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=load_context,
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            self.assertIs(state.context, load_context)

            with self.assertRaisesRegex(
                RuntimeError,
                r"loaded under run mode 'sim_trade'.*cannot initialize under run mode 'backtest'",
            ):
                loaded.initialize(Context(Portfolio(1000000.0)))

            self.assertIs(state.context, load_context)
            self.assertFalse(hasattr(state.g, "initialized"))

    def test_loader_rejects_scheduled_callback_mode_change_after_live_load(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    g.callback_ran = True
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "mode_change_callback_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            load_context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 2, 9, 30),
            )
            load_context.run_params.type = "sim_trade"
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=load_context,
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(load_context)

            callbacks = state.scheduler.callbacks_for(load_context.current_dt, "open")
            self.assertEqual(len(callbacks), 1)

            with self.assertRaisesRegex(
                RuntimeError,
                r"loaded under run mode 'sim_trade'.*cannot execute scheduled callback under run mode 'backtest'",
            ):
                callbacks[0](Context(Portfolio(1000000.0), current_dt=load_context.current_dt))

            self.assertFalse(hasattr(state.g, "callback_ran"))

    def test_runtime_logger_writes_traceback_for_exc_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "runtime.log"
            logger = RuntimeLogger(log_path)

            try:
                raise ValueError("boom")
            except ValueError:
                logger.error("failed run", exc_info=True)

            text = log_path.read_text(encoding="utf-8")
            self.assertIn("ERROR failed run", text)
            self.assertIn("Traceback", text)
            self.assertIn("ValueError: boom", text)

    def test_loader_enforces_backtest_redis_safety_for_imports_and_wrappers(self):
        source = """
import redis
from jqdata import *

module_client = redis.Redis(host='live-host', port=6379)

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)
        self.redis_client_kind = type(self.redis_client).__name__

def init_jq_wrapper(strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
    wrapper = JQOrderWrapper(strategy_id, redis_config, send_redis_signal, context)
    g.wrapper_instance = wrapper
    return wrapper

def initialize(context):
    g.wrapper_instance = init_jq_wrapper(
        strategy_id='demo',
        send_redis_signal=True,
        context=context,
    )
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "safe_loader_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                wrapper = state.g.wrapper_instance
                self.assertFalse(wrapper.send_redis_signal_enabled)
                self.assertEqual(type(loaded.module.module_client).__name__, "InertRedisClient")
                self.assertIsNone(wrapper.redis_client)
                self.assertIsNot(loaded.module.redis, sentinel_redis)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_keeps_initialize_and_callback_redis_imports_inert_in_backtest(self):
        source = """
from jqdata import *

def initialize(context):
    import redis
    g.initialize_redis = redis
    g.initialize_client = redis.Redis(host='init-host', port=6379)
    run_daily(handle_open, time='open')

def handle_open(context):
    import redis
    g.callback_redis = redis
    g.callback_client = redis.Redis(host='callback-host', port=6380)
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "callback_redis_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                self.assertIsNot(state.g.initialize_redis, sentinel_redis)
                self.assertEqual(type(state.g.initialize_client).__name__, "InertRedisClient")
                self.assertIs(sys.modules.get("redis"), sentinel_redis)

                callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")
                self.assertEqual(len(callbacks), 1)
                callbacks[0](state.context)

                self.assertIsNot(state.g.callback_redis, sentinel_redis)
                self.assertEqual(type(state.g.callback_client).__name__, "InertRedisClient")
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_sanitizes_import_time_and_alias_wrappers_in_backtest(self):
        source = """
from jqdata import *
import redis

constructor_observations = []

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        constructor_observations.append(send_redis_signal)
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

CapturedWrapper = JQOrderWrapper
import_time_wrapper = JQOrderWrapper(strategy_id='import-time', send_redis_signal=True)

def make_alias_wrapper():
    return CapturedWrapper(strategy_id='alias', send_redis_signal=True)

def initialize(context):
    g.constructor_observations = list(constructor_observations)
    g.import_time_wrapper = import_time_wrapper
    g.alias_wrapper = make_alias_wrapper()
    g.constructor_observations = list(constructor_observations)
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "import_time_wrapper_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                self.assertEqual(loaded.module.constructor_observations, [False, False])
                self.assertEqual(state.g.constructor_observations, [False, False])
                self.assertFalse(loaded.module.import_time_wrapper.send_redis_signal_enabled)
                self.assertIsNone(loaded.module.import_time_wrapper.redis_client)
                self.assertFalse(state.g.import_time_wrapper.send_redis_signal_enabled)
                self.assertIsNone(state.g.import_time_wrapper.redis_client)
                self.assertFalse(state.g.alias_wrapper.send_redis_signal_enabled)
                self.assertIsNone(state.g.alias_wrapper.redis_client)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_patches_initialize_local_wrapper_before_constructor_runs(self):
        source = """
from jqdata import *
import redis

def initialize(context):
    g.initialize_constructor_observations = []

    class JQOrderWrapper:
        def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
            g.initialize_constructor_observations.append(send_redis_signal)
            self.strategy_id = strategy_id
            self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
            self.redis_client = redis.Redis(host='wrapper-host', port=6380)

    g.initialize_wrapper = JQOrderWrapper(strategy_id='initialize-local', send_redis_signal=True)
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "initialize_local_wrapper_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                self.assertEqual(state.g.initialize_constructor_observations, [False])
                self.assertFalse(state.g.initialize_wrapper.send_redis_signal_enabled)
                self.assertIsNone(state.g.initialize_wrapper.redis_client)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_patches_callback_local_wrapper_before_constructor_runs(self):
        source = """
from jqdata import *
import redis

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    g.callback_constructor_observations = []

    class JQOrderWrapper:
        def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
            g.callback_constructor_observations.append(send_redis_signal)
            self.strategy_id = strategy_id
            self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
            self.redis_client = redis.Redis(host='wrapper-host', port=6380)

    g.callback_wrapper = JQOrderWrapper(strategy_id='callback-local', send_redis_signal=True)
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "callback_local_wrapper_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(
                        Portfolio(1000000.0),
                        current_dt=datetime(2024, 1, 2, 9, 30),
                    ),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)
                callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")

                self.assertEqual(len(callbacks), 1)
                callbacks[0](state.context)

                self.assertEqual(state.g.callback_constructor_observations, [False])
                self.assertFalse(state.g.callback_wrapper.send_redis_signal_enabled)
                self.assertIsNone(state.g.callback_wrapper.redis_client)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_patches_alias_only_wrapper_classes_captured_in_defaults_and_closures(self):
        source = """
from jqdata import *
import redis

def build_wrapper_alias():
    class JQOrderWrapper:
        def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
            g.constructor_observations = getattr(g, 'constructor_observations', [])
            g.constructor_observations.append(
                {
                    'strategy_id': strategy_id,
                    'send_redis_signal': send_redis_signal,
                    'redis_client_kind': type(redis.Redis(host='wrapper-host', port=6380)).__name__,
                }
            )
            self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
            self.redis_client = redis.Redis(host='wrapper-host', port=6380)

    return JQOrderWrapper

WrapperAlias = build_wrapper_alias()
WrapperContainer = {'alias': WrapperAlias}

def make_factory(wrapper_cls=WrapperContainer['alias']):
    def _factory():
        return wrapper_cls(strategy_id='factory-default', send_redis_signal=True)

    return _factory

factory = make_factory()

def initialize(context):
    factory()
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "alias_capture_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                self.assertEqual(
                    state.g.constructor_observations,
                    [
                        {
                            "strategy_id": "factory-default",
                            "send_redis_signal": False,
                            "redis_client_kind": "InertRedisClient",
                        }
                    ],
                )
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_recursively_sanitizes_nested_import_time_wrappers_in_backtest(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

class WrapperHolder:
    pass

holder = WrapperHolder()
holder.wrapper = JQOrderWrapper(strategy_id='object-wrapper', send_redis_signal=True)

runtime_payload = {
    'wrappers': [JQOrderWrapper(strategy_id='list-wrapper', send_redis_signal=True)],
    'mapping': {'wrapped': JQOrderWrapper(strategy_id='dict-wrapper', send_redis_signal=True)},
    'holder': holder,
}

def initialize(context):
    g.runtime_payload = runtime_payload
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "nested_wrappers_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                list_wrapper = loaded.module.runtime_payload["wrappers"][0]
                dict_wrapper = loaded.module.runtime_payload["mapping"]["wrapped"]
                object_wrapper = loaded.module.runtime_payload["holder"].wrapper

                for wrapper in (list_wrapper, dict_wrapper, object_wrapper):
                    self.assertFalse(wrapper.send_redis_signal_enabled)
                    self.assertIsNone(wrapper.redis_client)
                self.assertIs(state.g.runtime_payload, loaded.module.runtime_payload)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_sanitizes_wrappers_inside_external_holder_objects(self):
        source = """
from jqdata import *
from helper_holder import ExternalHolder
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

holder = ExternalHolder()
holder.wrapper = JQOrderWrapper(strategy_id='external-holder', send_redis_signal=True)

def initialize(context):
    g.external_holder = holder
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                strategy_path = tmp_path / "external_holder_strategy.py"
                helper_path = tmp_path / "helper_holder.py"
                helper_path.write_text(
                    "class ExternalHolder:\n    pass\n",
                    encoding="utf-8",
                )
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(tmp_path / "runtime.log"),
                )

                original_sys_path = list(sys.path)
                try:
                    loaded = StrategyLoader().load(strategy_path, state)
                    loaded.initialize(state.context)
                finally:
                    sys.modules.pop("helper_holder", None)

                wrapper = loaded.module.holder.wrapper
                self.assertFalse(wrapper.send_redis_signal_enabled)
                self.assertIsNone(wrapper.redis_client)
                self.assertIs(state.g.external_holder, loaded.module.holder)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
                self.assertEqual(sys.path, original_sys_path)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_scopes_strategy_dir_for_initialize_sibling_imports(self):
        source = """
from jqdata import *

def initialize(context):
    from init_helper import read_value

    g.loaded_value = read_value()
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            strategy_path = tmp_path / "initialize_helper_strategy.py"
            helper_path = tmp_path / "init_helper.py"
            helper_path.write_text(
                "def read_value():\n    return 'loaded-during-initialize'\n",
                encoding="utf-8",
            )
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(tmp_path / "runtime.log"),
            )

            original_sys_path = list(sys.path)
            try:
                loaded = StrategyLoader().load(strategy_path, state)
                self.assertEqual(sys.path, original_sys_path)

                loaded.initialize(state.context)

                self.assertEqual(state.g.loaded_value, "loaded-during-initialize")
                self.assertEqual(sys.path, original_sys_path)
            finally:
                sys.modules.pop("init_helper", None)

    def test_loader_prioritizes_strategy_dir_over_existing_later_sys_path_entry(self):
        source = """
from jqdata import *

def initialize(context):
    from same_name import read_value

    g.loaded_value = read_value()
"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as wrong_tmp:
            tmp_path = Path(tmp)
            wrong_path = Path(wrong_tmp)
            strategy_path = tmp_path / "same_name_strategy.py"
            (tmp_path / "same_name.py").write_text(
                "def read_value():\n    return 'strategy-local'\n",
                encoding="utf-8",
            )
            (wrong_path / "same_name.py").write_text(
                "def read_value():\n    return 'wrong-earlier-path'\n",
                encoding="utf-8",
            )
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(tmp_path / "runtime.log"),
            )

            original_sys_path = list(sys.path)
            try:
                sys.path.insert(0, str(wrong_path))
                sys.path.append(str(tmp_path))
                expected_sys_path = list(sys.path)

                loaded = StrategyLoader().load(strategy_path, state)
                self.assertEqual(sys.path, expected_sys_path)

                loaded.initialize(state.context)

                self.assertEqual(state.g.loaded_value, "strategy-local")
                self.assertEqual(sys.path, expected_sys_path)
            finally:
                sys.path[:] = original_sys_path
                sys.modules.pop("same_name", None)

    def test_loader_scopes_strategy_dir_for_scheduled_callback_sibling_imports(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    from callback_helper import describe_call

    g.callback_value = describe_call()
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            strategy_path = tmp_path / "callback_helper_strategy.py"
            helper_path = tmp_path / "callback_helper.py"
            helper_path.write_text(
                "def describe_call():\n    return 'loaded-during-callback'\n",
                encoding="utf-8",
            )
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(tmp_path / "runtime.log"),
            )

            original_sys_path = list(sys.path)
            try:
                loaded = StrategyLoader().load(strategy_path, state)
                self.assertEqual(sys.path, original_sys_path)

                loaded.initialize(state.context)
                self.assertEqual(sys.path, original_sys_path)

                callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")
                self.assertEqual(len(callbacks), 1)
                callbacks[0](state.context)

                self.assertEqual(state.g.callback_value, "loaded-during-callback")
                self.assertEqual(sys.path, original_sys_path)
            finally:
                sys.modules.pop("callback_helper", None)

    def test_loader_isolates_same_named_sibling_helpers_across_strategies(self):
        source = """
from shared_helper import HELPER_VALUE
from jqdata import *

IMPORTED_VALUE = HELPER_VALUE

def initialize(context):
    g.module_value = IMPORTED_VALUE
    run_daily(handle_open, time='open')

def handle_open(context):
    from shared_helper import HELPER_VALUE

    g.callback_value = HELPER_VALUE
"""
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_path = Path(first_tmp)
            second_path = Path(second_tmp)
            first_strategy = first_path / "first_strategy.py"
            second_strategy = second_path / "second_strategy.py"
            first_strategy.write_text(source, encoding="utf-8")
            second_strategy.write_text(source, encoding="utf-8")
            (first_path / "shared_helper.py").write_text(
                "HELPER_VALUE = 'first-helper'\n",
                encoding="utf-8",
            )
            (second_path / "shared_helper.py").write_text(
                "HELPER_VALUE = 'second-helper'\n",
                encoding="utf-8",
            )

            first_state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(first_path / "first.log"),
            )
            second_state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(second_path / "second.log"),
            )

            try:
                first_loaded = StrategyLoader().load(first_strategy, first_state)
                first_loaded.initialize(first_state.context)
                first_callbacks = first_state.scheduler.callbacks_for(first_state.context.current_dt, "open")
                self.assertEqual(len(first_callbacks), 1)
                first_callbacks[0](first_state.context)

                self.assertEqual(first_state.g.module_value, "first-helper")
                self.assertEqual(first_state.g.callback_value, "first-helper")
                self.assertNotIn("shared_helper", sys.modules)

                second_loaded = StrategyLoader().load(second_strategy, second_state)
                second_loaded.initialize(second_state.context)
                second_callbacks = second_state.scheduler.callbacks_for(
                    second_state.context.current_dt, "open"
                )
                self.assertEqual(len(second_callbacks), 1)
                second_callbacks[0](second_state.context)

                self.assertEqual(second_state.g.module_value, "second-helper")
                self.assertEqual(second_state.g.callback_value, "second-helper")
                self.assertNotIn("shared_helper", sys.modules)
            finally:
                sys.modules.pop("shared_helper", None)

    def test_loader_scopes_runtime_state_during_module_execution_and_restores_previous_state(self):
        first_source = """
from jqdata import *

record(owner='first-load')
"""
        second_source = """
from jqdata import *

record(owner='second-load')
"""
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_path = Path(first_tmp)
            second_path = Path(second_tmp)
            first_strategy = first_path / "first_load_strategy.py"
            second_strategy = second_path / "second_load_strategy.py"
            first_strategy.write_text(first_source, encoding="utf-8")
            second_strategy.write_text(second_source, encoding="utf-8")
            first_state = self._make_runtime_state(first_path, "first")
            second_state = self._make_runtime_state(second_path, "second")
            sentinel_state = self._make_runtime_state(first_path, "sentinel")

            original_state = jqdata._STATE
            try:
                jqdata.set_runtime_state(sentinel_state)

                StrategyLoader().load(first_strategy, first_state)
                self.assertEqual(first_state.records, [{"owner": "first-load"}])
                self.assertEqual(second_state.records, [])
                self.assertIs(jqdata.runtime_state(), sentinel_state)

                StrategyLoader().load(second_strategy, second_state)
                self.assertEqual(second_state.records, [{"owner": "second-load"}])
                self.assertEqual(first_state.records, [{"owner": "first-load"}])
                self.assertIs(jqdata.runtime_state(), sentinel_state)
            finally:
                jqdata._STATE = original_state

    def test_loader_restores_runtime_state_to_uninitialized_after_module_execution(self):
        source = """
from jqdata import *

record(owner='loaded')
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            strategy_path = tmp_path / "uninitialized_load_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = self._make_runtime_state(tmp_path, "runtime")

            original_state = jqdata._STATE
            try:
                jqdata._STATE = None

                StrategyLoader().load(strategy_path, state)

                self.assertEqual(state.records, [{"owner": "loaded"}])
                with self.assertRaisesRegex(RuntimeError, "not initialized"):
                    jqdata.runtime_state()
            finally:
                jqdata._STATE = original_state

    def test_loader_initialize_scopes_runtime_state_to_loaded_strategy_and_restores_previous_state(self):
        source = """
from jqdata import *

def initialize(context):
    record(owner='initialize-first')
"""
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_path = Path(first_tmp)
            second_path = Path(second_tmp)
            first_strategy = first_path / "first_initialize_strategy.py"
            second_strategy = second_path / "second_initialize_strategy.py"
            first_strategy.write_text(source, encoding="utf-8")
            second_strategy.write_text(source.replace("initialize-first", "initialize-second"), encoding="utf-8")
            first_state = self._make_runtime_state(first_path, "first")
            second_state = self._make_runtime_state(second_path, "second")

            original_state = jqdata._STATE
            try:
                first_loaded = StrategyLoader().load(first_strategy, first_state)
                second_loaded = StrategyLoader().load(second_strategy, second_state)
                jqdata.set_runtime_state(second_state)

                self.assertIs(jqdata.runtime_state(), second_state)

                first_loaded.initialize(first_state.context)

                self.assertEqual(first_state.records, [{"owner": "initialize-first"}])
                self.assertEqual(second_state.records, [])
                self.assertIs(jqdata.runtime_state(), second_state)

                second_loaded.initialize(second_state.context)

                self.assertEqual(second_state.records, [{"owner": "initialize-second"}])
                self.assertIs(jqdata.runtime_state(), second_state)
            finally:
                jqdata._STATE = original_state

    def test_loader_initialize_restores_runtime_state_to_uninitialized(self):
        source = """
from jqdata import *

def initialize(context):
    record(owner='initialize')
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            strategy_path = tmp_path / "uninitialized_initialize_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = self._make_runtime_state(tmp_path, "runtime")
            loaded = StrategyLoader().load(strategy_path, state)

            original_state = jqdata._STATE
            try:
                jqdata._STATE = None

                loaded.initialize(state.context)

                self.assertEqual(state.records, [{"owner": "initialize"}])
                with self.assertRaisesRegex(RuntimeError, "not initialized"):
                    jqdata.runtime_state()
            finally:
                jqdata._STATE = original_state

    def test_loader_callback_scopes_runtime_state_to_loaded_strategy_and_restores_previous_state(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    record(owner='callback-first')
"""
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_path = Path(first_tmp)
            second_path = Path(second_tmp)
            first_strategy = first_path / "first_callback_strategy.py"
            second_strategy = second_path / "second_callback_strategy.py"
            first_strategy.write_text(source, encoding="utf-8")
            second_strategy.write_text(source.replace("callback-first", "callback-second"), encoding="utf-8")
            run_context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 2, 9, 30),
            )
            first_state = self._make_runtime_state(first_path, "first", context=run_context)
            second_state = self._make_runtime_state(
                second_path,
                "second",
                context=Context(
                    Portfolio(1000000.0),
                    current_dt=datetime(2024, 1, 2, 9, 30),
                ),
            )

            original_state = jqdata._STATE
            try:
                first_loaded = StrategyLoader().load(first_strategy, first_state)
                first_loaded.initialize(first_state.context)
                first_callbacks = first_state.scheduler.callbacks_for(first_state.context.current_dt, "open")

                second_loaded = StrategyLoader().load(second_strategy, second_state)
                second_loaded.initialize(second_state.context)
                jqdata.set_runtime_state(second_state)
                self.assertIs(jqdata.runtime_state(), second_state)

                self.assertEqual(len(first_callbacks), 1)
                first_callbacks[0](first_state.context)

                self.assertEqual(first_state.records, [{"owner": "callback-first"}])
                self.assertEqual(second_state.records, [])
                self.assertIs(jqdata.runtime_state(), second_state)
            finally:
                jqdata._STATE = original_state

    def test_loader_callback_restores_runtime_state_to_uninitialized(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    record(owner='callback')
"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            strategy_path = tmp_path / "uninitialized_callback_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            context = Context(
                Portfolio(1000000.0),
                current_dt=datetime(2024, 1, 2, 9, 30),
            )
            state = self._make_runtime_state(tmp_path, "runtime", context=context)
            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)
            callbacks = state.scheduler.callbacks_for(state.context.current_dt, "open")

            original_state = jqdata._STATE
            try:
                jqdata._STATE = None

                self.assertEqual(len(callbacks), 1)
                callbacks[0](state.context)

                self.assertEqual(state.records, [{"owner": "callback"}])
                with self.assertRaisesRegex(RuntimeError, "not initialized"):
                    jqdata.runtime_state()
            finally:
                jqdata._STATE = original_state

    def test_loader_fails_loud_when_runtime_walk_budget_is_exceeded(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

def initialize(context):
    g.graph = {'level_0': {'level_1': {'level_2': {'level_3': JQOrderWrapper()}}}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "budget_exhaustion_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)

            with mock.patch.object(BacktestRuntimeSafety, "_MAX_WALK_OBJECTS", 4):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"walk budget exceeded.*strategy budget_exhaustion_strategy",
                ):
                    loaded.initialize(state.context)

    def test_loader_binds_send_flag_by_constructor_parameter_name(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', send_redis_signal=True, redis_config=None, context=None):
        g.received_args = {
            'strategy_id': strategy_id,
            'send_redis_signal': send_redis_signal,
            'redis_config': redis_config,
        }
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_config = redis_config
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

def initialize(context):
    g.wrapper_instance = JQOrderWrapper('demo', True, 'cfg-token', context)
"""
        sentinel_redis = ModuleType("redis")
        original_redis = sys.modules.get("redis")
        sys.modules["redis"] = sentinel_redis
        try:
            with tempfile.TemporaryDirectory() as tmp:
                strategy_path = Path(tmp) / "signature_drift_strategy.py"
                strategy_path.write_text(source, encoding="utf-8")
                state = RuntimeState(
                    data_portal=SimpleNamespace(),
                    scheduler=Scheduler(),
                    broker=BrokerStub(),
                    context=Context(Portfolio(1000000.0)),
                    log=RuntimeLogger(Path(tmp) / "runtime.log"),
                )

                loaded = StrategyLoader().load(strategy_path, state)
                loaded.initialize(state.context)

                self.assertEqual(
                    state.g.received_args,
                    {
                        "strategy_id": "demo",
                        "send_redis_signal": False,
                        "redis_config": "cfg-token",
                    },
                )
                self.assertFalse(state.g.wrapper_instance.send_redis_signal_enabled)
                self.assertEqual(state.g.wrapper_instance.redis_config, "cfg-token")
                self.assertIsNone(state.g.wrapper_instance.redis_client)
                self.assertIs(sys.modules.get("redis"), sentinel_redis)
        finally:
            if original_redis is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis

    def test_loader_generated_wrapper_strategy_before_open_stays_backtest_safe(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

def before_open(context):
    g.wrapper_instance = JQOrderWrapper(send_redis_signal=True)

def initialize(context):
    run_daily(before_open, time='before_open')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "generated_wrapper_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(
                    Portfolio(1000000.0),
                    current_dt=datetime(2024, 1, 2, 9, 0),
                ),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)

            callbacks = state.scheduler.callbacks_for(state.context.current_dt, "before_open")
            self.assertEqual(len(callbacks), 1)
            callbacks[0](state.context)

            wrapper = state.g.wrapper_instance
            self.assertFalse(wrapper.send_redis_signal_enabled)
            self.assertIsNone(wrapper.redis_client)
            self.assertEqual(type(loaded.module.redis.Redis()).__name__, "InertRedisClient")

    def test_loader_generated_wrapper_strategy_captures_target_portfolio_signal(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

    def send_target_portfolio_map(self, target_positions, source='unit-test', execution_mode='confirmed'):
        positions = [
            {'stock_code': stock_code, 'volume': volume}
            for stock_code, volume in target_positions.items()
        ]
        return self.send_redis_signal({
            'type': 'target_portfolio',
            'positions': positions,
            'source': source,
            'execution_mode': execution_mode,
        })

    def send_redis_signal(self, signal):
        return True

def before_open(context):
    g.wrapper_instance = JQOrderWrapper(send_redis_signal=True)

def initialize(context):
    run_daily(before_open, time='before_open')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "generated_target_capture_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(
                    Portfolio(1000000.0),
                    current_dt=datetime(2024, 1, 2, 9, 30),
                ),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)
            callbacks = state.scheduler.callbacks_for(state.context.current_dt, "before_open")
            self.assertEqual(len(callbacks), 1)
            callbacks[0](state.context)
            wrapper = state.g.wrapper_instance

            sent = wrapper.send_target_portfolio_map(
                {"000001.XSHE": 1000},
                source="unit-test",
                execution_mode="confirmed",
            )

            self.assertTrue(sent)
            self.assertFalse(wrapper.send_redis_signal_enabled)
            self.assertIsNone(wrapper.redis_client)
            self.assertEqual(len(state.broker.target_portfolio_signals), 1)
            self.assertEqual(
                state.broker.target_portfolio_signals[0]["positions"],
                [{"stock_code": "000001.XSHE", "volume": 1000}],
            )

    def test_loader_captures_module_level_target_portfolio_signal_in_backtest(self):
        source = """
from jqdata import *

def send_ptrade_redis_signal(context, signal):
    if not signal:
        return False
    return False

def initialize(context):
    g.sent = send_ptrade_redis_signal(
        context,
        {
            'type': 'target_portfolio',
            'positions': [{'stock_code': '000001.XSHE', 'volume': 500}],
            'source': 'module-test',
        },
    )
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "module_level_target_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            loaded.initialize(state.context)

            self.assertTrue(state.g.sent)
            self.assertEqual(len(state.broker.target_portfolio_signals), 1)
            self.assertEqual(
                state.broker.target_portfolio_signals[0]["positions"],
                [{"stock_code": "000001.XSHE", "volume": 500}],
            )

    def test_loader_does_not_scan_sdk_target_capture_closure_caches(self):
        source = """
from jqdata import *

def send_ptrade_redis_signal(context, signal):
    return False

def initialize(context):
    g.sent = send_ptrade_redis_signal(
        context,
        {
            'type': 'target_portfolio',
            'positions': [{'stock_code': '000001.XSHE', 'volume': 500}],
            'source': 'module-test',
        },
    )
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "target_capture_cache_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            cached_graph = {}
            current = cached_graph
            for index in range(700):
                child = {}
                current[f"level_{index}"] = child
                current = child
            state = RuntimeState(
                data_portal=SimpleNamespace(_fetch_cache={}),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)
            state.data_portal._fetch_cache["cached"] = cached_graph

            with mock.patch.object(BacktestRuntimeSafety, "_MAX_WALK_OBJECTS", 512):
                loaded.initialize(state.context)

            self.assertTrue(state.g.sent)
            self.assertEqual(len(state.broker.target_portfolio_signals), 1)

    def test_loader_sanitizes_wrappers_when_initialize_raises(self):
        source = """
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, strategy_id='default', redis_config=None, send_redis_signal=True, context=None):
        self.strategy_id = strategy_id
        self.send_redis_signal_enabled = send_redis_signal if send_redis_signal is not None else True
        self.redis_client = redis.Redis(host='wrapper-host', port=6380)

def initialize(context):
    g.wrapper_instance = JQOrderWrapper(strategy_id='boom', send_redis_signal=True)
    raise ValueError('boom')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "initialize_exception_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=RuntimeLogger(Path(tmp) / "runtime.log"),
            )

            loaded = StrategyLoader().load(strategy_path, state)

            with self.assertRaisesRegex(ValueError, "boom"):
                loaded.initialize(state.context)

            wrapper = state.g.wrapper_instance
            self.assertFalse(wrapper.send_redis_signal_enabled)
            self.assertIsNone(wrapper.redis_client)

    def test_backtest_runtime_safety_logs_suppressed_sanitize_failure_and_preserves_strategy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeState(
                data_portal=SimpleNamespace(),
                scheduler=Scheduler(),
                broker=BrokerStub(),
                context=Context(Portfolio(1000000.0)),
                log=mock.Mock(),
            )
            safety = BacktestRuntimeSafety(state, Path(tmp) / "suppressed_sanitize_strategy.py")

            def _boom(_context):
                raise ValueError("strategy boom")

            with mock.patch.object(
                safety,
                "sanitize_runtime_objects",
                side_effect=RuntimeError("sanitize boom"),
            ):
                with self.assertRaisesRegex(ValueError, "strategy boom"):
                    safety.execute(_boom, state.context)

            state.log.error.assert_called_once()
            args, kwargs = state.log.error.call_args
            self.assertIn("Suppressed runtime safety cleanup error", args[0])
            self.assertTrue(kwargs["exc_info"])


if __name__ == "__main__":
    unittest.main()
