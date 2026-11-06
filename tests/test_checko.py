import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('checko', Path(__file__).resolve().parents[1] / 'scripts/checko.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def response(status='ok', data=None):
    return json.dumps({'meta': {'status': status, 'today_request_count': 9, 'balance': 0},
                       'data': data if data is not None else {'source_date': '2020-01-01', 'name': 'untrusted'}}).encode()


class CheckoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name).resolve() / 'private'
        self.clock = 1800000000.0
        self.calls = []
        self.env = patch.dict(os.environ, {'CHECKO_API_KEY': 'synthetic-secret-never-print'})
        self.env.start()
        self.client = self.open()
        self.client.init_budget('campaign', 5, 3)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def fake(self, method, params, key, timeout):
        self.calls.append((method, dict(params)))
        self.assertEqual(key, 'synthetic-secret-never-print')
        self.assertNotIn('key', params)
        return response()

    def open(self, transport=None):
        return M.Client(self.runtime, transport=transport or self.fake, now=lambda: self.clock)

    def request(self, **kwargs):
        options = dict(method='company', params={'inn': '1234567890'}, budget='campaign', ttl=60, allow_network=True)
        options.update(kwargs)
        return self.client.request(**options)

    def test_cache_hit_stale_refresh_and_no_fact_stdout(self):
        first = self.request()
        self.assertEqual(first['status'], 'fetched')
        self.assertNotIn('name', first)
        self.assertEqual(self.request(allow_network=False)['status'], 'cache_hit')
        self.assertEqual(len(self.calls), 1)
        self.clock += 60
        with self.assertRaises(M.CheckoError):
            self.request(allow_network=False)
        self.assertEqual(self.request()['status'], 'fetched')
        self.assertEqual(self.request(refresh=True)['status'], 'fetched')
        self.assertEqual(len(self.calls), 3)
        fetched, raw = self.client.db.execute('SELECT fetched_at,raw FROM cache').fetchone()
        self.assertEqual(fetched, self.clock)
        self.assertEqual(json.loads(raw)['data']['source_date'], '2020-01-01')

    def test_disabled_and_missing_key_reserve_nothing(self):
        with self.assertRaises(M.CheckoError):
            self.request(allow_network=False)
        with patch.dict(os.environ, {'CHECKO_API_KEY': ''}):
            with self.assertRaises(M.CheckoError):
                self.request()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_immutable_budget_and_cap_survive_restart(self):
        self.client.init_budget('tiny', 1, 1)
        self.request(budget='tiny')
        self.client.close()
        self.client = self.open()
        self.assertEqual(self.client.init_budget('tiny', 1, 1)['max_attempts'], 1)
        with self.assertRaises(M.CheckoError):
            self.client.init_budget('tiny', 2, 1)
        self.clock += 86400
        with self.assertRaises(M.CheckoError):
            self.request(budget='tiny', refresh=True)
        self.assertEqual(len(self.calls), 1)

    def test_daily_cap_counts_failed_attempts(self):
        self.client.init_budget('daily', 2, 1)
        self.client.transport = lambda *args: response('error')
        with self.assertRaises(M.CheckoError):
            self.request(budget='daily')
        with self.assertRaises(M.CheckoError):
            self.request(budget='daily', params={'inn': '2222222222'})
        self.clock += 86400
        self.client.transport = self.fake
        self.assertEqual(self.request(budget='daily')['status'], 'fetched')

    def test_timeout_persists_unknown_without_secret(self):
        def timeout(*args):
            raise TimeoutError('synthetic-secret-never-print')
        self.client.transport = timeout
        with self.assertRaises(M.CheckoError) as error:
            self.request()
        self.assertNotIn('synthetic-secret', str(error.exception))
        self.client.close()
        self.client = self.open()
        with self.assertRaises(M.CheckoError):
            self.request()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.db.execute('SELECT state FROM attempts').fetchone()[0], 'unknown')

    def test_api_errors_and_bad_envelopes(self):
        self.client.init_budget('schema', 10, 10)
        for payload in (b'not-json', b'{"meta":[]}', b'{"meta":{"status":"success"}}',
                        b'{"meta":{"status":"ok","status":"error"}}', b'{"meta":{"status":"ok"},"data":NaN}'):
            self.client.transport = lambda *args, raw=payload: raw
            with self.assertRaises(M.CheckoError):
                self.request(budget='schema', params={'inn': str(1000000000 + len(self.client.db.execute('SELECT * FROM attempts').fetchall()))})
        self.client.init_budget('errors', 2, 2)
        self.client.transport = lambda *args: response('error', {'error': 'synthetic-secret-never-print'})
        with self.assertRaises(M.CheckoError) as error:
            self.request(budget='errors', params={'inn': '9999999999'})
        self.assertNotIn('synthetic-secret', str(error.exception))
        self.assertEqual(self.client.db.execute("SELECT count(*) FROM cache WHERE cache_key LIKE '%:error:%'").fetchone()[0], 1)

    def test_concurrent_same_request_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return response()
        def worker():
            client = self.open(transport=slow)
            try:
                outcomes.append(client.request('company', {'inn': '1234567890'}, 'campaign', 60, True))
            except Exception as error:
                outcomes.append(error)
            finally:
                client.close()
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(M.CheckoError):
                self.request()
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(outcomes[0]['status'], 'fetched')
        self.assertEqual(self.client.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_concurrent_different_requests_share_cap(self):
        self.client.init_budget('one', 1, 1)
        gate = threading.Barrier(3)
        outcomes = []
        def worker(inn):
            client = self.open()
            try:
                gate.wait(5)
                outcomes.append(client.request('company', {'inn': inn}, 'one', 60, True)['status'])
            except M.CheckoError:
                outcomes.append('blocked')
            finally:
                client.close()
        threads = [threading.Thread(target=worker, args=(inn,)) for inn in ('1111111111', '2222222222')]
        for thread in threads:
            thread.start()
        gate.wait(5)
        for thread in threads:
            thread.join(5)
        self.assertCountEqual(outcomes, ['fetched', 'blocked'])
        self.assertEqual(self.client.db.execute("SELECT count(*) FROM attempts WHERE budget='one'").fetchone()[0], 1)

    def test_https_post_form_and_redirect_refusal(self):
        captured = {}
        class Reply:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                captured['read_limit'] = size
                return response()
        class Opener:
            def open(self, request, timeout):
                captured['request'], captured['timeout'] = request, timeout
                return Reply()
        with patch.object(M.urllib.request, 'build_opener', return_value=Opener()) as build:
            M.network_transport('company', {'inn': '1234567890'}, 'synthetic-secret', 9)
        request = captured['request']
        self.assertEqual(request.full_url, 'https://api.checko.ru/v2/company')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(M.urllib.parse.parse_qs(request.data.decode()),
                         {'inn': ['1234567890'], 'key': ['synthetic-secret']})
        self.assertNotIn('synthetic-secret', request.full_url)
        self.assertEqual(captured['read_limit'], M.MAX_RESPONSE + 1)
        self.assertEqual(build.call_args.args[0].proxies, {})
        with self.assertRaises(M.CheckoError):
            M.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_read_exact_private_bytes_no_network_or_overwrite(self):
        fetched = self.request()
        output = self.runtime / 'company.json'
        attempts = self.client.db.execute('SELECT count(*) FROM attempts').fetchone()[0]
        self.client.transport = lambda *args: self.fail('read must not invoke transport')
        with patch.dict(os.environ, {'CHECKO_API_KEY': ''}):
            result = self.client.read(fetched['cache_key'], output)
        self.assertEqual(output.read_bytes(), response())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result, {'status': 'exported', 'sha256': M.hashlib.sha256(response()).hexdigest(),
                                  'fetched_at': fetched['fetched_at']})
        self.assertEqual(self.client.db.execute('SELECT count(*) FROM attempts').fetchone()[0], attempts)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(FileExistsError):
            self.client.read(fetched['cache_key'], output)
        self.assertEqual(output.read_bytes(), response())

    def test_read_rejects_keys_and_unsafe_paths(self):
        fetched = self.request()
        output = self.runtime / 'not-created.json'
        for key in ('', '../x', 'A' * 64, 'f' * 64):
            with self.assertRaises(M.CheckoError):
                self.client.read(key, output)
        self.assertFalse(output.exists())
        unsafe = self.runtime.parent / 'unsafe-read'
        unsafe.mkdir(mode=0o755)
        with self.assertRaises(M.CheckoError):
            self.client.read(fetched['cache_key'], unsafe / 'output.json')
        linked = self.runtime.parent / 'linked-read'
        linked.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaises(M.CheckoError):
            self.client.read(fetched['cache_key'], linked / 'output.json')
        output.symlink_to(self.runtime / 'checko.sqlite')
        with self.assertRaises(OSError):
            self.client.read(fetched['cache_key'], output)
        repo = self.runtime.parent / 'repo-read'
        repo.mkdir(mode=0o700)
        (repo / '.git').mkdir()
        with self.assertRaises(M.CheckoError):
            self.client.read(fetched['cache_key'], repo / 'output.json')

    def test_read_cli_has_only_metadata_and_needs_no_key(self):
        fetched = self.request()
        out = io.StringIO()
        with patch.dict(os.environ, {'CHECKO_API_KEY': ''}), contextlib.redirect_stdout(out):
            result = M.main(['--runtime', str(self.runtime), 'read', '--cache-key', fetched['cache_key'],
                             '--output', str(self.runtime / 'cli-read.json')])
        self.assertEqual(result, 0)
        self.assertEqual(set(json.loads(out.getvalue())), {'status', 'sha256', 'fetched_at'})
        self.assertNotIn('untrusted', out.getvalue())
        self.assertEqual(len(self.calls), 1)

    def test_validation_and_methods(self):
        for method in M.METHODS - {'search'}:
            self.assertEqual(M.normalize(method, {'ogrn': '1234567890123'}), {'ogrn': '1234567890123'})
        for method, params in [('person', {'inn': '123456789012'}), ('company', {'inn': '123456789012'}),
                               ('company', {'inn': '1234567890', 'page': 2}), ('company', {'key': 'secret'}),
                               ('search', {'by': 'phone', 'obj': 'org', 'query': 'x'}),
                               ('search', {'by': 'name', 'obj': 'org', 'query': 'x', 'page': 2}),
                               ('search', {'by': 'okved', 'obj': 'org', 'query': 'x', 'limit': True})]:
            with self.assertRaises(M.CheckoError):
                M.normalize(method, params)
        normalized = M.normalize('search', {'by': 'okved', 'obj': 'org', 'query': ' 62.01 ', 'region': '77', 'page': 2})
        self.assertEqual(normalized['codes'], 'all')
        self.assertEqual(normalized['query'], '62.01')
        for ttl in (True, -1, float('nan'), float('inf'), 31536001):
            with self.assertRaises(M.CheckoError):
                self.request(ttl=ttl)
        self.assertEqual(self.calls, [])

    def test_private_paths_and_modes(self):
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.runtime / 'checko.sqlite').stat().st_mode & 0o777, 0o600)
        link = self.runtime.parent / 'link'
        link.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaises(M.CheckoError):
            M.Client(link)
        repo = self.runtime.parent / 'repo'
        repo.mkdir()
        (repo / '.git').mkdir()
        with self.assertRaises(M.CheckoError):
            M.Client(repo / 'runtime')
        unsafe = self.runtime.parent / 'unsafe'
        unsafe.mkdir(mode=0o755)
        with self.assertRaises(M.CheckoError):
            M.Client(unsafe)
        other = self.runtime.parent / 'other'
        other.mkdir(mode=0o700)
        (other / 'checko.sqlite').symlink_to(self.runtime / 'checko.sqlite')
        with self.assertRaises(M.CheckoError):
            M.Client(other)
        (other / 'checko.sqlite').unlink()
        os.link(self.runtime / 'checko.sqlite', other / 'checko.sqlite')
        with self.assertRaises(M.CheckoError):
            M.Client(other)
        (other / 'checko.sqlite').unlink()

    def test_cli_error_is_sanitized_and_stdout_contains_only_metadata(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            result = M.main(['--runtime', str(self.runtime), 'request', 'company', '--budget', 'campaign',
                             '--ttl', '60', '--params', '{"key":"synthetic-secret-never-print"}'])
        self.assertEqual(result, 2)
        self.assertNotIn('synthetic-secret', err.getvalue() + out.getvalue())
        self.clock = time.time()
        self.request()
        with contextlib.redirect_stdout(out):
            result = M.main(['--runtime', str(self.runtime), 'request', 'company', '--budget', 'campaign',
                             '--ttl', '31536000', '--params', '{"inn":"1234567890"}'])
        self.assertEqual(result, 0)
        self.assertNotIn('untrusted', out.getvalue())


if __name__ == '__main__':
    unittest.main()
