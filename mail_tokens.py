#!/usr/bin/env python3
"""Mail every student on the roster their own course token, one message each.

    ./mail_tokens.py tokens.csv                     # dry run
    ./mail_tokens.py tokens.csv --eml-dir /tmp/out  # read one before sending
    ./mail_tokens.py tokens.csv --send --from you@utexas.edu

tokens.csv carries the EID, name, group and token, so it is the only input
needed. Pass --roster roster.csv too and the roster becomes the authority on
who gets mail: anyone on it with no token is reported rather than silently
missed (they already had one, and only the hash is stored, so it cannot be
looked up), and any token row that is NOT on it - a leftover test account, a
student who dropped - is skipped instead of mailed a live credential.

`tokens.csv` is every listed student's credential in plaintext, and a token can
stop another group's session and destroy their disks. So: one message per
student, containing only that student's token, never the file itself and never
a CC or BCC of the class.

Nothing is sent unless you pass --send. The default is a dry run that prints the
addresses and redacts the tokens, because a terminal scrollback holding fifty
credentials is the same problem as the file.

Stdlib only, like the rest of the student-facing code.
"""

import argparse
import csv
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from email.message import EmailMessage
from getpass import getpass
from pathlib import Path

DEFAULT_API = "https://utcs378-infra.duckdns.org"
# The roster is UT EIDs, and this is where an EID's mail goes. An `email`
# column in the roster wins over it, so a roster carrying real addresses needs
# no flag.
DEFAULT_DOMAIN = "my.utexas.edu"

SUBJECT = "Your {course} GPU token ({assignment})"

BODY = """\
Hi {name},

Here is your personal token for the {course} GPU lease service. It is yours
alone - do not share it or paste it into a group chat. Anyone who has it can
start and stop your group's nodes and destroy what is on them.

  token    {token}
  group    {group}

Getting started - you need Python 3.9+.

  1. Get gpulease.py: {cli_url}
  2. python3 gpulease.py login {token}
  3. python3 gpulease.py start     # a few minutes, then an ssh line per node
     python3 gpulease.py status    # check on it any time
     python3 gpulease.py stop      # when you are done - idle nodes still bill

  On Windows, type `py` everywhere this says `python3` - the python.org
  installer does not give you a `python3` command.

{count} Warnings:

  * YOUR NODES ARE TEMPORARY. `stop`, the end of your lease and the
    assignment deadline all destroy them AND their disks. Nothing is backed
    up and nothing carries over. Work in git and push before you stop.

  * Your group shares {quota} GPU-hours for {assignment}, counted per
    node per hour. When they are gone, that is the end of the assignment
    for your group. Stopping when nobody is using the machine is how you
    make them last.{node_note}
{starts_note}
  * Everything ends at the deadline:
    {deadline}
    After it, nothing starts and anything still running is destroyed.

`stop` ends the session for your whole group, not just for you, so tell them
before you run it.

{signature}
"""


def die(msg):
    sys.exit(f"error: {msg}")


def read_csv(path, required):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        die(f"{path} has no rows")
    missing = required - set(rows[0])
    if missing:
        die(f"{path} is missing columns: {sorted(missing)}")
    return rows


def collect(tokens_path, roster_path=None):
    """(recipients, no_token, not_on_roster) from tokens.csv, roster optional.

    tokens.csv already carries everything a message needs. The roster only ever
    subtracts: it names who is supposed to be here, so it can report a student
    the mint skipped and withhold a row that is not a student at all.
    """
    tokens = read_csv(tokens_path, {"student_id", "token"})
    rows = [{
        "student_id": r["student_id"].strip(),
        "name": (r.get("name") or "").strip() or r["student_id"].strip(),
        "group_id": str(r.get("group_id") or "?").strip(),
        "email": (r.get("email") or "").strip(),
        "token": r["token"].strip(),
    } for r in tokens]

    if not roster_path:
        return rows, [], []

    roster = read_csv(roster_path, {"student_id", "group_id"})
    listed = {r["student_id"].strip() for r in roster}
    have = {r["student_id"] for r in rows}
    return (
        [r for r in rows if r["student_id"] in listed],
        sorted(listed - have),
        sorted(have - listed),
    )


def address(row, domain):
    """An `email` column in the roster wins; otherwise <eid>@<domain>."""
    return row["email"] or f"{row['student_id']}@{domain}"


# What a group actually gets when starts are rationed. Written from the
# server's own max_starts, because the difference between "the hours are the
# limit" and "you get ONE session, and stopping it ends the assignment" is the
# difference between a group that plans and a group that finds out.
STARTS_NOTE = """
  * Your group gets {starts}. `stop` ends {it} for good and there is
    no next one, so do not run it until you are finished - not overnight
    and not "just to be safe". Hours left and no session means you are
    stuck until you email the instructor.
"""


def live_config(api_url):
    """Read the assignment, node count, budget, start limit and deadline off the
    running service.

    So the mail cannot quote a deadline, a budget or a ration the deployment
    does not actually have. Getting that wrong in fifty mailboxes is not
    something you can take back, and it is exactly the sort of thing that
    drifts between an edit and a send.
    """
    with urllib.request.urlopen(api_url.rstrip("/") + "/healthz", timeout=15) as r:
        return json.loads(r.read())


def wrong_domain_warning(pending, domain):
    """--domain rewrites every recipient, which is only ever right for a whole
    class on one mail system. Mailing <eid>@somewhere-else is not a typo you
    find out about gently: each message is a live credential, and the mailbox
    it lands in probably belongs to a stranger."""
    return [
        f"WARNING: --domain {domain} sends to addresses like "
        f"{address(pending[0], domain)},",
        f"  not @{DEFAULT_DOMAIN}. Every one of those messages is a working token.",
        "  To send yourself a test, use --to <your address>, which leaves the",
        "  student addresses alone.",
    ]


def redact(token):
    return token.split(".", 1)[0] + "." + "*" * 12


def build(row, args, health):
    msg = EmailMessage()
    msg["Subject"] = SUBJECT.format(course=args.course, assignment=health["assignment"])
    msg["From"] = args.sender
    msg["To"] = args.to or address(row, args.domain)
    if args.reply_to:
        msg["Reply-To"] = args.reply_to
    nodes = health.get("nodes_per_group") or 1
    # The server's own figure, so the mail cannot quote a budget the deployment
    # does not have. --gpu-hours is the fallback for a service too old to
    # publish it.
    quota = health.get("gpu_hour_quota") or args.gpu_hours
    # ":g" so a whole number reads as "20", not "20.0". The quota arrives as a
    # float from JSON and "20.0 GPU-hours" looks like a rounding artefact.
    quota = f"{quota:g}" if isinstance(quota, (int, float)) else quota
    starts = health.get("max_starts") or 0
    starts_note = "" if not starts else STARTS_NOTE.format(
        starts="ONE session, total" if starts == 1 else f"{starts} sessions in total",
        it="it" if starts == 1 else "one of them",
    )
    msg.set_content(BODY.format(
        name=row["name"],
        course=args.course,
        assignment=health["assignment"],
        token=row["token"],
        group=row["group_id"],
        quota=quota,
        # Its own line rather than mid-sentence: this is plain text with a
        # hand-wrapped body, so an inline clause of variable length is how you
        # mail fifty people a 120-column paragraph.
        node_note="" if nodes == 1 else (
            f"\n    Your group gets {nodes} nodes at once, so one hour of running\n"
            f"    costs {nodes} GPU-hours."
        ),
        starts_note=starts_note,
        # The bullets below are counted, and one of them is conditional.
        count="Four" if starts_note else "Three",
        deadline=health.get("deadline") or "none",
        cli_url=args.cli_url,
        signature=args.signature,
    ))
    if args.attach:
        data = Path(args.attach).read_bytes()
        # text/x-python rather than application/octet-stream: some providers
        # quarantine unknown binary attachments outright.
        msg.add_attachment(data, maintype="text", subtype="x-python",
                           filename=Path(args.attach).name)
    return msg


def already_sent(path):
    if not path or not os.path.exists(path):
        return set()
    with open(path) as f:
        return {line.strip() for line in f if line.strip()}


def record(path, student_id):
    """Append before moving on, so an interrupted run resumes without
    double-sending - and, more importantly, without silently skipping anyone."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(student_id + "\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tokens", help="tokens.csv from ./admin.py roster")
    ap.add_argument("--roster", help="roster.csv: cross-check who is missing, "
                                     "and skip token rows that are not on it")
    ap.add_argument("--send", action="store_true", help="actually send (default: dry run)")
    ap.add_argument("--eml-dir", help="write .eml files here instead of sending")
    ap.add_argument("--from", dest="sender", help="From: address (required with --send)")
    ap.add_argument("--reply-to")
    ap.add_argument("--course", default="CS 378")
    ap.add_argument("--gpu-hours", default="20",
                    help="fallback only: the service publishes GPULEASE_GPU_HOUR_QUOTA "
                         "on /healthz and that wins")
    ap.add_argument("--api-url", default=DEFAULT_API)
    ap.add_argument("--cli-url", default="the course page on Canvas",
                    help="where students download cli/gpulease.py")
    ap.add_argument("--attach", help="attach this file (e.g. cli/gpulease.py)")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN,
                    help=f"student addresses are <student_id>@DOMAIN (default {DEFAULT_DOMAIN})")
    ap.add_argument("--to", metavar="ADDRESS",
                    help="smoke test: send every message to this address instead of the "
                         "student. Not written to the sent log, so the real send still goes")
    ap.add_argument("--signature", default="-- \nthe course staff")
    ap.add_argument("--smtp-host", default="smtp.gmail.com")
    ap.add_argument("--smtp-port", type=int, default=587)
    ap.add_argument("--smtp-user", help="default: the --from address")
    ap.add_argument("--sent-log", default="tokens-sent.log")
    ap.add_argument("--only", action="append", help="just this student_id (repeatable)")
    ap.add_argument("--limit", type=int, help="stop after N messages - test with --limit 1")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between sends")
    ap.add_argument("--show-tokens", action="store_true",
                    help="print tokens in full; leaves every credential in your scrollback")
    args = ap.parse_args()

    recipients, no_token, extra = collect(args.tokens, args.roster)

    if no_token:
        print(f"WARNING: {len(no_token)} roster student(s) have no token in {args.tokens}:")
        print("  " + ", ".join(no_token))
        print("  They already had one, so it was not reissued - and only the hash is stored,")
        print("  so it cannot be looked up. Reissue with:  ./admin.py roster roster.csv --rotate\n")
    if extra:
        print(f"note: {len(extra)} token(s) in {args.tokens} are not on the roster and will")
        print(f"      NOT be mailed: {', '.join(extra)}\n")

    if args.only:
        recipients = [r for r in recipients if r["student_id"] in set(args.only)]
    done = already_sent(args.sent_log)
    pending = [r for r in recipients if r["student_id"] not in done]
    if args.limit:
        pending = pending[:args.limit]

    health = live_config(args.api_url)
    print(f"service: {args.api_url}  assignment={health['assignment']} "
          f"nodes={health.get('nodes_per_group')} "
          f"quota={health.get('gpu_hour_quota') or args.gpu_hours} "
          f"max_starts={health.get('max_starts') or 'unlimited'} "
          f"deadline={health.get('deadline')}")
    scope = "student(s) on the roster with a token" if args.roster else "token(s) to mail"
    print(f"{len(recipients)} {scope}, {len(done)} already sent, {len(pending)} to go\n")
    if not pending:
        return

    if args.eml_dir:
        out = Path(args.eml_dir)
        out.mkdir(parents=True, exist_ok=True)
        for row in pending:
            fd = os.open(out / f"{row['student_id']}.eml",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(build(row, args, health).as_bytes())
        print(f"wrote {len(pending)} .eml file(s) to {out} (0600 - they hold live tokens)")
        return

    if not args.send:
        for row in pending:
            tok = row["token"] if args.show_tokens else redact(row["token"])
            print(f"  {address(row, args.domain):<34} group {row['group_id']:<4} {tok}")
        if args.to:
            print(f"\n  (--to {args.to}: every one of these would be addressed to you)")
        elif args.domain != DEFAULT_DOMAIN:
            print("\n" + "\n".join(wrong_domain_warning(pending, args.domain)))
        print(f"\ndry run. Nothing sent. Add --send (and --from) to mail these "
              f"{len(pending)} student(s).")
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
                s.send_message(build(row, args, health))
            except smtplib.SMTPException as e:
                # Keep going: one bad address must not strand the rest of the
                # class, and the sent-log makes the retry safe.
                print(f"  FAILED {to}: {e}")
                continue
            if not args.to:
                record(args.sent_log, row["student_id"])
            sent += 1
            print(f"  sent   {to}")
            time.sleep(args.delay)

    if args.to:
        print(f"\n{sent} test message(s) sent to {args.to}. Nothing logged, so the real "
              f"send still covers everyone.")
        return
    print(f"\n{sent} sent, logged in {args.sent_log}. Re-running skips them.")
    print("Now delete tokens.csv - it is the only copy of these secrets.")


if __name__ == "__main__":
    main()
