"""Large stock universes remain data while Redis isolation stays enforced."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.globals_state import RuntimeState
from jq_tushare_sdk.runtime.loader import BacktestRuntimeSafety, InertRedisClient, StrategyLoader
from jq_tushare_sdk.runtime.logger import RuntimeLogger
from jq_tushare_sdk.runtime.scheduler import Scheduler


class RuntimeSafetyLargeUniverseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.state = RuntimeState(
            data_portal=SimpleNamespace(), scheduler=Scheduler(), broker=SimpleNamespace(),
            context=Context(Portfolio(1_000_000.), current_dt=datetime(2026, 7, 1, 9, 30)),
            log=RuntimeLogger(self.path / 'runtime.log'))
        self.safety = BacktestRuntimeSafety(self.state, self.path / 'strategy.py')

    def tearDown(self):
        self.tmp.cleanup()

    def stocks(self, count=6000):
        return [f'{number:06d}.XSHE' for number in range(count)]

    def unclearable_wrapper(self):
        class JQOrderWrapper:
            send_redis_signal_enabled = False

            @property
            def redis_client(self):
                return SimpleNamespace(close=lambda: None)

            @redis_client.setter
            def redis_client(self, value):
                raise RuntimeError('client is read-only')
        return JQOrderWrapper()

    def test_large_stock_universe_and_scalar_maps_pass_without_raising_object_limit(self):
        codes = self.stocks()
        self.state.g.sector_reference_universe = tuple(codes)
        self.state.g.industry_name_by_code = {code: f'industry-{index % 31}' for index, code in enumerate(codes)}
        self.state.g.sector_score_by_code = {code: index / len(codes) for index, code in enumerate(codes)}
        self.state.g.prices = {code: float(index + 1) for index, code in enumerate(codes)}
        self.state.g.atomic_values = [None, 1, 2.0, True, b'bytes', 3j]
        self.safety.sanitize_runtime_objects()
        self.assertEqual(len(self.state.g.sector_reference_universe), 6000)
        self.assertEqual(self.safety._MAX_WALK_OBJECTS, 2048)
        self.assertEqual(self.state.g.sector_score_by_code[codes[-1]], 5999 / 6000)

    def test_wrapper_at_tail_of_long_list_is_still_rejected(self):
        self.state.g.universe = self.stocks() + [self.unclearable_wrapper()]
        with self.assertRaisesRegex(RuntimeError, r'cannot clear redis_client'):
            self.safety.sanitize_runtime_objects()

    def test_exact_string_subclass_keeps_hidden_attributes_visible(self):
        class Symbol(str):
            pass
        symbol = Symbol('000001.XSHE')
        symbol.wrapper = self.unclearable_wrapper()
        self.state.g.universe = self.stocks() + [symbol]
        with self.assertRaisesRegex(RuntimeError, r'cannot clear redis_client'):
            self.safety.sanitize_runtime_objects()

    def test_non_atomic_dictionary_key_keeps_hidden_wrapper_visible(self):
        class Symbol(str):
            pass
        symbol = Symbol('000001.XSHE')
        symbol.wrapper = self.unclearable_wrapper()
        self.state.g.mapping = {symbol: 1.0}
        with self.assertRaisesRegex(RuntimeError, r'cannot clear redis_client'):
            self.safety.sanitize_runtime_objects()

    def test_container_subclass_attributes_are_not_skipped_after_data_items(self):
        class Universe(list):
            pass
        universe = Universe(self.stocks())
        universe.wrapper = self.unclearable_wrapper()
        self.state.g.universe = universe
        with self.assertRaisesRegex(RuntimeError, r'cannot clear redis_client'):
            self.safety.sanitize_runtime_objects()

    def test_large_number_of_distinct_containers_still_exceeds_object_limit(self):
        graph = [{} for _ in range(self.safety._MAX_WALK_OBJECTS)]
        with self.assertRaisesRegex(RuntimeError, r'walk budget exceeded.*2048 objects'):
            self.safety._walk_runtime_object(graph, 'containers')

    def test_pure_atomic_input_still_has_finite_total_scan_budget(self):
        graph = ['000001.XSHE'] * self.safety._MAX_WALK_EDGES
        with self.assertRaisesRegex(RuntimeError, r'walk budget exceeded.*references limit'):
            self.safety._walk_runtime_object(graph, 'flat input')

    def test_repeated_container_identity_still_consumes_reference_scan_budget(self):
        graph = [{}] * self.safety._MAX_WALK_EDGES
        with self.assertRaisesRegex(RuntimeError, r'walk budget exceeded.*references limit'):
            self.safety._walk_runtime_object(graph, 'aliases')

    def test_deep_container_graph_fails_with_explicit_depth_budget(self):
        graph = []
        current = graph
        for _ in range(self.safety._MAX_WALK_DEPTH + 1):
            child = []
            current.append(child)
            current = child
        with self.assertRaisesRegex(RuntimeError, r'walk budget exceeded.*depth limit'):
            self.safety._walk_runtime_object(graph, 'deep input')

    def test_cycles_remain_bounded_and_do_not_hide_wrapper(self):
        graph = []
        graph.append(graph)
        self.safety._walk_runtime_object(graph, 'cycle')
        graph.append(self.unclearable_wrapper())
        with self.assertRaisesRegex(RuntimeError, r'cannot clear redis_client'):
            self.safety._walk_runtime_object(graph, 'cycle with wrapper')

    def test_real_loader_and_callback_imports_remain_inert_with_large_runtime_data(self):
        source = '''
from jqdata import *
import redis

class JQOrderWrapper:
    def __init__(self, send_redis_signal=True):
        self.send_redis_signal_enabled = send_redis_signal
        self.redis_client = redis.Redis(host='must-not-connect')

def initialize(context):
    g.symbols = ['%06d.XSHE' % index for index in range(6000)]
    g.scores = {code: float(index) for index, code in enumerate(g.symbols)}
    run_daily(handle_open, time='open')

def handle_open(context):
    import redis
    g.callback_client = redis.Redis(host='must-not-connect-either')
    g.wrapper = JQOrderWrapper(send_redis_signal=True)
    g.symbols.append(g.wrapper)
'''
        strategy_path = self.path / 'large_universe_callback.py'
        strategy_path.write_text(source, encoding='utf-8')
        real_redis = ModuleType('redis')
        real_redis.Redis = Mock(side_effect=AssertionError('network Redis constructor must not run'))
        with patch.dict(sys.modules, {'redis': real_redis}):
            loaded = StrategyLoader().load(strategy_path, self.state)
            self.assertIsInstance(loaded.safety, BacktestRuntimeSafety)
            loaded.initialize(self.state.context)
            callback = self.state.scheduler.callbacks_for(self.state.context.current_dt, 'open')[0]
            callback(self.state.context)
            real_redis.Redis.assert_not_called()
            self.assertIsInstance(self.state.g.callback_client, InertRedisClient)
            self.assertFalse(self.state.g.wrapper.send_redis_signal_enabled)
            self.assertIsNone(self.state.g.wrapper.redis_client)
            self.assertIs(sys.modules['redis'], real_redis)


if __name__ == '__main__':
    unittest.main(verbosity=2)
