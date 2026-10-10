"""Console controls must preserve the user's explicit financial data mode."""
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from jq_tushare_sdk.web import app as web_app


MODES = ('cached_snapshot', 'research_reconstructed', 'strict_pit')


class ModeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_mode = False
        self.options = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'select' and attributes.get('id') == 'financial-mode':
            self.in_mode = True
        if tag == 'option' and self.in_mode:
            self.options.append(attributes)

    def handle_endtag(self, tag):
        if tag == 'select':
            self.in_mode = False


class WebFinancialModeTests(unittest.TestCase):
    def test_settings_render_all_modes_with_unchanged_original_snapshot_default(self):
        html = web_app._render_app_html(Path('/repo'), Path('/repo/cache.db'), Path('/repo/runs'))
        parser = ModeParser()
        parser.feed(html)
        self.assertEqual([option['value'] for option in parser.options], list(MODES))
        self.assertEqual([option['value'] for option in parser.options if 'selected' in option], ['cached_snapshot'])
        self.assertGreater(html.index('id="financial-mode"'), html.index('id="settings-panel"'))
        self.assertIn('财务数据模式', html)
        self.assertIn('研究重构（非原生复现）', html)
        self.assertIn('严格历史（需历史版本）', html)

    @unittest.skipUnless(shutil.which('node'), 'Node.js needed for JavaScript request contract')
    def test_active_and_legacy_payload_functions_submit_selected_mode(self):
        # This executes only the pure payload functions against scalar controls.
        # It does not start a browser or render a local/remote page.
        source = Path(web_app.__file__).read_text(encoding='utf-8')
        bodies = re.findall(r'^function requestPayload\(\) \{.*?^\}', source, re.M | re.S)
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            for mode in MODES:
                controls = {
                    'strategy-select': {'value': 'strategy.py'},
                    'start-date': {'value': '2026-07-01'}, 'end-date': {'value': '2026-09-30'},
                    'initial-cash': {'value': '1000000'}, 'cache-db': {'value': 'cache.db'},
                    'output-dir': {'value': 'runs'}, 'optimize-data': {'checked': True},
                    'financial-mode': {'value': mode},
                }
                script = ('const state={strategyPath:"strategy.py"};\nconst controls='
                          + json.dumps(controls) + ';\nconst $=(id)=>controls[id];\n'
                          + body + '\nprocess.stdout.write(JSON.stringify(requestPayload()));')
                with self.subTest(mode=mode, legacy='strategy-select' in body):
                    completed = subprocess.run([shutil.which('node'), '-e', script], check=True,
                                               capture_output=True, text=True, timeout=5)
                    self.assertEqual(json.loads(completed.stdout)['financial_mode'], mode)
        active = web_app._app_js()
        self.assertIn('const request = requestPayload();', active)
        self.assertIn('async function runDataReadinessCheck(request = requestPayload())', active)

    def test_payload_without_new_control_preserves_cached_snapshot(self):
        request = web_app.BacktestRequest.from_payload({})
        self.assertEqual(request.financial_mode, 'cached_snapshot')

    def test_http_check_and_backtest_preserve_all_modes_and_reject_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'strategy.py').write_text('def initialize(context):\n    pass\n', encoding='utf-8')
            manager = web_app.BacktestJobManager(project_root=root, default_cache_db=root / 'cache.db',
                                                 output_dir=root / 'runs', synchronous=True)
            handler = web_app._handler_factory(project_root=root, cache_db=root / 'cache.db',
                output_dir=root / 'runs', manager=manager, run_store=web_app.RunStore(root / 'runs'))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f'http://127.0.0.1:{server.server_address[1]}'

            def post(path, mode):
                payload = {'strategy_path': 'strategy.py', 'start_date': '2026-07-01',
                           'end_date': '2026-09-30', 'initial_cash': 1_000_000,
                           'financial_mode': mode}
                request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                with opener.open(request, timeout=3) as response:
                    return json.loads(response.read())

            try:
                with mock.patch.object(web_app, '_check_data_readiness', return_value=[]) as readiness, \
                     mock.patch.object(manager, '_run_job') as launch:
                    for mode in MODES:
                        with self.subTest(mode=mode):
                            checked = post('/api/check-data', mode)
                            self.assertTrue(checked['ok'])
                            self.assertEqual(checked['financial_mode'], mode)
                            self.assertEqual(readiness.call_args.args[0].financial_mode, mode)
                            started = post('/api/backtests', mode)
                            self.assertEqual(started['job']['financial_mode'], mode)
                            self.assertEqual(launch.call_args.args[1].financial_mode, mode)
                    for path in ('/api/check-data', '/api/backtests'):
                        with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                            post(path, 'silently_degrade')
                        self.assertEqual(caught.exception.code, 400)
                        self.assertIn('Unsupported financial_mode', caught.exception.read().decode())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
