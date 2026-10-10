"""Production entry points must preserve the requested financial data contract."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from jq_tushare_sdk import cli
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.readiness import DataReadinessCheck, ReadinessIssue
from jq_tushare_sdk.runtime.engine import BacktestEngine
from jq_tushare_sdk.web import app


OUT = Path.cwd() / 'jq_tushare_backtests/sdk_consistency_repaired_20261009/mode_wiring_units'


class MinimalBackend:
    def __init__(self, mode='cached_snapshot', ready=True):
        self.financial_mode = mode
        self.ready = ready
        self.cutoffs = []

    def financial_readiness(self, as_of):
        self.cutoffs.append(as_of)
        return {'ready': self.ready, 'reason': 'ready' if self.ready else 'financial_vintage_missing',
                'legacy_rows': 1, 'visible_revision_rows': 1 if self.ready else 0,
                'legacy_cache_historical_vintages_recovered': False}

    def fetch(self, api_name, **params):
        if api_name == 'trade_cal':
            rows = [{'exchange': 'SSE', 'cal_date': day, 'is_open': 1}
                    for day in ('20260928', '20260929', '20260930')]
            return pd.DataFrame([row for row in rows
                if (not params.get('start_date') or row['cal_date'] >= str(params['start_date']))
                and (not params.get('end_date') or row['cal_date'] <= str(params['end_date']))])
        return pd.DataFrame()


class FinancialModeWiring(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=OUT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.strategy = self.root / 'strategy.py'
        self.strategy.write_text('from jqdata import *\ndef initialize(context):\n    run_daily(handle, time="open")\ndef handle(context):\n    log.info("mode-wiring callback")\n')

    def config(self, mode='cached_snapshot'):
        return BacktestConfig(str(self.strategy), '2026-09-29', '2026-09-30', 10000.,
            str(self.root / 'cache.db'), output_dir=str(self.root / 'runs'), financial_mode=mode)

    def cli_args(self, command, mode=None):
        args = [command, str(self.strategy), '--start', '2026-09-29', '--end', '2026-09-30',
                '--cache-db', str(self.root / 'cache.db'), '--output-dir', str(self.root / 'runs')]
        if command == 'backtest': args += ['--no-deterministic']
        if mode is not None: args += ['--financial-mode', mode]
        return args

    def test_config_roundtrip_and_invalid_mode(self):
        for mode in ('cached_snapshot', 'research_reconstructed', 'strict_pit'):
            config = self.config(mode)
            self.assertEqual(BacktestConfig(**json.loads(json.dumps(config.to_json_dict()))), config)
        with self.assertRaises(ValueError): self.config('silent_fallback')

    def test_cli_parser_default_choices_for_both_commands(self):
        for command in ('check-data', 'backtest'):
            self.assertEqual(cli.build_parser().parse_args(self.cli_args(command)).financial_mode, 'cached_snapshot')
            self.assertEqual(cli.build_parser().parse_args(self.cli_args(command, 'strict_pit')).financial_mode, 'strict_pit')
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.build_parser().parse_args(self.cli_args(command, 'invalid'))

    def test_cli_check_passes_mode_to_config_and_backend(self):
        backend = mock.Mock()
        with mock.patch.object(cli, 'TushareCacheBackend', return_value=backend) as constructor, \
             mock.patch.object(cli, '_check_local_readiness', return_value=[]) as check, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(self.cli_args('check-data', 'strict_pit')), 0)
        self.assertEqual(constructor.call_args.kwargs['financial_mode'], 'strict_pit')
        self.assertEqual(check.call_args.args[0].financial_mode, 'strict_pit')
        backend.close.assert_called_once()

    def test_cli_backtest_preserves_research_mode(self):
        backend = mock.Mock()
        with mock.patch.object(cli, 'TushareCacheBackend', return_value=backend) as constructor, \
             mock.patch.object(cli, '_ensure_local_readiness', return_value=[]), \
             mock.patch.object(cli, 'BacktestEngine') as engine, redirect_stdout(io.StringIO()):
            engine.return_value.run.return_value = SimpleNamespace(run_dir=self.root / 'result')
            self.assertEqual(cli.main(self.cli_args('backtest', 'research_reconstructed')), 0)
        self.assertEqual(constructor.call_args.kwargs['financial_mode'], 'research_reconstructed')
        self.assertEqual(engine.call_args.args[0].financial_mode, 'research_reconstructed')

    def test_cli_does_not_autoupdate_missing_historical_vintages(self):
        issue = ReadinessIssue('financial_vintages', 'financial_vintage_missing', 'Import dated versions')
        with mock.patch.object(cli, 'TushareCacheBackend'), \
             mock.patch.object(cli, '_local_readiness_issues', return_value=[issue]), \
             mock.patch.object(cli, 'update_missing_data') as update, \
             mock.patch.object(cli, 'BacktestEngine') as engine, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(self.cli_args('backtest', 'strict_pit')), 1)
        update.assert_not_called()
        engine.assert_not_called()

    def test_strict_income_preflight_is_nonrepairable_without_vintages(self):
        backend = MinimalBackend('strict_pit', ready=False)
        issue = DataReadinessCheck(backend)._check_income(self.config('strict_pit'), '20260929', '20260930')
        self.assertEqual(issue.api_name, 'financial_vintages')
        self.assertIn('financial_vintage_missing', issue.message)
        self.assertEqual(issue.update_requests, ())
        self.assertEqual(backend.cutoffs, ['2026-09-29 00:00:00'])
        issue = DataReadinessCheck(SimpleNamespace())._check_income(self.config('strict_pit'), '20260929', '20260930')
        self.assertEqual(issue.api_name, 'financial_vintages')

    def test_web_request_and_worker_config_roundtrip_preserve_mode(self):
        request = app.BacktestRequest.from_payload({'strategy_path': str(self.strategy), 'start_date': '2026-09-29',
            'end_date': '2026-09-30', 'initial_cash': 10000., 'financial_mode': 'strict_pit'})
        manager = app.BacktestJobManager(project_root=self.root, default_cache_db=self.root / 'cache.db',
            output_dir=self.root / 'runs', runner=lambda _: None)
        config = manager._config_from_request(request)
        self.assertEqual(config.financial_mode, 'strict_pit')
        self.assertEqual(BacktestConfig(**json.loads(json.dumps(config.to_json_dict()))).financial_mode, 'strict_pit')
        bad = app.BacktestRequest.from_payload({'strategy_path': str(self.strategy), 'start_date': '2026-09-29',
            'end_date': '2026-09-30', 'initial_cash': 10000., 'financial_mode': 'invalid'})
        with self.assertRaises(ValueError): manager._config_from_request(bad)

    def test_web_runner_passes_mode_to_backend(self):
        import jq_tushare_sdk.adapters.tushare.cache_backend as backend_module
        import jq_tushare_sdk.runtime.engine as engine_module
        backend = mock.Mock()
        with mock.patch.object(backend_module, 'TushareCacheBackend', return_value=backend) as constructor, \
             mock.patch.object(app, '_readiness_issues', return_value=[]), \
             mock.patch.object(engine_module, 'BacktestEngine') as engine:
            result = SimpleNamespace(run_dir=self.root / 'fake')
            engine.return_value.run.return_value = result
            self.assertIs(app._run_local_backtest(self.config('research_reconstructed')), result)
        self.assertEqual(constructor.call_args.kwargs['financial_mode'], 'research_reconstructed')
        self.assertEqual(engine.call_args.args[0].financial_mode, 'research_reconstructed')

    def test_web_strict_preflight_does_not_autoupdate(self):
        import jq_tushare_sdk.adapters.tushare.cache_backend as backend_module
        import jq_tushare_sdk.data.readiness as readiness_module
        issue = ReadinessIssue('financial_vintages', 'financial_vintage_missing', 'Import dated versions')
        with mock.patch.object(backend_module, 'TushareCacheBackend'), \
             mock.patch.object(app, '_readiness_issues', return_value=[issue]), \
             mock.patch.object(readiness_module, 'update_missing_data') as update:
            with self.assertRaisesRegex(RuntimeError, 'financial_vintage_missing'):
                app._run_local_backtest(self.config('strict_pit'))
        update.assert_not_called()

    def test_engine_records_actual_nondefault_backend_mode(self):
        engine = BacktestEngine(self.config(), MinimalBackend('research_reconstructed'))
        self.assertEqual(engine.config.financial_mode, 'research_reconstructed')
        with self.assertRaisesRegex(ValueError, 'financial_mode mismatch'):
            BacktestEngine(self.config('strict_pit'), MinimalBackend('cached_snapshot'))
        with self.assertRaisesRegex(ValueError, 'financial_mode mismatch'):
            BacktestEngine(self.config('research_reconstructed'), MinimalBackend('strict_pit'))

    def test_engine_manifests_do_not_promote_mode_to_pit_eligibility(self):
        for mode in ('cached_snapshot', 'research_reconstructed', 'strict_pit'):
            manifest = BacktestEngine(self.config(), MinimalBackend(mode)).run()
            payload = json.loads((manifest.run_dir / 'manifest.json').read_text())
            config = json.loads((manifest.run_dir / 'config.json').read_text())
            self.assertEqual(payload['financial_mode'], mode)
            self.assertEqual(config['financial_mode'], mode)
            self.assertIs(payload['strict_pit_eligible'], False)
            self.assertEqual(payload['financial_provenance']['verified'], mode == 'strict_pit')
            self.assertEqual(payload['financial_provenance']['as_of'], '2026-09-29 00:00:00')

    def test_engine_strict_missing_vintage_fails_before_strategy_or_output(self):
        engine = BacktestEngine(self.config('strict_pit'), MinimalBackend('strict_pit', ready=False))
        with self.assertRaisesRegex(ValueError, 'financial_vintage_missing'):
            engine.run()
        self.assertIsNone(engine.active_run_dir)
        self.assertFalse(Path(engine.config.output_dir).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
