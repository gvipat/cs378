#!/usr/bin/env python3
"""Turn the Google Form group-signup export into a roster for `admin.py roster`.

    ./roster_from_form.py "CS 378 Groups - Form Responses 1.csv"

The form gives one row per *group* with a name/EID pair per member; `admin.py
roster` wants one row per *student*, `student_id,name,group_id`. Between those
two shapes sit the things students actually type into a form: mixed-case EIDs,
"N/A" where a partner should be, trailing spaces, and the same person on two
submissions. Each of those is handled here and reported, because the fixups are
not always right and you should see them before minting tokens.

The EID becomes `student_id`, which is a primary key and the prefix of every
token, so it is lowercased -- `RSB2838` and `rsb2838` must not become two
students holding two tokens for one group.

Stdlib only, Python 3.9+.
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime

# What students write in a member field when there is no such member.
NOT_A_MEMBER = {"", "n/a", "na", "n\\a", "none", "-", "--", "x", "tbd"}

# Not a validator, just a smell test: a UT EID is letters then digits.
EID_SHAPE = re.compile(r"^[a-z]{1,6}\d{1,6}$")


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def clean_name(raw):
    return " ".join((raw or "").split())


def clean_eid(raw):
    return "".join((raw or "").split()).lower()


def member_columns(fieldnames):
    """Find the (name, eid) column pairs, however many members the form asked for."""
    eid_cols = [c for c in fieldnames if re.search(r"\beid\b", c, re.I)]
    pairs = []
    for eid_col in eid_cols:
        stem = re.sub(r"\s*eid\s*$", "", eid_col, flags=re.I).strip().lower()
        name_col = next(
            (c for c in fieldnames if c.strip().lower() == f"{stem} name"), None
        )
        pairs.append((name_col, eid_col))
    if not pairs:
        sys.exit(f"no EID columns found in: {fieldnames}")
    return pairs


def find_column(fieldnames, pattern, what):
    col = next((c for c in fieldnames if re.search(pattern, c, re.I)), None)
    if col is None:
        sys.exit(f"no {what} column found in: {fieldnames}")
    return col


def parse_timestamp(raw):
    """Form timestamps, so a resubmission can be ordered against the original.

    Rows are not in timestamp order in the export. An unparseable stamp sorts
    last rather than aborting the run: order only matters for the handful of
    rows that collide.
    """
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.max


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("form_csv", help="the Form Responses export")
    ap.add_argument("-o", "--out", default="roster.csv", help="default: roster.csv")
    ap.add_argument(
        "--keep",
        choices=("last", "first"),
        default="last",
        help="which submission wins when one student appears twice "
             "(default: last, i.e. their most recent answer)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would be written, write nothing"
    )
    args = ap.parse_args()

    with open(args.form_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("form export is empty")

    cols = list(rows[0].keys())
    ts_col = find_column(cols, r"timestamp", "timestamp")
    group_col = find_column(cols, r"group", "group")
    pairs = member_columns(cols)
    print(f"reading {len(rows)} submission(s); {len(pairs)} member slot(s) per group\n")

    # student_id -> record. Sorted so that the winning submission is written last.
    students = {}
    conflicts = []

    for row in sorted(
        rows, key=lambda r: parse_timestamp(r.get(ts_col)), reverse=(args.keep == "first")
    ):
        gid = str(row.get(group_col) or "").strip()
        if not gid:
            warn(f"submission at {row.get(ts_col)!r} has no group number - skipped")
            continue

        for name_col, eid_col in pairs:
            eid = clean_eid(row.get(eid_col))
            name = clean_name(row.get(name_col) if name_col else "")
            if eid in NOT_A_MEMBER:
                continue
            if not EID_SHAPE.match(eid):
                warn(f"group {gid}: {eid!r} does not look like an EID - kept anyway")

            prev = students.get(eid)
            if prev and prev["group_id"] != gid:
                # Same person, two groups. Only one can win: `students.group_id`
                # is a single column, and the group id is the whole identity a
                # session is keyed on.
                conflicts.append((eid, name or prev["name"], prev["group_id"], gid))
            students[eid] = {"student_id": eid, "name": name, "group_id": gid}

    if conflicts:
        print(
            f"\n{len(conflicts)} student(s) submitted under two groups "
            f"(--keep {args.keep} wins):",
            file=sys.stderr,
        )
        for eid, name, losing, winning in conflicts:
            print(f"  {eid} ({name}): group {losing} -> group {winning}", file=sys.stderr)

    # A group that lost every member to a conflict is gone, not empty.
    listed = {str(r.get(group_col) or "").strip() for r in rows}
    kept = {s["group_id"] for s in students.values()}
    for gid in sorted(listed - kept, key=lambda g: (len(g), g)):
        if gid:
            warn(f"group {gid} has no members left after dedup - dropped")

    by_group = {}
    for s in students.values():
        by_group.setdefault(s["group_id"], []).append(s)
    for gid, members in sorted(by_group.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if len(members) == 1:
            warn(f"group {gid} has one member ({members[0]['student_id']})")

    def sort_key(s):
        gid = s["group_id"]
        return (0, int(gid), s["student_id"]) if gid.isdigit() else (1, 0, gid)

    out_rows = sorted(students.values(), key=sort_key)
    print(f"\n{len(out_rows)} student(s) in {len(by_group)} group(s)")

    if args.dry_run:
        print("(dry run, nothing written)")
        return

    # Names and EIDs for a whole class, usually written on a shared host: 0600
    # from the moment the file exists, like tokens.csv.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["student_id", "name", "group_id"])
        w.writeheader()
        w.writerows(out_rows)

    print(f"wrote {args.out}\n\nNext: ./admin.py roster {args.out}")


if __name__ == "__main__":
    main()
