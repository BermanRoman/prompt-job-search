#!/usr/bin/env python3
"""Local, single-writer campaign ledger. No network or sending capability.

Approval/evidence fields record declarations, not their truth or authority.
Keep the database and inputs private and outside the public repository.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    def invalid(_):
        raise ValueError('non-finite JSON number')
    return json.loads(Path(path).read_text(encoding='utf-8'),
                      object_pairs_hook=pairs, parse_constant=invalid)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def fields(obj, required, optional=()):
    require(type(obj) is dict, 'expected JSON object')
    require(set(required) <= obj.keys() and obj.keys() <= set(required) | set(optional),
            'missing or unexpected fields')


def string(value):
    require(type(value) is str and bool(value.strip()), 'expected nonempty string')
    return value


def sha(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'expected SHA-256')
    return value


def timestamp(value):
    require(type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value),
            'expected UTC YYYY-MM-DDTHH:MM:SSZ')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


SCHEMA = '''
CREATE TABLE IF NOT EXISTS opportunities (
 id TEXT PRIMARY KEY, company TEXT NOT NULL, version TEXT NOT NULL,
 blocked TEXT, payload TEXT, reviewed TEXT, approval TEXT);
CREATE TABLE IF NOT EXISTS sources (
 source TEXT NOT NULL, source_id TEXT NOT NULL, opportunity TEXT NOT NULL,
 hash TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(source, source_id),
 FOREIGN KEY(opportunity) REFERENCES opportunities(id));
CREATE TABLE IF NOT EXISTS policies (
 id TEXT PRIMARY KEY, hash TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
 id TEXT PRIMARY KEY, opportunity TEXT NOT NULL, policy TEXT NOT NULL,
 policy_hash TEXT NOT NULL, payload_digest TEXT NOT NULL,
 approval TEXT NOT NULL, payload TEXT NOT NULL,
 created TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, evidence TEXT,
 FOREIGN KEY(opportunity) REFERENCES opportunities(id));
CREATE TABLE IF NOT EXISTS company_blocks (
 company TEXT PRIMARY KEY, created TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_revocations (
 policy TEXT PRIMARY KEY, created TEXT NOT NULL, evidence TEXT NOT NULL,
 FOREIGN KEY(policy) REFERENCES policies(id));
'''


class Ledger:
    def __init__(self, path, now=utc_now):
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript(SCHEMA)
        self.now = now

    def close(self):
        self.db.close()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        require(row is not None, 'record not found')
        return row

    def opportunity(self, value):
        return self.one('SELECT * FROM opportunities WHERE id=?', (string(value),))

    def active(self, value):
        return self.db.execute("SELECT 1 FROM attempts WHERE opportunity=? AND state IN "
                               "('reserved','unknown','sent')", (value,)).fetchone() is not None

    def blocked(self, op):
        return bool(op['blocked'] or self.db.execute(
            'SELECT 1 FROM company_blocks WHERE company=?', (op['company'],)).fetchone())

    def run(self, command, data=None):
        require(command in ('import', 'policy', 'prepare', 'review', 'reserve',
                            'outcome', 'block', 'queue', 'block-company',
                            'revoke-policy', 'status'), 'unknown command')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = getattr(self, 'cmd_' + command.replace('import', 'import_').replace('-', '_'))(data)
            self.db.execute('COMMIT')
            return result
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def cmd_import_(self, data):
        fields(data, ('vacancies',))
        require(type(data['vacancies']) is list, 'vacancies must be a list')
        changed = set()
        for item in data['vacancies']:
            fields(item, ('source', 'id', 'opportunity_id', 'company_id',
                          'identity_evidence', 'content'))
            for key in ('source', 'id', 'opportunity_id', 'company_id', 'identity_evidence'):
                string(item[key])
            require(type(item['content']) is dict and item['content'], 'content must be an object')
            oid = item['opportunity_id']
            old = self.db.execute('SELECT * FROM sources WHERE source=? AND source_id=?',
                                  (item['source'], item['id'])).fetchone()
            require(old is None or old['opportunity'] == oid, 'source identity cannot be reassigned')
            self.db.execute('INSERT OR IGNORE INTO opportunities(id,company,version) VALUES(?,?,?)',
                            (oid, item['company_id'], digest([])))
            op = self.opportunity(oid)
            require(op['company'] == item['company_id'], 'opportunity company mismatch')
            h = digest(item['content'])
            self.db.execute('INSERT INTO sources VALUES(?,?,?,?,?) ON CONFLICT(source,source_id) '
                            'DO UPDATE SET hash=excluded.hash,evidence=excluded.evidence',
                            (item['source'], item['id'], oid, h, item['identity_evidence']))
            rows = self.db.execute('SELECT source,source_id,hash,evidence FROM sources '
                                   'WHERE opportunity=? ORDER BY source,source_id', (oid,)).fetchall()
            version = digest([list(row) for row in rows])
            if version != op['version']:
                changed.add(oid)
                self.db.execute('UPDATE opportunities SET version=?,reviewed=NULL,approval=NULL '
                                'WHERE id=?', (version, oid))
        return {'changed': sorted(changed)}

    def cmd_policy(self, data):
        fields(data, ('id', 'expires_at', 'allowed_channels', 'max_total', 'max_daily',
                      'policy_version', 'opportunity_ids', 'company_ids', 'approval_digest'))
        for key in ('id', 'policy_version'):
            string(data[key])
        sha(data['approval_digest'])
        require(timestamp(data['expires_at']) > self.now(), 'policy expired')
        for key in ('allowed_channels', 'opportunity_ids', 'company_ids'):
            require(type(data[key]) is list and len(data[key]) == len(set(map(canonical, data[key]))),
                    'scope/channels must be unique lists')
            for value in data[key]:
                string(value)
        require(data['allowed_channels'] and set(data['allowed_channels']) <= {'hh', 'ats', 'email'},
                'unsupported channel')
        require(data['opportunity_ids'] or data['company_ids'], 'empty policy scope')
        for key in ('max_total', 'max_daily'):
            require(type(data[key]) is int and data[key] > 0, 'caps must be positive integers')
        h = digest(data)
        existing = self.db.execute('SELECT hash FROM policies WHERE id=?', (data['id'],)).fetchone()
        require(existing is None or existing['hash'] == h, 'policy ID is immutable; use a new ID')
        self.db.execute('INSERT OR IGNORE INTO policies VALUES(?,?,?)', (data['id'], h, canonical(data)))
        return {'policy_id': data['id'], 'policy_hash': h}

    def cmd_prepare(self, data):
        fields(data, ('opportunity_id', 'channel', 'recipient', 'artifact_path'))
        op = self.opportunity(data['opportunity_id'])
        require(not self.blocked(op) and not self.active(op['id']), 'opportunity blocked or already attempted')
        require(data['channel'] in ('hh', 'ats', 'email'), 'unsupported channel')
        string(data['recipient'])
        artifact = Path(string(data['artifact_path'])).resolve()
        h = hashlib.sha256(artifact.read_bytes()).hexdigest()
        payload = {'opportunity_id': op['id'], 'version': op['version'], 'channel': data['channel'],
                   'recipient': data['recipient'], 'artifact_path': str(artifact), 'artifact_hash': h}
        body = canonical(payload)
        if body != op['payload']:
            self.db.execute('UPDATE opportunities SET payload=?,reviewed=NULL,approval=NULL WHERE id=?',
                            (body, op['id']))
        return {'opportunity_id': op['id'], 'payload_digest': digest(payload), 'artifact_hash': h}

    def current_payload(self, op):
        require(op['payload'], 'preparation required')
        payload = json.loads(op['payload'])
        require(payload['version'] == op['version'], 'vacancy changed; prepare again')
        actual = hashlib.sha256(Path(payload['artifact_path']).read_bytes()).hexdigest()
        require(actual == payload['artifact_hash'], 'artifact changed; prepare again')
        return payload, digest(payload)

    def cmd_review(self, data):
        fields(data, ('opportunity_id', 'payload_digest', 'approval_digest'))
        op = self.opportunity(data['opportunity_id'])
        require(not self.blocked(op) and not self.active(op['id']), 'opportunity blocked or already attempted')
        _, h = self.current_payload(op)
        require(sha(data['payload_digest']) == h, 'review does not match payload')
        sha(data['approval_digest'])
        self.db.execute('UPDATE opportunities SET reviewed=?,approval=? WHERE id=?',
                        (h, data['approval_digest'], op['id']))
        return {'opportunity_id': op['id'], 'status': 'review_recorded', 'payload_digest': h}

    def cmd_reserve(self, data):
        fields(data, ('opportunity_id', 'policy_id', 'policy_hash', 'payload_digest'))
        op = self.opportunity(data['opportunity_id'])
        require(not self.blocked(op) and not self.active(op['id']), 'opportunity blocked or already attempted')
        payload, h = self.current_payload(op)
        require(sha(data['payload_digest']) == h == op['reviewed'] and op['approval'],
                'current review required')
        row = self.one('SELECT * FROM policies WHERE id=?', (string(data['policy_id']),))
        require(not self.db.execute('SELECT 1 FROM policy_revocations WHERE policy=?',
                                   (row['id'],)).fetchone(), 'policy revoked')
        require(sha(data['policy_hash']) == row['hash'], 'policy hash mismatch')
        policy = json.loads(row['body'])
        now = self.now()
        require(timestamp(policy['expires_at']) > now, 'policy expired')
        require(payload['channel'] in policy['allowed_channels'], 'channel outside policy')
        require(op['id'] in policy['opportunity_ids'] or op['company'] in policy['company_ids'],
                'opportunity outside policy')
        # Limits cover all policies: replacing permission cannot reset consumption.
        used = self.db.execute("SELECT created FROM attempts WHERE state != 'not_sent'").fetchall()
        require(len(used) < policy['max_total'], 'total cap reached')
        require(sum(row['created'][:10] == stamp(now)[:10] for row in used) < policy['max_daily'],
                'daily cap reached')
        rid = uuid.uuid4().hex
        self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (rid, op['id'], policy['id'], row['hash'], h, op['approval'],
                         op['payload'], stamp(now), 'reserved', None, None))
        return {'reservation_id': rid, 'opportunity_id': op['id'], 'status': 'reserved'}

    def cmd_outcome(self, data):
        fields(data, ('reservation_id', 'status'), ('receipt', 'external_evidence'))
        attempt = self.one('SELECT * FROM attempts WHERE id=?', (string(data['reservation_id']),))
        status = data['status']
        require(status in ('sent', 'unknown', 'not_sent', 'rejected', 'no_contact'), 'invalid outcome')
        require(attempt['state'] in ('reserved', 'unknown') or
                (attempt['state'] == 'sent' and status in ('rejected', 'no_contact')),
                'outcome is final')
        receipt = data.get('receipt', attempt['receipt'])
        evidence = data.get('external_evidence')
        if status == 'sent':
            string(receipt)
        if status in ('not_sent', 'rejected', 'no_contact'):
            string(evidence)
        if receipt is not None:
            string(receipt)
        if evidence is not None:
            string(evidence)
        self.db.execute('UPDATE attempts SET state=?,receipt=?,evidence=? WHERE id=?',
                        (status, receipt, evidence, attempt['id']))
        if status in ('rejected', 'no_contact'):
            self.db.execute('UPDATE opportunities SET blocked=?,reviewed=NULL,approval=NULL WHERE id=?',
                            (status, attempt['opportunity']))
        elif status == 'not_sent':
            self.db.execute('UPDATE opportunities SET payload=NULL,reviewed=NULL,approval=NULL WHERE id=?',
                            (attempt['opportunity'],))
        return {'reservation_id': attempt['id'], 'status': status}

    def cmd_block(self, data):
        fields(data, ('opportunity_id', 'reason', 'external_evidence'))
        op = self.opportunity(data['opportunity_id'])
        require(data['reason'] in ('rejected', 'no_contact'), 'invalid block reason')
        string(data['external_evidence'])
        self.db.execute('UPDATE opportunities SET blocked=?,reviewed=NULL,approval=NULL WHERE id=?',
                        (data['reason'], op['id']))
        # Preserve the evidence without creating a send attempt.
        self.db.execute('CREATE TABLE IF NOT EXISTS blocks(opportunity TEXT, reason TEXT, evidence TEXT)')
        self.db.execute('INSERT INTO blocks VALUES(?,?,?)',
                        (op['id'], data['reason'], data['external_evidence']))
        return {'opportunity_id': op['id'], 'status': 'blocked'}

    def cmd_queue(self, data):
        result = []
        for op in self.db.execute('SELECT * FROM opportunities ORDER BY id').fetchall():
            if self.blocked(op) or self.active(op['id']):
                continue
            try:
                _, h = self.current_payload(op)
                if h == op['reviewed']:
                    continue
            except (ValueError, OSError):
                pass
            result.append({'opportunity_id': op['id'], 'version': op['version']})
        return {'queue': result}

    def cmd_block_company(self, data):
        fields(data, ('company_id', 'external_evidence'))
        company = string(data['company_id'])
        evidence = string(data['external_evidence'])
        self.db.execute('INSERT OR IGNORE INTO company_blocks VALUES(?,?,?)',
                        (company, stamp(self.now()), evidence))
        self.db.execute('UPDATE opportunities SET reviewed=NULL,approval=NULL WHERE company=?', (company,))
        return {'company_id': company, 'status': 'blocked'}

    def cmd_revoke_policy(self, data):
        fields(data, ('policy_id', 'external_evidence'))
        policy = self.one('SELECT id FROM policies WHERE id=?', (string(data['policy_id']),))
        evidence = string(data['external_evidence'])
        self.db.execute('INSERT OR IGNORE INTO policy_revocations VALUES(?,?,?)',
                        (policy['id'], stamp(self.now()), evidence))
        return {'policy_id': policy['id'], 'status': 'revoked'}

    def cmd_status(self, data):
        result = []
        for op in self.db.execute('SELECT * FROM opportunities ORDER BY id').fetchall():
            state, payload_digest = 'needs_preparation', None
            attempt = self.db.execute("SELECT state FROM attempts WHERE opportunity=? "
                                      "AND state IN ('reserved','unknown','sent') LIMIT 1", (op['id'],)).fetchone()
            if self.blocked(op):
                state = 'blocked'
            elif attempt:
                state = attempt['state']
            else:
                try:
                    _, payload_digest = self.current_payload(op)
                    state = 'ready' if payload_digest == op['reviewed'] else 'needs_review'
                except (ValueError, OSError):
                    pass
            result.append({'opportunity_id': op['id'], 'company_id': op['company'],
                           'state': state, 'version': op['version'], 'payload_digest': payload_digest})
        attempts = [dict(row) for row in self.db.execute(
            'SELECT id AS reservation_id,opportunity AS opportunity_id,state,created AS reserved_at,'
            'payload_digest,policy AS policy_id,policy_hash FROM attempts ORDER BY created,id')]
        revoked = [dict(row) for row in self.db.execute(
            'SELECT policy AS policy_id,created AS revoked_at FROM policy_revocations ORDER BY policy')]
        companies = [dict(row) for row in self.db.execute(
            'SELECT company AS company_id,created AS blocked_at FROM company_blocks ORDER BY company')]
        return {'opportunities': result, 'attempts': attempts, 'revoked_policies': revoked,
                'blocked_companies': companies}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('command', choices=('import', 'policy', 'prepare', 'review', 'reserve',
                                            'outcome', 'block', 'queue', 'block-company',
                                            'revoke-policy', 'status'))
    parser.add_argument('--input')
    args = parser.parse_args()
    ledger = None
    try:
        require(args.command in ('queue', 'status') or args.input, '--input required')
        data = read_json(args.input) if args.input else None
        ledger = Ledger(args.db)
        print(canonical(ledger.run(args.command, data)))
        return 0
    except (ValueError, OSError, sqlite3.Error, TypeError, OverflowError):
        # Inputs may contain contacts or private resume text. Do not echo them.
        print('{"error":"local validation or storage failure; no action authorized"}', file=sys.stderr)
        return 2
    finally:
        if ledger:
            ledger.close()


if __name__ == '__main__':
    sys.exit(main())
