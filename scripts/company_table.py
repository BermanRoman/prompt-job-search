#!/usr/bin/env python3
"""Persistent private company table, with fresh offline decisions and ledger overlays."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

try:
    from scripts import company_screen as screen
except ModuleNotFoundError:
    import company_screen as screen

METADATA = ("company_name", "inn", "city", "website", "workplace", "role", "need_hypothesis",
            "recipient", "contact_source", "vacancy_url")
SCHEMA = """CREATE TABLE IF NOT EXISTS company_cards (
company_id TEXT PRIMARY KEY, body TEXT NOT NULL, version TEXT NOT NULL, updated_at TEXT NOT NULL)"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(body):
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def validate_body(body):
    screen.keys(body, ("company_id", "checks", "wave"), METADATA)
    screen.require(type(body["wave"]) is int and body["wave"] in (1, 2))
    for name in METADATA:
        if name in body:
            screen.require(isinstance(body[name], str))
    screen.require(not body.get("inn") or re.fullmatch("[0-9]{10}", body["inn"]))
    screen.validate({"companies": [{"company_id": body["company_id"], "checks": body["checks"]}]})


def decode_record(row):
    if row is None:
        return None
    body = json.loads(row[1], object_pairs_hook=screen.unique_object, parse_constant=screen.reject_constant)
    validate_body(body)
    screen.require(body["company_id"] == row[0] and digest(body) == row[2])
    screen.timestamp(row[3])
    return {"company_id": row[0], "body": body, "version": row[2], "updated_at": row[3]}


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def get_card(conn, company_id):
    if not table_exists(conn, "company_cards"):
        return None
    return decode_record(conn.execute("SELECT company_id,body,version,updated_at FROM company_cards "
                                      "WHERE company_id=?", (company_id,)).fetchone())


def decision(conn, company_id, now):
    """Read-only; caller may hold BEGIN IMMEDIATE for atomic reservation checks."""
    card = get_card(conn, company_id)
    result = {"company_id": company_id, "decision": "clarify", "reason": "missing_card",
              "last_contact": None, "screening": None}
    if card is not None:
        body = card["body"]
        result["screening"] = screen.screen({"companies": [
            {"company_id": company_id, "checks": body["checks"]}]}, "final", now)["companies"][0]
        status = result["screening"]["decision"]
        result.update(decision={"eligible": "write", "exclude": "skip"}.get(status, "clarify"),
                      reason={"eligible": "eligible_pending_review_and_authority", "exclude": "current_fail"}.get(status, status))
        if not body.get("inn") and result["decision"] != "skip":
            result.update(decision="clarify", reason="identity_unknown")
    if table_exists(conn, "company_blocks") and conn.execute(
            "SELECT 1 FROM company_blocks WHERE company=?", (company_id,)).fetchone():
        result.update(decision="skip", reason="company_blocked")
    attempts = []
    if table_exists(conn, "attempts") and table_exists(conn, "opportunities"):
        attempts = conn.execute("SELECT a.state,a.created FROM attempts a JOIN opportunities o "
                                "ON o.id=a.opportunity WHERE o.company=? AND a.state!='not_sent' "
                                "ORDER BY a.created DESC", (company_id,)).fetchall()
    if attempts:
        result["last_contact"] = {"state": attempts[0][0], "at": attempts[0][1]}
        states = {row[0] for row in attempts}
        if result["reason"] != "company_blocked":
            reason = ("company_refused" if states & {"rejected", "no_contact"} else
                      "contact_unresolved" if states & {"reserved", "unknown"} else "already_contacted_continuation_only")
            result.update(decision="skip", reason=reason)
    if table_exists(conn, "opportunities") and conn.execute(
            "SELECT 1 FROM opportunities WHERE company=? AND blocked IS NOT NULL", (company_id,)).fetchone():
        result.update(decision="skip", reason="company_refused")
    return result


def import_cards(conn, document, now):
    screen.keys(document, ("companies",))
    screen.require(isinstance(document["companies"], list))
    seen, changes, inn_owners = set(), [], {}
    for row in conn.execute("SELECT company_id,body,version,updated_at FROM company_cards"):
        existing = decode_record(row)["body"]
        if existing.get("inn"):
            screen.require(existing["inn"] not in inn_owners)
            inn_owners[existing["inn"]] = existing["company_id"]
    for item in document["companies"]:
        screen.keys(item, ("company_id", "checks", "wave", "expected_version"), METADATA)
        body = {key: value for key, value in item.items() if key != "expected_version"}
        validate_body(body)
        company_id = body["company_id"]
        screen.require(company_id not in seen)
        seen.add(company_id)
        expected = item["expected_version"]
        screen.require(expected is None or isinstance(expected, str) and re.fullmatch("[0-9a-f]{64}", expected))
        current, version = get_card(conn, company_id), digest(body)
        inn = body.get("inn")
        if current and current["body"].get("inn"):
            screen.require(inn == current["body"]["inn"])
        if inn:
            screen.require(inn_owners.get(inn, company_id) == company_id)
            inn_owners[inn] = company_id
        if current and current["version"] == version:
            continue
        screen.require(expected == (current["version"] if current else None))
        conn.execute("INSERT INTO company_cards(company_id,body,version,updated_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(company_id) DO UPDATE SET body=excluded.body,version=excluded.version,updated_at=excluded.updated_at",
                     (company_id, canonical(body), version, now.strftime("%Y-%m-%dT%H:%M:%SZ")))
        changes.append(company_id)
    return len(changes)


def rows(conn, now, company_id=None):
    ids = ([company_id] if company_id is not None else
           [row[0] for row in conn.execute("SELECT company_id FROM company_cards ORDER BY company_id")])
    return [{"card": get_card(conn, key), "evaluation": decision(conn, key, now)} for key in ids]


def export_html(data):
    columns = (("decision", "Решение"), ("reason", "Причина"), ("company_name", "Компания"),
               ("inn", "ИНН"), ("company_id", "ID компании"), ("wave", "Волна"), ("city", "Город"),
               ("website", "Сайт"), ("workplace", "Место работы"), ("role", "Роль"),
               ("need_hypothesis", "Гипотеза потребности"), ("recipient", "Адресат"),
               ("contact_source", "Источник контакта"), ("vacancy_url", "Вакансия"),
               ("last_contact", "Последняя попытка"), ("identity", "Идентификация"),
               ("registry", "Реестр"), ("bankruptcy", "Банкротство"), ("payroll", "Трудовые риски"),
               ("site", "Сайт проверен"), ("commute", "Дорога"), ("contact", "Контакт проверен"),
               ("updated_at", "Обновлено UTC"), ("sources_dates", "Источники и сроки"))
    reasons = {"eligible_pending_review_and_authority": "Нужны содержательное ревью и текущее поручение",
               "current_fail": "Есть подтверждённый стоп-фактор", "review": "Нужен содержательный разбор",
               "unknown": "Недостаточно актуальных доказательств", "identity_unknown": "Не установлен ИНН юридического лица",
               "company_blocked": "Контакт с компанией запрещён", "company_refused": "Компания отказала или запретила контакт",
               "contact_unresolved": "Не выяснен результат прежней попытки",
               "already_contacted_continuation_only": "Первичная попытка уже была, допустима только разрешённая переписка"}
    statuses = {"pass": "Пройдено", "fail": "Стоп-фактор", "review": "Нужен разбор", "unknown": "Неизвестно"}
    pieces = ['<!doctype html><html lang="ru"><meta charset="utf-8"><title>Компании</title>',
              '<style>body{font:15px/1.5 system-ui,sans-serif;margin:24px;color:#182230;background:#f8fafc}'
              'h1{margin-bottom:8px}.scroll{overflow:auto;max-height:80vh;border:1px solid #cbd5e1;border-radius:8px}'
              'table{border-collapse:separate;border-spacing:0;background:white;min-width:2400px}'
              'th,td{text-align:left;vertical-align:top;padding:12px;border-right:1px solid #e2e8f0;'
              'border-bottom:1px solid #e2e8f0;min-width:130px;max-width:280px;overflow-wrap:anywhere}'
              'th{position:sticky;top:0;background:#e2e8f0;z-index:1}td:first-child{font-weight:700}'
              'td:nth-child(2){min-width:230px}tr:nth-child(even){background:#f8fafc}'
              'pre{font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;min-width:280px}'
              'summary{cursor:pointer;font-weight:600}</style>',
              '<h1>Компании</h1><p>ПИШЕМ означает только формальную готовность: нужны проверка и поручение.</p>',
              '<div class="scroll"><table><thead><tr>']
    pieces.extend("<th>" + html.escape(label) + "</th>" for _, label in columns)
    pieces.append("</tr></thead><tbody>")
    for row in data:
        card, evaluation = row["card"], row["evaluation"]
        cells = dict(card["body"])
        checks = evaluation["screening"]["checks"]
        cells.update({name: statuses[checks[name]["status"]] for name in screen.CHECKS})
        cells.update(decision={"write": "ПИШЕМ", "skip": "НЕ ПИШЕМ", "clarify": "НУЖНО УТОЧНИТЬ"}[evaluation["decision"]],
                     reason=reasons.get(evaluation["reason"], evaluation["reason"]),
                     last_contact=evaluation["last_contact"], updated_at=card["updated_at"],
                     sources_dates=card["body"]["checks"])
        rendered = []
        for key, _ in columns:
            value = cells.get(key, "—")
            if key == "sources_dates":
                value = ('<details><summary>Доказательства и сроки</summary><pre>' +
                         html.escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre></details>')
            else:
                value = html.escape(value if isinstance(value, str) else canonical(value))
            rendered.append("<td>" + value + "</td>")
        pieces.append("<tr>" + "".join(rendered) + "</tr>")
    pieces.append("</tbody></table></div></html>")
    return "\n".join(pieces)


def connect_private(path):
    screen.private_path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        screen.require(stat.S_ISREG(os.fstat(fd).st_mode))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute(SCHEMA)
    return conn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("command", choices=("import", "status", "read", "export"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--company-id")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    conn = None
    try:
        now = screen.timestamp(args.now) if args.now else datetime.now(timezone.utc).replace(microsecond=0)
        screen.require(args.command != "import" or args.input is not None)
        screen.require(args.command not in ("read", "export") or args.output is not None)
        screen.require(args.command != "read" or args.company_id is not None)
        data = screen.load_document(screen.private_path(args.input)) if args.command == "import" else None
        if args.output:
            screen.private_path(args.output)
        conn = connect_private(args.db)
        conn.execute("BEGIN IMMEDIATE" if args.command == "import" else "BEGIN")
        if args.command == "import":
            result = {"changed": import_cards(conn, data, now)}
        else:
            data = rows(conn, now, args.company_id if args.command == "read" else None)
            screen.require(all(row["card"] is not None for row in data))
            result = {"companies": len(data), "counts": {name: sum(row["evaluation"]["decision"] == name for row in data)
                                                       for name in ("write", "skip", "clarify")}}
            if args.output:
                if args.command == "export":
                    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(export_html(data))
                else:
                    screen.save_private(args.output, {"evaluated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": data})
        conn.commit()
        print(canonical(result))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error, RecursionError, OverflowError):
        if conn is not None:
            conn.rollback()
        print("company table failed: invalid input, version conflict or private storage failure", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
