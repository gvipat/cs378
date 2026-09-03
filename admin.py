#!/usr/bin/env python3
"""Instructor tools. There is no web UI, so you need this from week one.

    ./admin.py roster roster.csv        # load students, mint tokens -> tokens.csv
    ./admin.py roster roster.csv --rotate
    ./admin.py sessions                 # who has what, and what it has cost
    ./admin.py instances                # ground truth from EC2
    ./admin.py kill --group 7           # stop instances now
    ./admin.py reap                     # run one reconciliation pass
    ./admin.py grant 7                  # give group 7 another start
    ./admin.py disable abc123           # revoke a token (enable puts it back)

Run it on the control-plane box; it reads the same gpulease.env the service does.
"""

import argparse
import csv
import os
import pathlib
import secrets
import sys
import time

# Re-exec under the virtualenv setup.sh built, so `./admin.py` works from any
# shell without anyone having to remember to activate anything.
_ROOT = pathlib.Path(__file__).resolve().parent
_VENV = _ROOT / ".venv"
if (_VENV / "bin" / "python").exists() and pathlib.Path(sys.prefix) != _VENV:
    _py = str(_VENV / "bin" / "python")
    os.execv(_py, [_py, str(_ROOT / "admin.py"), *sys.argv[1:]])

from gpulease import aws, config, db  # noqa: E402


def fmt_age(ts):
    if not ts:
        return "-"
    d = int(time.time()) - int(ts)
    return f"{d // 3600}h{(d % 3600) // 60:02d}m"


def cmd_roster(args):
    """Load the roster and mint one token per student.

    tokens.csv is the ONLY copy of the secrets - only their SHA-256 is stored -
    so distribute it and then delete it. Re-running is safe: existing students
    keep their token unless you pass --rotate.
    """
    db.init()
    with open(args.roster, newline="") as f:
        rows = list(csv.DictReader(f))
    missing = {"student_id", "group_id"} - set(rows[0].keys() if rows else [])
    if missing:
        sys.exit(f"roster is missing columns: {sorted(missing)}")

    issued, kept = [], 0
    with db.write() as cx:
        for row in rows:
            sid = row["student_id"].strip()
            gid = str(row["group_id"]).strip()
            name = (row.get("name") or "").strip()
            existing = db.get_student(cx, sid)

            if existing and not args.rotate:
                if existing["group_id"] != gid or existing["name"] != name:
                    db.upsert_student(cx, sid, name, gid)
                    print(f"  updated {sid}: group {existing['group_id']} -> {gid}")
                kept += 1
                continue

            token = f"{sid}.{secrets.token_urlsafe(24)}"
            db.upsert_student(cx, sid, name, gid, db.hash_secret(token.split(".", 1)[1]))
            issued.append({"student_id": sid, "name": name, "group_id": gid, "token": token})

    if issued:
        # 0600 from the moment it exists: this file is every listed student's
        # live credential, in plaintext, and it usually gets written on a shared
        # host. Opening then chmod'ing would leave a readable window.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["student_id", "name", "group_id", "token"])
            w.writeheader()
            w.writerows(issued)

    print(f"\n{len(issued)} token(s) issued -> {args.out}")
    print(f"{kept} student(s) already had one (use --rotate to reissue)")
    if issued:
        print("\nThis file is the only copy of these secrets. Distribute it, then delete it.")


def cmd_access(args):
    """Revoke or restore a student's access without touching their group.

    Loading a roster never disables anyone who has dropped off it - silently
    revoking access because a CSV changed is not a good default - so this is
    how you actually do it.
    """
    disabled = 1 if args.cmd == "disable" else 0
    with db.write() as cx:
        if not db.get_student(cx, args.student_id):
            sys.exit(f"no such student: {args.student_id}")
        cx.execute(
            "UPDATE students SET disabled = ? WHERE student_id = ?",
            (disabled, args.student_id),
        )
    print(f"{args.student_id}: {'disabled' if disabled else 'enabled'}")
    if disabled:
        print("Their group's running session is unaffected; stop it if you need to.")


def cmd_grant(args):
    """Give a group another start after their one session ended badly.

    With GPULEASE_MAX_STARTS=1 this is the only way back in, so expect to use
    it: an instance that came up broken, a student who stopped the session by
    mistake, a lease that expired mid-experiment.
    """
    aid = args.assignment or config.ACTIVE_ASSIGNMENT
    left = db.grant_starts(args.group_id, aid, args.starts)
    if left is None:
        sys.exit(f"no session row for group {args.group_id} on {aid} - nothing to grant")
    used = f"{left}/{config.MAX_STARTS}" if config.MAX_STARTS else f"{left} (no limit set)"
    print(f"group {args.group_id} on {aid}: starts used now {used}")


def cmd_sessions(args):
    rows = sorted(db.all_sessions(), key=lambda r: (r["status"], r["group_id"]))
    limit = config.MAX_STARTS or "-"
    if config.DEADLINE:
        state = "PASSED" if time.time() >= config.DEADLINE else "upcoming"
        print(f"{config.ACTIVE_ASSIGNMENT} deadline: {config.deadline_str()} ({state})\n")
    print(f"{'GROUP':<8}{'ASSIGN':<10}{'STATUS':<14}{'UP':<8}{'GPU-HRS':<9}{'STARTS':<8}"
          f"{'NODES':<7}INSTANCES")
    total = 0.0
    for r in rows:
        hrs = (r["gpu_seconds_used"] or 0) / 3600
        total += hrs
        up = fmt_age(r["started_at"]) if r["status"] in db.LIVE else "-"
        starts = f"{r['starts_used']}/{limit}"
        ids = db.node_instance_ids(r)
        print(
            f"{r['group_id']:<8}{r['assignment_id']:<10}{r['status']:<14}"
            f"{up:<8}{hrs:<9.2f}{starts:<8}{db.node_count(r):<7}"
            f"{','.join(ids) if ids else '-'}"
        )
    # GPU-hours are node-hours: a two-node session bills two per wall-clock
    # hour, which is the number that matches the invoice.
    print(f"\ntotal GPU-hours consumed: {total:.2f} (quota {config.GPU_HOUR_QUOTA}/group, "
          f"{config.NODES_PER_GROUP} node(s) per session)")
    if config.MAX_STARTS:
        spent = sum(1 for r in rows if r["starts_used"] >= config.MAX_STARTS
                    and r["status"] not in db.LIVE)
        print(f"{spent} group(s) have used up their sessions (./admin.py grant <group>)")


def cmd_instances(args):
    print(f"{'INSTANCE':<21}{'GROUP':<8}{'NODE':<6}{'STATE':<12}{'TYPE':<14}{'PRIVATE':<16}HOST")
    for i in sorted(
        aws.course_instances(),
        key=lambda i: (aws.instance_tag(i, "Group", ""), aws.instance_tag(i, "NodeIndex", "0")),
    ):
        print(
            f"{i['InstanceId']:<21}{aws.instance_tag(i, 'Group', '-'):<8}"
            f"{aws.instance_tag(i, 'NodeIndex', '-'):<6}"
            f"{i['State']['Name']:<12}{i['InstanceType']:<14}"
            f"{aws.private_ip(i) or '-':<16}"
            f"{aws.public_host(i) or '-'}"
        )


def cmd_kill(args):
    ids = [
        i["InstanceId"]
        for i in aws.course_instances(states=("pending", "running"))
        if not args.group or aws.instance_tag(i, "Group") == args.group
    ]
    if not ids:
        print("nothing running")
        return
    print("stopping:", ids)
    if input("confirm (yes): ").strip() != "yes":
        return
    aws.stop_instances(ids, "instructor kill")
    print("done. The reaper will reconcile the database within a couple of minutes.")


def cmd_reap(args):
    from gpulease import reaper

    actions = reaper.run_once()
    print(actions or "nothing to do")


def cmd_preflight(args):
    print(f"course={config.COURSE} region={config.REGION} db={config.DB_PATH}")
    sys.exit(0 if aws.preflight() else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("roster", help="load students and mint tokens")
    p.add_argument("roster")
    p.add_argument("--out", default="tokens.csv")
    p.add_argument("--rotate", action="store_true", help="reissue tokens that already exist")
    p.set_defaults(func=cmd_roster)

    sub.add_parser("sessions", help="what the database believes").set_defaults(func=cmd_sessions)
    sub.add_parser("instances", help="what EC2 actually has").set_defaults(func=cmd_instances)
    sub.add_parser("reap", help="run one reconciliation pass").set_defaults(func=cmd_reap)
    sub.add_parser("preflight", help="check AWS permissions").set_defaults(func=cmd_preflight)

    p = sub.add_parser("kill", help="stop instances now")
    p.add_argument("--group", help="limit to one group")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("grant", help="give a group another start")
    p.add_argument("group_id")
    p.add_argument("--starts", type=int, default=1, help="how many (default 1)")
    p.add_argument("--assignment", help="default: the active one")
    p.set_defaults(func=cmd_grant)

    for verb in ("disable", "enable"):
        p = sub.add_parser(verb, help=f"{verb} one student's token")
        p.add_argument("student_id")
        p.set_defaults(func=cmd_access)

    args = ap.parse_args()
    args.func(args)
