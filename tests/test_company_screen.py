import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/company_screen.py"
SPEC = importlib.util.spec_from_file_location("company_screen", SCRIPT)
SCREEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCREEN)
NOW_TEXT = "2030-01-01T12:00:00Z"
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def document():
    evidence = {"source": "snapshot:synthetic/example-1", "scope": "synthetic check only",
                "checked_at": "2030-01-01T11:00:00Z", "valid_until": "2030-01-01T13:00:00Z"}
    return {"companies": [{"company_id": "synthetic-company", "checks": {
        name: {"status": "pass", "evidence": [copy.deepcopy(evidence)]} for name in SCREEN.CHECKS}}]}


def decision(data, stage="final"):
    return SCREEN.screen(data, stage, NOW)["companies"][0]["decision"]


class CompanyScreenTests(unittest.TestCase):
    def test_final_requires_all_checks_and_api_is_only_preliminary(self):
        data = document()
        self.assertEqual(decision(data), "eligible")
        for name in SCREEN.CHECKS[4:]:
            del data["companies"][0]["checks"][name]
        self.assertEqual(decision(data, "api"), "preliminary")
        self.assertEqual(decision(data), "unknown")

    def test_each_missing_check_prevents_final_eligible(self):
        for name in SCREEN.CHECKS:
            with self.subTest(check=name):
                data = document()
                del data["companies"][0]["checks"][name]
                self.assertEqual(decision(data), "unknown")

    def test_missing_evidence_never_passes(self):
        for evidence in (None, []):
            data = document()
            check = data["companies"][0]["checks"]["identity"]
            if evidence is None:
                del check["evidence"]
            else:
                check["evidence"] = evidence
            self.assertEqual(decision(data), "unknown")

    def test_exact_expiry_downgrades_every_declared_status(self):
        for status in SCREEN.STATUSES:
            with self.subTest(status=status):
                data = document()
                check = data["companies"][0]["checks"]["payroll"]
                check["status"] = status
                check["evidence"][0]["valid_until"] = NOW_TEXT
                self.assertEqual(decision(data), "unknown")

    def test_future_check_is_unknown_and_checked_boundary_is_valid(self):
        data = document()
        evidence = data["companies"][0]["checks"]["identity"]["evidence"][0]
        evidence["checked_at"] = "2030-01-01T12:00:01Z"
        self.assertEqual(decision(data), "unknown")
        evidence["checked_at"] = NOW_TEXT
        self.assertEqual(decision(data), "eligible")

    def test_one_stale_reference_prevents_passing(self):
        data = document()
        refs = data["companies"][0]["checks"]["registry"]["evidence"]
        refs.append(dict(refs[0], valid_until=NOW_TEXT))
        self.assertEqual(decision(data), "unknown")

    def test_fail_is_not_averaged_by_passes_or_unknowns(self):
        data = document()
        checks = data["companies"][0]["checks"]
        checks["payroll"]["status"] = "fail"
        checks["bankruptcy"]["status"] = "review"
        del checks["site"]
        self.assertEqual(decision(data), "exclude")

    def test_current_review_has_priority_over_unknown(self):
        data = document()
        data["companies"][0]["checks"]["registry"]["status"] = "review"
        del data["companies"][0]["checks"]["identity"]
        self.assertEqual(decision(data), "review")

    def test_duplicate_company_ids_are_rejected(self):
        data = document()
        data["companies"].append(copy.deepcopy(data["companies"][0]))
        with self.assertRaises(ValueError):
            decision(data)

    def test_invalid_formats_are_rejected(self):
        mutations = [lambda d: d.update(extra=True),
                     lambda d: d["companies"][0].update(company_id=" "),
                     lambda d: d["companies"][0]["checks"]["site"].update(status="safe"),
                     lambda d: d["companies"][0]["checks"]["site"].update(raw_checko={}),
                     lambda d: d["companies"][0]["checks"]["site"]["evidence"][0].update(source=""),
                     lambda d: d["companies"][0]["checks"]["site"]["evidence"][0].update(scope=""),
                     lambda d: d["companies"][0]["checks"]["site"]["evidence"][0].update(
                         checked_at="2030-01-01T11:00:00+00:00"),
                     lambda d: d["companies"][0]["checks"]["site"]["evidence"][0].update(
                         checked_at="2030-02-30T11:00:00Z"),
                     lambda d: d["companies"][0]["checks"]["site"]["evidence"][0].update(
                         checked_at="2030-01-01T13:00:00Z")]
        for mutate in mutations:
            data = document()
            mutate(data)
            with self.assertRaises(ValueError):
                decision(data)

    def test_context_is_immutable(self):
        data = document()
        original = copy.deepcopy(data)
        first = SCREEN.screen(data, "final", NOW)
        first["companies"][0]["checks"]["site"]["status"] = "fail"
        self.assertEqual(data, original)
        self.assertEqual(decision(data), "eligible")

    def test_duplicate_json_keys_and_non_finite_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            for raw in ('{"companies":[],"companies":[]}', '{"companies":[NaN]}',
                        '{"companies":[Infinity]}', '{"companies":[-Infinity]}'):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    SCREEN.load_document(path)

    def run_cli(self, input_path, output_path):
        return subprocess.run([sys.executable, str(SCRIPT), "--input", str(input_path),
                               "--output", str(output_path), "--stage", "final", "--now", NOW_TEXT],
                              capture_output=True, text=True)

    def test_cli_private_output_counts_and_no_input_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            input_path, output_path = Path(folder) / "in.json", Path(folder) / "out.json"
            raw = json.dumps(document()).encode()
            input_path.write_bytes(raw)
            result = self.run_cli(input_path, output_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"companies": 1, "counts": {"eligible": 1}})
            self.assertNotIn("synthetic-company", result.stdout + result.stderr)
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
            self.assertEqual(input_path.read_bytes(), raw)
            self.assertEqual(json.loads(output_path.read_text())["stage"], "final")

    def test_cli_refuses_existing_output_and_symlink_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            input_path, output_path = Path(folder) / "in.json", Path(folder) / "out.json"
            input_path.write_text(json.dumps(document()))
            output_path.write_text("keep")
            self.assertEqual(self.run_cli(input_path, output_path).returncode, 2)
            self.assertEqual(output_path.read_text(), "keep")
            output_path.unlink()
            target = Path(folder) / "absent.json"
            output_path.symlink_to(target)
            self.assertEqual(self.run_cli(input_path, output_path).returncode, 2)
            self.assertFalse(target.exists())

    def test_cli_invalid_input_creates_no_report_and_leaks_no_content(self):
        with tempfile.TemporaryDirectory() as folder:
            input_path, output_path = Path(folder) / "in.json", Path(folder) / "out.json"
            input_path.write_text('{"private-marker": NaN}')
            result = self.run_cli(input_path, output_path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("private-marker", result.stderr)
            self.assertFalse(output_path.exists())

    def test_public_repo_paths_are_rejected_and_runs_allowed(self):
        with self.assertRaises(ValueError):
            SCREEN.private_path(SCRIPT.parent / "private-input.json")
        self.assertEqual(SCREEN.private_path(SCREEN.ROOT / "runs" / "case" / "input.json"),
                         SCREEN.ROOT / "runs" / "case" / "input.json")


if __name__ == "__main__":
    unittest.main()
