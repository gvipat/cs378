#!/usr/bin/env python3
"""Mail every student their own LLM proxy key, one message each.

    ./litellm/mail_keys.py llm-tokens.csv                                   # dry run
    ./litellm/mail_keys.py llm-tokens.csv --to you@utexas.edu --limit 1 --send --from you@utexas.edu
    ./litellm/mail_keys.py llm-tokens.csv --send --from you@utexas.edu

llm-tokens.csv is what `keys.py mint` writes: student_id, name, group_id, key.
Same rules as mail_tokens.py, whose addressing and sent log this reuses: one
message per student holding only that student's key, nothing is sent without
--send, and the dry run hides the keys. A key spends its group's budget, so it
is a credential like a gpu-lease token.

To reissue one student's key, mint it from a one-row roster with --rotate and
mail that file with --resend:

    (head -1 new-roster.csv; grep '^eid,' new-roster.csv) > one.csv
    ./litellm/keys.py mint one.csv --rotate --out one-key.csv
    ./litellm/mail_keys.py one-key.csv --resend --send --from you@utexas.edu
"""

import argparse
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mail_tokens import (  # noqa: E402
    DEFAULT_DOMAIN, address, already_sent, die, read_csv, record, wrong_domain_warning)
from keys import read_roster  # noqa: E402  (accepts Canvas's column names too)

DEFAULT_PAGE = "https://utcs378.github.io/fall26/assignments/assignment4"
DEFAULT_REPO = "https://github.com/utcs378/assignment4-template"

SUBJECT = "{course} assignment4 key"

BODY = """\
Hi {name},

Assignment 4 is out.

  webpage     {page_url}
  code        {repo_url}

You can use the key below to call LLMs for Part 3 by following the Model
access section of the assignment README.
This key is yours alone - do not share it with anyone, and never put it in a
notebook or a committed file. Your group's keys share a ${budget} budget, and
requests are refused once it is spent.

  key         {key}
  group       {group}

{signature}
"""


def collect(path, roster_path=None):
    """(recipients, no_key, not_on_roster), like mail_tokens.collect."""
    rows = [{
        "student_id": r["student_id"].strip(),
        "name": (r.get("name") or "").strip() or r["student_id"].strip(),
        "group_id": str(r.get("group_id") or "?").strip(),
        "email": (r.get("email") or "").strip(),
        "key": r["key"].strip(),
    } for r in read_csv(path, {"student_id", "key"})]
    if not roster_path:
        return rows, [], []
    listed = {r["student_id"].strip() for r in read_roster(roster_path)}
    have = {r["student_id"] for r in rows}
    return [r for r in rows if r["student_id"] in listed], sorted(listed - have), sorted(have - listed)


def redact(key):
    return key[:3] + "*" * 12


def build(row, args):
    msg = EmailMessage()
    msg["Subject"] = SUBJECT.format(course=args.course)
    msg["From"] = args.sender
    msg["To"] = args.to or address(row, args.domain)
    if args.reply_to:
        msg["Reply-To"] = args.reply_to
    msg.set_content(BODY.format(
        name=row["name"], key=row["key"], group=row["group_id"], budget=args.budget,
        page_url=args.page_url, repo_url=args.repo_url, signature=args.signature))
    return msg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", help="llm-tokens.csv from ./litellm/keys.py mint")
    ap.add_argument("--roster", help="roster csv: report students with no key, skip rows not on it")
    ap.add_argument("--send", action="store_true", help="actually send (default: dry run)")
    ap.add_argument("--eml-dir", help="write .eml files here instead of sending")
    ap.add_argument("--from", dest="sender", help="From: address (required with --send)")
    ap.add_argument("--reply-to")
    ap.add_argument("--course", default="CS 378")
    ap.add_argument("--budget", default="50", help="USD per group, as quoted in the mail (default 50)")
    ap.add_argument("--page-url", default=DEFAULT_PAGE, help=f"assignment webpage (default {DEFAULT_PAGE})")
    ap.add_argument("--repo-url", default=DEFAULT_REPO, help=f"template students start from (default {DEFAULT_REPO})")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN,
                    help=f"student addresses are <student_id>@DOMAIN (default {DEFAULT_DOMAIN})")
    ap.add_argument("--to", metavar="ADDRESS",
                    help="smoke test: send every message here instead; not written to the sent log")
    ap.add_argument("--signature", default="- CS 378 course staff")
    ap.add_argument("--smtp-host", default="smtp.gmail.com")
    ap.add_argument("--smtp-port", type=int, default=587)
    ap.add_argument("--smtp-user", help="default: the --from address")
    ap.add_argument("--sent-log", default="llm-keys-sent.log")
    ap.add_argument("--resend", action="store_true",
                    help="mail students already in the sent log too (after reissuing a key)")
    ap.add_argument("--only", action="append", help="just this student_id (repeatable)")
    ap.add_argument("--limit", type=int, help="stop after N messages - test with --limit 1")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between sends")
    ap.add_argument("--show-keys", action="store_true",
                    help="print keys in full; leaves every credential in your scrollback")
    args = ap.parse_args()

    recipients, no_key, extra = collect(args.keys, args.roster)
    if no_key:
        print(f"WARNING: {len(no_key)} roster student(s) have no key in {args.keys}:")
        print("  " + ", ".join(no_key))
        print("  They already had one, which cannot be looked up. Reissue with --rotate.\n")
    if extra:
        print(f"note: {len(extra)} key(s) in {args.keys} are not on the roster and will NOT be "
              f"mailed: {', '.join(extra)}\n")
    if args.only:
        recipients = [r for r in recipients if r["student_id"] in set(args.only)]
    done = set() if args.resend else already_sent(args.sent_log)
    pending = [r for r in recipients if r["student_id"] not in done]
    if args.limit:
        pending = pending[:args.limit]
    print(f"{len(recipients)} key(s) to mail, {len(done)} already sent, {len(pending)} to go\n")
    if not pending:
        return

    if args.eml_dir:
        out = Path(args.eml_dir)
        out.mkdir(parents=True, exist_ok=True)
        for row in pending:
            fd = os.open(out / f"{row['student_id']}.eml", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(build(row, args).as_bytes())
        print(f"wrote {len(pending)} .eml file(s) to {out} (0600 - they hold live keys)")
        return
    if not args.send:
        for row in pending:
            key = row["key"] if args.show_keys else redact(row["key"])
            print(f"  {address(row, args.domain):<34} group {row['group_id']:<4} {key}")
        if args.to:
            print(f"\n  (--to {args.to}: every one of these would be addressed to you)")
        elif args.domain != DEFAULT_DOMAIN:
            print("\n" + "\n".join(wrong_domain_warning(pending, args.domain)))
        print(f"\ndry run. Nothing sent. Add --send (and --from) to mail these {len(pending)} student(s).")
        return

    if not args.sender:
        die("--send needs --from")
    if args.domain != DEFAULT_DOMAIN and not args.to:
        print("\n".join(wrong_domain_warning(pending, args.domain)))
        if input(f"type the domain ({args.domain}) to send anyway: ").strip() != args.domain:
            die("nothing sent")
    user = args.smtp_user or args.sender
    password = os.environ.get("GPULEASE_SMTP_PASSWORD") or getpass(f"SMTP password for {user}: ")
    sent = 0
    with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        for row in pending:
            to = args.to or address(row, args.domain)
            try:
                s.send_message(build(row, args))
            except smtplib.SMTPException as e:
                print(f"  FAILED {to}: {e}")
                continue
            if not args.to:
                record(args.sent_log, row["student_id"])
            sent += 1
            print(f"  sent   {to}")
            time.sleep(args.delay)
    if args.to:
        print(f"\n{sent} test message(s) sent to {args.to}. Nothing logged, so the real send still covers everyone.")
        return
    print(f"\n{sent} sent, logged in {args.sent_log}. Re-running skips them.")
    print(f"Now delete {args.keys} - it is the only copy of these keys.")


if __name__ == "__main__":
    main()
