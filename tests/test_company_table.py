import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("company_table", SCRIPTS / "company_table.py")
TABLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TABLE)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def batch():
    evidence = {"source": "snapshot:synthetic/check", "scope": "synthetic only",
                "checked_at": "2030-01-01T11:00:00Z", "valid_until": "2030-01-01T13:00:00Z"}
    return {"companies": [{"company_id": "synthetic-A", "company_name": "Example", "inn": "1234567890", "wave": 2,
                           "checks": {name: {"status": "pass", "evidence": [copy.deepcopy(evidence)]}
                                      for name in TABLE.screen.CHECKS}, "expected_version": None}]}


def ledger_tables(conn):
    conn.executescript("""CREATE TABLE opportunities(id TEXT PRIMARY KEY,company TEXT,blocked TEXT);
    CREATE TABLE attempts(id TEXT PRIMARY KEY,opportunity TEXT,state TEXT,created TEXT);
    CREATE TABLE company_blocks(company TEXT PRIMARY KEY,created TEXT,evidence TEXT);""")


class CompanyTableTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.execute(TABLE.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_cas_and_idempotent_retry(self):
        data = batch()
        self.assertEqual(TABLE.import_cards(self.conn, data, NOW), 1)
        version = TABLE.get_card(self.conn, "synthetic-A")["version"]
        self.assertEqual(TABLE.import_cards(self.conn, data, NOW), 0)
        data["companies"][0]["role"] = "new role"
        with self.assertRaises(ValueError):
            TABLE.import_cards(self.conn, data, NOW)
        data["companies"][0]["expected_version"] = version
        self.assertEqual(TABLE.import_cards(self.conn, data, NOW), 1)
        with self.assertRaises(ValueError):
            data["companies"][0]["role"] = "another change"
            TABLE.import_cards(self.conn, data, NOW)

    def test_decision_changes_with_time_without_reimport(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        self.assertEqual(TABLE.decision(self.conn, "synthetic-A", NOW)["decision"], "write")
        self.assertEqual(TABLE.decision(self.conn, "synthetic-A", NOW + timedelta(hours=1))["decision"], "clarify")
        self.assertEqual(TABLE.decision(self.conn, "missing", NOW)["reason"], "missing_card")

    def test_current_fail_and_review(self):
        for number, (status, expected) in enumerate((("fail", "skip"), ("review", "clarify"), ("unknown", "clarify"))):
            data = batch()
            data["companies"][0]["company_id"] = status
            data["companies"][0]["inn"] = str(1234567890 + number)
            data["companies"][0]["checks"]["payroll"]["status"] = status
            TABLE.import_cards(self.conn, data, NOW)
            self.assertEqual(TABLE.decision(self.conn, status, NOW)["decision"], expected)

    def test_contact_history_overrides_and_survives_import(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        ledger_tables(self.conn)
        self.conn.execute("INSERT INTO opportunities VALUES('old-role','synthetic-A',NULL)")
        for status, reason in (("not_sent", "eligible_pending_review_and_authority"),
                               ("sent", "already_contacted_continuation_only"),
                               ("reserved", "contact_unresolved"), ("unknown", "contact_unresolved"),
                               ("rejected", "company_refused"), ("no_contact", "company_refused")):
            self.conn.execute("DELETE FROM attempts")
            self.conn.execute("INSERT INTO attempts VALUES('attempt','old-role',?,'2030-01-01T10:00:00Z')", (status,))
            TABLE.import_cards(self.conn, batch(), NOW)
            result = TABLE.decision(self.conn, "synthetic-A", NOW)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["decision"], "write" if status == "not_sent" else "skip")
        self.conn.execute("DELETE FROM attempts")
        self.conn.execute("INSERT INTO company_blocks VALUES('synthetic-A','2030-01-01T10:00:00Z','ref')")
        self.assertEqual(TABLE.decision(self.conn, "synthetic-A", NOW)["reason"], "company_blocked")

    def test_pre_attempt_refusal_is_also_company_wide(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        ledger_tables(self.conn)
        self.conn.execute("INSERT INTO opportunities VALUES('old-role','synthetic-A','rejected')")
        self.assertEqual(TABLE.decision(self.conn, "synthetic-A", NOW)["reason"], "company_refused")

    def test_manual_body_tampering_with_old_version_is_rejected(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        card = TABLE.get_card(self.conn, "synthetic-A")
        card["body"]["role"] = "tampered"
        self.conn.execute("UPDATE company_cards SET body=?", (json.dumps(card["body"]),))
        with self.assertRaises(ValueError):
            TABLE.decision(self.conn, "synthetic-A", NOW)

    def test_html_injection_is_escaped_everywhere(self):
        data = batch()
        data["companies"][0]["company_name"] = '<script>alert("X")</script>'
        data["companies"][0]["contact_source"] = 'javascript:alert("Y")'
        data["companies"][0]["checks"]["site"]["evidence"][0]["scope"] = '<img src=x onerror=alert(1)>'
        TABLE.import_cards(self.conn, data, NOW)
        rendered = TABLE.export_html(TABLE.rows(self.conn, NOW))
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("href=", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("<details>", rendered)
        self.assertIn("position:sticky", rendered)
        self.assertIn("overflow:auto", rendered)
        self.assertLess(rendered.index("<th>Решение</th>"), rendered.index("<th>Компания</th>"))

    def test_batch_rollback_on_stale_conflict(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        data = batch()
        data["companies"][0]["company_id"] = "new-company"
        data["companies"][0]["inn"] = "2234567890"
        stale = copy.deepcopy(batch()["companies"][0])
        stale["role"] = "stale update"
        data["companies"].append(stale)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            TABLE.import_cards(self.conn, data, NOW)
        self.conn.rollback()
        self.assertIsNone(TABLE.get_card(self.conn, "new-company"))

    def test_duplicate_batch_ids_and_invalid_metadata_are_rejected(self):
        data = batch()
        data["companies"].append(copy.deepcopy(data["companies"][0]))
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            TABLE.import_cards(self.conn, data, NOW)
        self.conn.rollback()
        for field, value in (("wave", True), ("wave", 3), ("full_resume", "private"), ("role", []),
                             ("inn", "123456789012"), ("inn", "12345"), ("inn", "１２３４５６７８９０")):
            data = batch()
            data["companies"][0][field] = value
            with self.assertRaises(ValueError):
                TABLE.import_cards(self.conn, data, NOW)

    def test_duplicate_inn_alias_is_rejected_and_batch_rolls_back(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        data = batch()
        data["companies"][0]["company_id"] = "new-good-company"
        data["companies"][0]["inn"] = "2234567890"
        alias = copy.deepcopy(batch()["companies"][0])
        alias["company_id"] = "checko-alias"
        data["companies"].append(alias)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            TABLE.import_cards(self.conn, data, NOW)
        self.conn.rollback()
        self.assertIsNone(TABLE.get_card(self.conn, "new-good-company"))
        self.assertIsNone(TABLE.get_card(self.conn, "checko-alias"))
        self.assertIsNotNone(TABLE.get_card(self.conn, "synthetic-A"))

    def test_duplicate_inn_within_new_batch_is_rejected(self):
        data = batch()
        alias = copy.deepcopy(data["companies"][0])
        alias["company_id"] = "new-alias"
        data["companies"].append(alias)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            TABLE.import_cards(self.conn, data, NOW)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_cards").fetchone()[0], 0)

    def test_established_inn_cannot_change_or_disappear(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        version = TABLE.get_card(self.conn, "synthetic-A")["version"]
        for inn in (None, "", "2234567890"):
            data = batch()
            data["companies"][0]["expected_version"] = version
            if inn is None:
                del data["companies"][0]["inn"]
            else:
                data["companies"][0]["inn"] = inn
            with self.assertRaises(ValueError):
                TABLE.import_cards(self.conn, data, NOW)
            self.assertEqual(TABLE.get_card(self.conn, "synthetic-A")["body"]["inn"], "1234567890")

    def test_missing_inn_cannot_write_but_contact_block_still_applies(self):
        data = batch()
        del data["companies"][0]["inn"]
        TABLE.import_cards(self.conn, data, NOW)
        result = TABLE.decision(self.conn, "synthetic-A", NOW)
        self.assertEqual((result["decision"], result["reason"]), ("clarify", "identity_unknown"))
        ledger_tables(self.conn)
        self.conn.execute("INSERT INTO opportunities VALUES('old-role','synthetic-A',NULL)")
        self.conn.execute("INSERT INTO attempts VALUES('attempt','old-role','sent','2030-01-01T10:00:00Z')")
        result = TABLE.decision(self.conn, "synthetic-A", NOW)
        self.assertEqual((result["decision"], result["reason"]), ("skip", "already_contacted_continuation_only"))

    def test_decision_works_inside_existing_transaction_without_writes(self):
        TABLE.import_cards(self.conn, batch(), NOW)
        self.conn.execute("BEGIN IMMEDIATE")
        before = self.conn.total_changes
        self.assertEqual(TABLE.decision(self.conn, "synthetic-A", NOW)["decision"], "write")
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.total_changes, before)
        self.conn.rollback()

    def test_missing_inn_does_not_downgrade_current_fail(self):
        data = batch()
        del data["companies"][0]["inn"]
        data["companies"][0]["checks"]["payroll"]["status"] = "fail"
        TABLE.import_cards(self.conn, data, NOW)
        result = TABLE.decision(self.conn, "synthetic-A", NOW)
        self.assertEqual((result["decision"], result["reason"]), ("skip", "current_fail"))

    def test_cli_persistence_permissions_and_private_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            input_path, database, output = folder / "cards.json", folder / "shared.sqlite", folder / "table.html"
            input_path.write_text(json.dumps(batch()))
            prefix = [sys.executable, str(SCRIPTS / "company_table.py"), "--db", str(database)]
            imported = subprocess.run(prefix + ["import", "--input", str(input_path), "--now", "2030-01-01T12:00:00Z"],
                                      capture_output=True, text=True)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(json.loads(imported.stdout), {"changed": 1})
            exported = subprocess.run(prefix + ["export", "--output", str(output), "--now", "2030-01-01T12:00:00Z"],
                                      capture_output=True, text=True)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertNotIn("synthetic-A", exported.stdout + exported.stderr)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            reopened = TABLE.connect_private(database)
            try:
                self.assertEqual(TABLE.decision(reopened, "synthetic-A", NOW)["decision"], "write")
                self.assertEqual(TABLE.decision(reopened, "synthetic-A", NOW + timedelta(hours=1))["decision"], "clarify")
            finally:
                reopened.close()

    def test_database_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target, alias = Path(directory) / "target.sqlite", Path(directory) / "alias.sqlite"
            target.write_text("untouched")
            alias.symlink_to(target)
            with self.assertRaises(OSError):
                TABLE.connect_private(alias)
            self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
