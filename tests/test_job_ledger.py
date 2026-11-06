"""Synthetic local cases only; no network, actual contacts, or personal data."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

SPEC = importlib.util.spec_from_file_location('ledger', Path(__file__).parents[1] / 'scripts/job_ledger.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
TABLE_SPEC = importlib.util.spec_from_file_location('company_table', Path(__file__).parents[1] / 'scripts/company_table.py')
TABLE = importlib.util.module_from_spec(TABLE_SPEC)
TABLE_SPEC.loader.exec_module(TABLE)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'ledger.sqlite'
        self.clock = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.ledger = M.Ledger(self.path, now=lambda: self.clock)
        self.addCleanup(lambda: self.ledger.close())
        self.artifact = self.root / 'sample.txt'
        self.artifact.write_text('Synthetic confirmed fact only.\n')

    def run_cmd(self, command, data=None):
        return self.ledger.run(command, data)

    def vacancy(self, oid='role-1', source='hh', sid='123', content=None, company_id='company-1'):
        return {'source': source, 'id': sid, 'opportunity_id': oid, 'company_id': company_id,
                'identity_evidence': 'Synthetic matching employer requisition REQ-1',
                'content': content or {'title': 'Example role'}}

    def import_one(self, **kwargs):
        self.install_card(kwargs.get('company_id', 'company-1'))
        return self.run_cmd('import', {'vacancies': [self.vacancy(**kwargs)]})

    def install_card(self, company_id):
        self.ledger.db.execute(TABLE.SCHEMA)
        evidence = {'source': 'fixture:verified-synthetic', 'scope': 'synthetic test only',
                    'checked_at': '2029-12-31T00:00:00Z', 'valid_until': '2030-01-05T00:00:00Z'}
        synthetic_inn = '99' + str(int(M.hashlib.sha256(company_id.encode()).hexdigest()[:8], 16)).zfill(10)[-8:]
        body = {'company_id': company_id, 'wave': 1, 'inn': synthetic_inn,
                'checks': {name: {'status': 'pass', 'evidence': [evidence]} for name in TABLE.screen.CHECKS}}
        TABLE.import_cards(self.ledger.db, {'companies': [{**body, 'expected_version': None}]}, self.clock)

    def policy(self, **updates):
        data = {'id': 'permission-1', 'expires_at': '2030-01-02T00:00:00Z',
                'allowed_channels': ['hh', 'ats', 'email'], 'max_total': 10, 'max_daily': 10,
                'policy_version': '1', 'opportunity_ids': [], 'company_ids': ['company-1'],
                'approval_digest': 'a' * 64}
        data.update(updates)
        return self.run_cmd('policy', data)

    def prepare(self, oid='role-1', channel='hh', recipient='synthetic-recipient'):
        return self.run_cmd('prepare', {'opportunity_id': oid, 'channel': channel,
                                       'recipient': recipient, 'artifact_path': str(self.artifact)})

    def reviewed(self, oid='role-1', **kwargs):
        result = self.prepare(oid, **kwargs)
        self.run_cmd('review', {'opportunity_id': oid, 'payload_digest': result['payload_digest'],
                                'approval_digest': 'b' * 64})
        return result

    def reserve(self, oid='role-1', policy=None, prepared=None):
        policy = policy or self.policy()
        prepared = prepared or self.reviewed(oid)
        return self.run_cmd('reserve', {'opportunity_id': oid, **policy,
                                       'payload_digest': prepared['payload_digest']})

    def test_reimport_and_cross_channel_identity(self):
        self.assertEqual(self.import_one()['changed'], ['role-1'])
        self.assertEqual(self.import_one()['changed'], [])
        self.import_one(source='ats', sid='REQ-1')
        self.assertEqual(len(self.run_cmd('queue')['queue']), 1)
        self.import_one(oid='role-2', sid='different')  # same title alone never merges
        self.assertEqual(len(self.run_cmd('queue')['queue']), 2)
        with self.assertRaises(ValueError):
            self.import_one(oid='role-2')  # stable source ID cannot move

    def test_prepare_changes_and_vacancy_changes_revoke_review(self):
        self.import_one()
        old = self.reviewed()
        policy = self.policy()
        fresh = self.prepare(channel='email')
        self.assertNotEqual(old['payload_digest'], fresh['payload_digest'])
        with self.assertRaises(ValueError):
            self.reserve(policy=policy, prepared=fresh)
        self.reviewed()
        self.import_one(content={'title': 'Changed scope'})
        with self.assertRaises(ValueError):
            self.reserve(policy=policy, prepared=old)
        self.assertEqual(len(self.run_cmd('queue')['queue']), 1)

    def test_external_artifact_edit_invalidates_review(self):
        self.import_one()
        prepared = self.reviewed()
        self.artifact.write_text('Changed synthetic material')
        with self.assertRaises(ValueError):
            self.reserve(prepared=prepared)
        self.assertEqual(len(self.run_cmd('queue')['queue']), 1)

    def test_expiry_scope_channel_and_policy_hash(self):
        self.import_one()
        prepared = self.reviewed()
        policy = self.policy(allowed_channels=['email'])
        with self.assertRaises(ValueError):
            self.reserve(policy=policy, prepared=prepared)
        other = self.policy(id='scope', company_ids=['other-company'])
        with self.assertRaises(ValueError):
            self.reserve(policy=other, prepared=prepared)
        good = self.policy(id='good')
        with self.assertRaises(ValueError):
            self.reserve(policy={**good, 'policy_hash': '0' * 64}, prepared=prepared)
        self.clock += timedelta(days=1)
        with self.assertRaises(ValueError):
            self.reserve(policy=good, prepared=prepared)

    def test_pending_caps_and_policy_change_cannot_reset(self):
        self.import_one()
        limited = self.policy(max_total=1)
        self.reserve(policy=limited)
        self.import_one(oid='role-2', sid='2')
        prepared = self.reviewed('role-2')
        replacement = self.policy(id='permission-2', max_total=1, policy_version='2')
        with self.assertRaises(ValueError):
            self.reserve('role-2', replacement, prepared)
        self.clock += timedelta(days=1)
        replacement = self.policy(id='permission-3', expires_at='2030-01-04T00:00:00Z', max_total=1)
        with self.assertRaises(ValueError):
            self.reserve('role-2', replacement, prepared)

    def test_daily_cap_utc_and_not_double_counting(self):
        self.import_one()
        policy = self.policy(max_daily=1, expires_at='2030-01-04T00:00:00Z', company_ids=['company-1', 'company-2'])
        first = self.reserve(policy=policy)
        self.run_cmd('outcome', {**first_without_status(first), 'status': 'sent', 'receipt': 'synthetic receipt'})
        self.import_one(oid='role-2', sid='2', company_id='company-2')
        prepared = self.reviewed('role-2')
        with self.assertRaises(ValueError):
            self.reserve('role-2', policy, prepared)
        self.clock += timedelta(days=1)
        self.reserve('role-2', policy, prepared)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 2)

    def test_company_dedup_across_waves_channels_and_changed_text(self):
        self.import_one()
        first = self.reserve()
        self.run_cmd('outcome', {**first_without_status(first), 'status': 'sent', 'receipt': 'HH receipt'})
        self.import_one(oid='unsolicited', source='company_proposal', sid='company-1:proposal',
                        content={'kind': 'company_proposal', 'need': 'hypothesis, not vacancy'})
        self.artifact.write_text('A different letter to the director')
        p = self.reviewed('unsolicited', channel='email', recipient='director@example.invalid')
        with self.assertRaisesRegex(ValueError, 'company already contacted'):
            self.reserve('unsolicited', prepared=p)
        self.assertEqual(self.run_cmd('queue')['queue'], [])
        self.assertEqual(self.run_cmd('status')['opportunities'][1]['state'], 'company_contacted')
        self.ledger.close()
        self.ledger = M.Ledger(self.path, now=lambda: self.clock)
        with self.assertRaisesRegex(ValueError, 'company already contacted'):
            self.reserve('unsolicited', prepared=p)

    def test_concurrent_different_opportunities_same_company(self):
        self.import_one()
        self.import_one(oid='role-2', sid='2', source='ats')
        policy = self.policy()
        requests = []
        for oid in ('role-1', 'role-2'):
            p = self.reviewed(oid, channel='email', recipient=oid + '@example.invalid')
            requests.append({'opportunity_id': oid, **policy, 'payload_digest': p['payload_digest']})
        self.assertEqual(sorted(self.concurrent_reserve(requests)), ['denied', 'reserved'])

    def test_unknown_and_rejected_keep_company_lock(self):
        self.import_one()
        first = self.reserve()
        self.import_one(oid='role-2', sid='2')
        p = self.reviewed('role-2', channel='email')
        for state in ('unknown', 'rejected'):
            self.run_cmd('outcome', {**first_without_status(first), 'status': state,
                                     'external_evidence': 'synthetic provider evidence'})
            with self.assertRaisesRegex(ValueError, 'company already contacted'):
                self.reserve('role-2', prepared=p)

    def test_cc_changes_invalidate_payload_and_card_is_required_for_proposal(self):
        self.import_one()
        self.artifact.write_text('{"to":["director@example.invalid"],"cc":[],"body":"proposal"}')
        p = self.reviewed(channel='email')
        self.artifact.write_text('{"to":["director@example.invalid"],"cc":["hr@example.invalid"],"body":"proposal"}')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.reserve(prepared=p)
        self.import_one(oid='proposal', source='company_proposal', sid='proposal')
        self.ledger.db.execute('DELETE FROM company_cards')
        p = self.reviewed('proposal', channel='email')
        with self.assertRaisesRegex(ValueError, 'requires a current company card'):
            self.reserve('proposal', prepared=p)

    def test_every_source_needs_card_and_no_contact_blocks_future_company_op(self):
        for source in ('hh', 'ats', 'email'):
            self.import_one(oid=source, source=source, sid=source)
            self.ledger.db.execute('DELETE FROM company_cards')
            p = self.reviewed(source, channel='email')
            with self.assertRaisesRegex(ValueError, 'requires a current company card'):
                self.reserve(source, prepared=p)
        self.install_card('company-1')
        self.run_cmd('block', {'opportunity_id': 'hh', 'reason': 'no_contact',
                               'external_evidence': 'company says do not contact'})
        self.import_one(oid='future', sid='future')
        with self.assertRaises(ValueError):
            self.prepare('future', channel='email')
        self.assertEqual(self.run_cmd('queue')['queue'], [])

    def test_company_card_change_and_expiry_block_reservation(self):
        self.import_one()
        p = self.reviewed(channel='email')
        card = TABLE.get_card(self.ledger.db, 'company-1')
        body = dict(card['body'], role='changed synthetic role', expected_version=card['version'])
        TABLE.import_cards(self.ledger.db, {'companies': [body]}, self.clock)
        with self.assertRaisesRegex(ValueError, 'company card changed'):
            self.reserve(prepared=p)
        p = self.reviewed(channel='email')
        self.clock = datetime(2030, 1, 5, tzinfo=timezone.utc)
        policy = self.policy(id='late-policy', expires_at='2030-01-10T00:00:00Z')
        with self.assertRaisesRegex(ValueError, 'company checks'):
            self.reserve(prepared=p, policy=policy)

    def test_crash_pending_unknown_and_receipt(self):
        self.import_one()
        reserved = self.reserve()
        self.ledger.close()
        self.ledger = M.Ledger(self.path, now=lambda: self.clock)
        with self.assertRaises(ValueError):
            self.prepare()
        rid = first_without_status(reserved)
        self.run_cmd('outcome', {**rid, 'status': 'unknown'})
        self.assertEqual(self.run_cmd('queue')['queue'], [])
        with self.assertRaises(ValueError):
            self.reserve()
        with self.assertRaises(ValueError):
            self.run_cmd('outcome', {**rid, 'status': 'sent'})
        self.run_cmd('outcome', {**rid, 'status': 'sent', 'receipt': 'provider receipt reference'})
        with self.assertRaises(ValueError):
            self.run_cmd('outcome', {**rid, 'status': 'not_sent', 'external_evidence': 'contradiction'})

    def test_explicit_not_sent_requires_evidence_and_new_review(self):
        self.import_one()
        rid = first_without_status(self.reserve())
        with self.assertRaises(ValueError):
            self.run_cmd('outcome', {**rid, 'status': 'not_sent'})
        self.run_cmd('outcome', {**rid, 'status': 'not_sent', 'external_evidence': 'provider confirms no submission'})
        policy = self.policy()
        prepared = self.prepare()
        with self.assertRaises(ValueError):
            self.reserve(policy=policy, prepared=prepared)
        self.reserve(policy=policy)

    def test_refusal_blocks_all_channels_even_after_reimport(self):
        self.import_one()
        rid = first_without_status(self.reserve())
        self.run_cmd('outcome', {**rid, 'status': 'sent', 'receipt': 'receipt'})
        self.run_cmd('outcome', {**rid, 'status': 'rejected', 'external_evidence': 'explicit refusal'})
        self.import_one(source='email', sid='copy')
        with self.assertRaises(ValueError):
            self.prepare(channel='email')
        self.assertEqual(self.run_cmd('queue')['queue'], [])
        self.assertEqual(self.ledger.db.execute('SELECT receipt FROM attempts').fetchone()[0], 'receipt')
        self.import_one(oid='role-2', sid='2')
        self.run_cmd('block', {'opportunity_id': 'role-2', 'reason': 'no_contact',
                               'external_evidence': 'explicit prohibition'})
        with self.assertRaises(ValueError):
            self.prepare('role-2')

    def test_invalid_json_policy_and_atomic_import(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{'):
            file = self.root / 'bad.json'
            file.write_text(text)
            with self.assertRaises(ValueError):
                M.read_json(file)
        for update in ({'max_total': True}, {'max_daily': 1.2}, {'expires_at': '2030-02-30T00:00:00Z'},
                       {'expires_at': '2030-01-02T03:00:00+03:00'}, {'opportunity_ids': [], 'company_ids': []}):
            with self.assertRaises(ValueError):
                self.policy(**update)
        bad = self.vacancy(sid='bad')
        del bad['identity_evidence']
        with self.assertRaises(ValueError):
            self.run_cmd('import', {'vacancies': [self.vacancy(), bad]})
        self.assertEqual(self.run_cmd('queue')['queue'], [])

    def concurrent_reserve(self, requests):
        barrier = Barrier(len(requests))

        def writer(data):
            ledger = M.Ledger(self.path, now=lambda: self.clock)
            try:
                barrier.wait(timeout=5)
                try:
                    ledger.run('reserve', data)
                    return 'reserved'
                except ValueError:
                    return 'denied'
            finally:
                ledger.close()

        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            return list(pool.map(writer, requests))

    def test_concurrent_writers_same_opportunity(self):
        self.import_one()
        policy = self.policy()
        prepared = self.reviewed()
        request = {'opportunity_id': 'role-1', **policy, 'payload_digest': prepared['payload_digest']}
        self.assertCountEqual(self.concurrent_reserve([request, request]), ['reserved', 'denied'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_concurrent_writers_global_cap(self):
        self.import_one()
        self.import_one(oid='role-2', sid='2')
        policy = self.policy(max_total=1)
        requests = [{'opportunity_id': oid, **policy, 'payload_digest': self.reviewed(oid)['payload_digest']}
                    for oid in ('role-1', 'role-2')]
        self.assertCountEqual(self.concurrent_reserve(requests), ['reserved', 'denied'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_company_block_applies_to_current_future_and_other_channel(self):
        self.import_one()
        policy = self.policy()
        prepared = self.reviewed()
        self.run_cmd('block-company', {'company_id': 'company-1', 'external_evidence': 'company-wide prohibition'})
        with self.assertRaises(ValueError):
            self.reserve(policy=policy, prepared=prepared)
        self.import_one(oid='future-role', sid='future', source='ats')
        with self.assertRaises(ValueError):
            self.prepare('future-role', channel='email')
        self.assertEqual(self.run_cmd('queue')['queue'], [])
        self.assertEqual({row['state'] for row in self.run_cmd('status')['opportunities']}, {'blocked'})
        # A company prohibition can also precede any known vacancy.
        self.run_cmd('block-company', {'company_id': 'future-company', 'external_evidence': 'external prohibition'})
        self.assertEqual(len(self.run_cmd('status')['blocked_companies']), 2)

    def test_policy_revocation_is_persistent_and_preserves_pending(self):
        self.import_one()
        policy = self.policy()
        first = self.reserve(policy=policy)
        self.import_one(oid='role-2', sid='2')
        prepared = self.reviewed('role-2')
        self.run_cmd('revoke-policy', {'policy_id': policy['policy_id'], 'external_evidence': 'user revoked permission'})
        self.policy()  # Re-import cannot revive an existing policy ID.
        with self.assertRaises(ValueError):
            self.reserve('role-2', policy, prepared)
        status = self.run_cmd('status')
        self.assertEqual(status['revoked_policies'][0]['policy_id'], policy['policy_id'])
        self.assertEqual(status['attempts'][0]['state'], 'reserved')
        self.run_cmd('outcome', {**first_without_status(first), 'status': 'unknown'})
        with self.assertRaises(ValueError):
            self.prepare()

    def test_status_recovery_without_private_fields(self):
        self.import_one()
        self.assertEqual(self.run_cmd('status')['opportunities'][0]['state'], 'needs_preparation')
        self.prepare()
        self.assertEqual(self.run_cmd('status')['opportunities'][0]['state'], 'needs_review')
        prepared = self.reviewed(recipient='private-synthetic-recipient')
        self.assertEqual(self.run_cmd('status')['opportunities'][0]['state'], 'ready')
        reserved = self.reserve(prepared=prepared)
        for state in ('reserved', 'unknown', 'sent'):
            if state != 'reserved':
                self.run_cmd('outcome', {**first_without_status(reserved), 'status': state,
                                         'receipt': 'private-synthetic-receipt',
                                         'external_evidence': 'private-synthetic-evidence'})
            status = self.run_cmd('status')
            self.assertEqual(status['opportunities'][0]['state'], state)
            attempt = status['attempts'][0]
            self.assertEqual(attempt['reservation_id'], reserved['reservation_id'])
            self.assertEqual(attempt['reserved_at'], '2030-01-01T00:00:00Z')
            self.assertEqual(attempt['payload_digest'], prepared['payload_digest'])
            self.assertNotIn('private-synthetic', json.dumps(status))
            self.assertNotIn(str(self.artifact), json.dumps(status))

    def test_cli_compact_output_and_private_error(self):
        script = str(Path(__file__).parents[1] / 'scripts/job_ledger.py')
        file = self.root / 'input.json'
        file.write_text(json.dumps({'vacancies': [self.vacancy()]}))
        result = subprocess.run([sys.executable, script, '--db', str(self.path),
                                 'import', '--input', str(file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'changed': ['role-1']})
        self.assertEqual(result.stderr, '')
        file.write_text('{"private-synthetic-value":1,"private-synthetic-value":2}')
        result = subprocess.run([sys.executable, script, '--db', str(self.path),
                                 'import', '--input', str(file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('private-synthetic-value', result.stderr)
        self.assertIn('error', json.loads(result.stderr))


def first_without_status(result):
    return {'reservation_id': result['reservation_id']}


if __name__ == '__main__':
    unittest.main()
