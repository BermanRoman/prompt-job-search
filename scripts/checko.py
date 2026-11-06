#!/usr/bin/env python3
"""Bounded company-only Checko client. All response content is untrusted data."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import urllib.parse
import urllib.request
import uuid

METHODS = {'search', 'company', 'finances', 'legal-cases', 'enforcements',
           'fedresurs', 'bankruptcy-messages', 'inspections'}
MAX_RESPONSE = 10 * 1024 * 1024


class CheckoError(ValueError):
    """Messages deliberately contain no request or provider content."""


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CheckoError('invalid integer')
    return value


def normalize(method, params):
    if method not in METHODS or not isinstance(params, dict):
        raise CheckoError('unsupported request')
    if method == 'search':
        if set(params) - {'by', 'obj', 'query', 'region', 'active', 'codes', 'limit', 'page'}:
            raise CheckoError('unsupported search parameter')
        if params.get('by') not in {'name', 'okved'} or params.get('obj') != 'org':
            raise CheckoError('company search required')
        query = params.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise CheckoError('invalid query')
        result = dict(params, query=query.strip())
        if 'region' in result and not re.fullmatch(r'[0-9]{2}', str(result['region'])):
            raise CheckoError('invalid region')
        if 'region' in result:
            result['region'] = str(result['region'])
        if result.get('active', 'true') is not True and result.get('active', 'true') != 'true':
            raise CheckoError('active companies required')
        result['active'] = 'true'
        if result['by'] == 'okved':
            if result.get('codes', 'all') != 'all':
                raise CheckoError('invalid codes')
            result['codes'] = 'all'
        elif 'codes' in result:
            raise CheckoError('codes only supported for okved')
        result['limit'] = integer(result.get('limit', 100), 1, 100)
        result['page'] = integer(result.get('page', 1), 1, 1000000)
        if result['by'] == 'name' and result['page'] != 1:
            raise CheckoError('name search limited to first 100 results')
        return result
    if set(params) not in ({'inn'}, {'ogrn'}):
        raise CheckoError('exactly one corporate identifier required; paging unsupported')
    field = next(iter(params))
    if not isinstance(params[field], str) or not re.fullmatch(r'[0-9]{%d}' % (10 if field == 'inn' else 13), params[field]):
        raise CheckoError('invalid corporate identifier')
    return dict(params)


def private_directory(path):
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise CheckoError('symlink path forbidden')
        if (part / '.git').exists():
            raise CheckoError('runtime must be outside Git')
    if not path.exists():
        path.mkdir(mode=0o700)  # parent must already exist
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CheckoError('runtime directory must be owned and mode 700')
    return path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CheckoError('redirect refused')


def network_transport(method, params, key, timeout):
    body = urllib.parse.urlencode(dict(params, key=key)).encode('utf-8')
    request = urllib.request.Request('https://api.checko.ru/v2/' + method, data=body,
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise CheckoError('response too large')
    return raw


def strict_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise CheckoError('duplicate JSON key')
            result[k] = v
        return result
    def invalid(value):
        raise CheckoError('invalid JSON number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class Client:
    def __init__(self, directory, transport=network_transport, now=time.time):
        self.directory = private_directory(directory)
        self.transport, self.now = transport, now
        path = self.directory / 'checko.sqlite'
        for candidate in [path, Path(str(path) + '-journal'), Path(str(path) + '-wal'), Path(str(path) + '-shm')]:
            if candidate.is_symlink():
                raise CheckoError('symlink database forbidden')
            if candidate.exists():
                info = candidate.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise CheckoError('database must be private regular file')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS budgets(id TEXT PRIMARY KEY, total INTEGER, daily INTEGER);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, budget TEXT, day TEXT,
          cache_key TEXT, state TEXT, created REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS pending ON attempts(cache_key)
          WHERE state IN ('reserved','unknown');
        CREATE TABLE IF NOT EXISTS responses(attempt TEXT PRIMARY KEY, received_at REAL, raw BLOB);
        CREATE TABLE IF NOT EXISTS cache(cache_key TEXT PRIMARY KEY, method TEXT,
          params TEXT, fetched_at REAL, raw BLOB, summary TEXT);
        ''')

    def close(self):
        self.db.close()

    def init_budget(self, budget, total, daily):
        if not isinstance(budget, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', budget):
            raise CheckoError('invalid budget ID')
        integer(total, 1, 1000000)
        integer(daily, 1, total)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT total,daily FROM budgets WHERE id=?', (budget,)).fetchone()
            if old and old != (total, daily):
                raise CheckoError('budget limits are immutable')
            self.db.execute('INSERT OR IGNORE INTO budgets VALUES(?,?,?)', (budget, total, daily))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return {'status': 'initialized', 'budget': budget, 'max_attempts': total, 'max_daily': daily}

    def read(self, cache_key, output):
        """Export only a successful cached response, with no network or budget mutation."""
        if not isinstance(cache_key, str) or not re.fullmatch(r'[0-9a-f]{64}', cache_key):
            raise CheckoError('invalid cache key')
        cached = self.db.execute('SELECT fetched_at,raw FROM cache WHERE cache_key=?', (cache_key,)).fetchone()
        if not cached:
            raise CheckoError('successful cache entry unavailable')
        fetched_at, raw = cached
        raw.decode('utf-8')  # Preserve exact bytes; refuse other encodings rather than transcode.
        target = Path(os.path.abspath(output))
        if not target.parent.is_dir():
            raise CheckoError('output parent must already exist')
        private_directory(target.parent)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                fd = None
                stream.write(raw)
        finally:
            if fd is not None:
                os.close(fd)
        return {'status': 'exported', 'sha256': hashlib.sha256(raw).hexdigest(), 'fetched_at': fetched_at}

    def request(self, method, params, budget, ttl, allow_network=False, refresh=False, timeout=30):
        params = normalize(method, params)
        if isinstance(ttl, bool) or not isinstance(ttl, (float, int)) or not math.isfinite(ttl) or not 0 <= ttl <= 31536000:
            raise CheckoError('invalid TTL')
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise CheckoError('invalid timeout')
        encoded = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        cache_key = hashlib.sha256((method + '\n' + encoded).encode()).hexdigest()
        now = self.now()
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
        attempt = uuid.uuid4().hex
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cached = self.db.execute('SELECT fetched_at,summary FROM cache WHERE cache_key=?', (cache_key,)).fetchone()
            if cached and not refresh and 0 <= now - cached[0] < ttl:
                self.db.execute('COMMIT')
                return dict(json.loads(cached[1]), status='cache_hit', cache_key=cache_key, fetched_at=cached[0])
            if not allow_network:
                raise CheckoError('network disabled; no fresh cache')
            key = os.environ.get('CHECKO_API_KEY')
            if not key or not key.strip():
                raise CheckoError('API key unavailable')
            limits = self.db.execute('SELECT total,daily FROM budgets WHERE id=?', (budget,)).fetchone()
            if not limits:
                raise CheckoError('initialize budget first')
            total, daily = self.db.execute('SELECT count(*),coalesce(sum(day=?),0) FROM attempts WHERE budget=?', (day, budget)).fetchone()
            if total >= limits[0] or daily >= limits[1]:
                raise CheckoError('attempt budget exhausted')
            if self.db.execute("SELECT 1 FROM attempts WHERE cache_key=? AND state IN ('reserved','unknown')", (cache_key,)).fetchone():
                raise CheckoError('request pending or unknown; automatic retry forbidden')
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)', (attempt, budget, day, cache_key, 'reserved', now))
            self.db.execute('COMMIT')
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        try:
            raw = self.transport(method, params, key, timeout)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
                raise CheckoError('invalid response')
            self.db.execute('INSERT INTO responses VALUES(?,?,?)', (attempt, self.now(), raw))
            envelope = strict_json(raw)
            if not isinstance(envelope, dict) or not isinstance(envelope.get('meta'), dict) or envelope['meta'].get('status') not in {'ok', 'error'}:
                raise CheckoError('invalid API envelope')
            meta = envelope['meta']
            summary = {'method': method, 'api_status': meta['status']}
            for field in ('today_request_count', 'balance'):
                value = meta.get(field)
                if type(value) in (int, float) and math.isfinite(value):
                    summary[field] = value
            fetched_at = self.now()
            self.db.execute('BEGIN IMMEDIATE')
            # API errors are retained privately as evidence, but never served as successful cache.
            if meta['status'] == 'ok':
                self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)',
                                (cache_key, method, encoded, fetched_at, raw, json.dumps(summary)))
            else:
                self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)',
                                (cache_key + ':error:' + attempt, method, encoded, fetched_at, raw, json.dumps(summary)))
            self.db.execute('UPDATE attempts SET state=? WHERE id=?', ('ok' if meta['status'] == 'ok' else 'api_error', attempt))
            self.db.execute('COMMIT')
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            self.db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (attempt,))
            raise CheckoError('request outcome unknown; attempt counted; no automatic retry') from None
        if meta['status'] == 'error':
            raise CheckoError('API reported error; attempt counted; details retained privately')
        return dict(summary, status='fetched', cache_key=cache_key, fetched_at=fetched_at)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--budget', required=True)
    init.add_argument('--max-attempts', type=int, required=True)
    init.add_argument('--max-daily', type=int, required=True)
    req = commands.add_parser('request')
    req.add_argument('method', choices=sorted(METHODS))
    req.add_argument('--params', required=True, help='JSON object; never include an API key')
    req.add_argument('--budget', required=True)
    req.add_argument('--ttl', type=float, required=True, help='Freshness in seconds, 0..31536000')
    req.add_argument('--timeout', type=float, default=30)
    req.add_argument('--allow-network', action='store_true')
    req.add_argument('--refresh', action='store_true')
    read = commands.add_parser('read')
    read.add_argument('--cache-key', required=True)
    read.add_argument('--output', required=True, help='New file in an existing private directory outside Git')
    client = None
    try:
        args = parser.parse_args(argv)
        client = Client(args.runtime)
        if args.command == 'init':
            result = client.init_budget(args.budget, args.max_attempts, args.max_daily)
        elif args.command == 'read':
            result = client.read(args.cache_key, args.output)
        else:
            result = client.request(args.method, strict_json(args.params), args.budget, args.ttl,
                                    args.allow_network, args.refresh, args.timeout)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        # Never render provider errors, JSON, filesystem paths or exception repr.
        print('Checko operation failed; inspect private state and documented constraints.', file=sys.stderr)
        return 2
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
