#!/usr/bin/env python3
"""Offline completeness/expiry gate for declared company checks; no fact verification."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

CHECKS = ("identity", "registry", "bankruptcy", "payroll", "site", "commute", "contact")
STATUSES = {"pass", "fail", "review", "unknown"}
ROOT = Path(__file__).resolve().parent.parent


def require(condition):
    if not condition:
        raise ValueError("invalid data")


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value))
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def text(value):
    require(isinstance(value, str) and bool(value.strip()) and value == value.strip())


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys())
    require(value.keys() <= set(required) | set(optional))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON")


def load_document(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)


def validate(document):
    keys(document, ("companies",))
    require(isinstance(document["companies"], list))
    seen = set()
    for company in document["companies"]:
        keys(company, ("company_id", "checks"))
        text(company["company_id"])
        require(company["company_id"] not in seen)
        seen.add(company["company_id"])
        keys(company["checks"], (), CHECKS)
        for check in company["checks"].values():
            keys(check, ("status",), ("evidence",))
            require(isinstance(check["status"], str) and check["status"] in STATUSES)
            require(isinstance(check.get("evidence", []), list))
            for evidence in check.get("evidence", []):
                keys(evidence, ("source", "scope", "checked_at", "valid_until"))
                text(evidence["source"])
                text(evidence["scope"])
                require(timestamp(evidence["checked_at"]) < timestamp(evidence["valid_until"]))


def screen(document, stage, now):
    require(stage in ("api", "final"))
    require(isinstance(now, datetime) and now.tzinfo == timezone.utc)
    validate(document)
    required = CHECKS[:4] if stage == "api" else CHECKS
    results = []
    for company in document["companies"]:
        checks = {}
        for name in required:
            check = company["checks"].get(name)
            status, reason = "unknown", "missing_check"
            if check is not None:
                status, reason = check["status"], "declared"
                evidence = check.get("evidence", [])
                if not evidence:
                    status, reason = "unknown", "missing_evidence"
                elif not all(timestamp(item["checked_at"]) <= now < timestamp(item["valid_until"])
                             for item in evidence):
                    status, reason = "unknown", "not_current"
            checks[name] = {"status": status, "reason": reason}
        statuses = {check["status"] for check in checks.values()}
        decision = ("exclude" if "fail" in statuses else "review" if "review" in statuses
                    else "unknown" if "unknown" in statuses
                    else "preliminary" if stage == "api" else "eligible")
        results.append({"company_id": company["company_id"], "decision": decision, "checks": checks})
    return {"stage": stage, "evaluated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "companies": results}


def private_path(path):
    resolved = path.resolve()
    require(not resolved.is_relative_to(ROOT) or resolved.is_relative_to(ROOT / "runs"))
    return resolved


def save_private(path, result):
    # Exclusive creation prevents overwriting input, an existing report or a symlink.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("api", "final"), required=True)
    parser.add_argument("--now", help="UTC YYYY-MM-DDTHH:MM:SSZ; default: current UTC")
    args = parser.parse_args(argv)
    try:
        input_path = private_path(args.input)
        # Preserve the final path component so O_EXCL also rejects dangling symlinks.
        private_path(args.output)
        now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
        result = screen(load_document(input_path), args.stage, now)
        save_private(args.output, result)
        print(json.dumps({"companies": len(result["companies"]),
                          "counts": dict(Counter(item["decision"] for item in result["companies"]))}))
        return 0
    except (ValueError, TypeError, OSError, RecursionError, OverflowError):
        print("company screening failed: invalid input or unavailable private storage", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
