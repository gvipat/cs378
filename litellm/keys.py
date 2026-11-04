#!/usr/bin/env python3
"""LLM proxy keys: one LiteLLM team per group, one key per student.

    ./litellm/keys.py mint roster.csv                  # $50 per group -> llm-tokens.csv
    ./litellm/keys.py mint roster.csv --rotate         # reissue existing keys
    ./litellm/keys.py show                             # spend per group
    ./litellm/keys.py budget 7 --usd 80                # change group 7's cap
    ./litellm/keys.py revoke abc123                    # block a key (restore undoes)

The cap is on the team, so a group's members share it. Run on the lease host;
it talks to the proxy on localhost with the master key from litellm/litellm.env.
LITELLM_URL / LITELLM_MASTER_KEY in the environment override both (e.g. over an
SSH tunnel). Stdlib only.
"""

import argparse
import csv
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

_DIR = pathlib.Path(__file__).resolve().parent
URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
DEFAULT_BUDGET_USD = 50.0


def _master_key():
    if os.environ.get("LITELLM_MASTER_KEY"):
        return os.environ["LITELLM_MASTER_KEY"]
    try:
        for line in (_DIR / "litellm.env").read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError as e:
        sys.exit(f"cannot read the master key: {e}")
    sys.exit("no LITELLM_MASTER_KEY in litellm/litellm.env")


def _models():
    """The model names config.yaml serves; every team gets all of them."""
    text = (_DIR / "config.yaml").read_text()
    names = re.findall(r"^\s*-\s*model_name:\s*(\S+)", text, re.M)
    if not names:
        sys.exit("no model_name entries in litellm/config.yaml")
    return names


def api(method, path, body=None, **query):
    url = URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_master_key()}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach the proxy at {URL}: {e.reason}")


def team_alias(group_id):
    return f"group-{group_id}"


def teams_by_alias():
    return {t.get("team_alias"): t for t in api("GET", "/team/list")}


def team_keys(team_id):
    """Every key on a team, paging through /key/list."""
    keys, page = [], 1
    while True:
        r = api("GET", "/key/list", team_id=team_id, return_full_object="true",
                page=page, size=100)
        keys += r.get("keys", [])
        if page >= (r.get("total_pages") or 1):
            return keys
        page += 1


def key_for(student_id):
    r = api("GET", "/key/list", key_alias=student_id, return_full_object="true")
    keys = r.get("keys", [])
    if not keys:
        sys.exit(f"no key for {student_id}")
    return keys[0]


def cmd_mint(args):
    """Create any missing teams and keys from the roster.

    Like admin.py roster: llm-tokens.csv is the only copy of the keys LiteLLM
    hands back (it stores a hash), existing students keep theirs unless you pass
    --rotate, and the file is rewritten with only what this run issued. Existing
    teams keep their budgets but get config.yaml's current model list.
    """
    with open(args.roster, newline="") as f:
        rows = list(csv.DictReader(f))
    missing = {"student_id", "group_id"} - set(rows[0].keys() if rows else [])
    if missing:
        sys.exit(f"roster is missing columns: {sorted(missing)}")

    models = _models()
    teams = teams_by_alias()
    roster_teams = {team_alias(str(r["group_id"]).strip()) for r in rows}
    need = roster_teams - set(teams)
    # A team's model list is fixed at creation, so a model added to config.yaml
    # is refused for existing groups until their list is brought up to date.
    for alias in sorted(roster_teams - need):
        if sorted(teams[alias].get("models") or []) != sorted(models):
            api("POST", "/team/update", {"team_id": teams[alias]["team_id"], "models": models})
            print(f"  updated {alias}: {len(models)} models")
    for alias in sorted(need):
        body = {"team_alias": alias, "max_budget": args.budget, "models": models}
        if args.duration:
            body["budget_duration"] = args.duration
        teams[alias] = api("POST", "/team/new", body)
        print(f"  created {alias}: ${args.budget:g}"
              + (f" per {args.duration}" if args.duration else " total"))

    existing = {}
    for alias in {team_alias(str(r["group_id"]).strip()) for r in rows}:
        for k in team_keys(teams[alias]["team_id"]):
            existing[k.get("key_alias")] = (alias, k)

    issued, kept = [], 0
    for row in rows:
        sid = row["student_id"].strip()
        gid = str(row["group_id"]).strip()
        alias = team_alias(gid)
        have = existing.get(sid)
        if have and not args.rotate:
            kept += 1
            continue
        if have:
            api("POST", "/key/delete", {"keys": [have[1]["token"]]})
        k = api("POST", "/key/generate", {
            "team_id": teams[alias]["team_id"], "user_id": sid, "key_alias": sid,
            "rpm_limit": args.rpm, "max_parallel_requests": args.parallel,
            "metadata": {"name": (row.get("name") or "").strip(), "group_id": gid},
        })
        issued.append({"student_id": sid, "name": (row.get("name") or "").strip(),
                       "group_id": gid, "key": k["key"]})

    # A student whose group changed in the roster still has a key on the old
    # team; say so rather than silently leaving them spending the old budget.
    for sid, (alias, _) in existing.items():
        for row in rows:
            if row["student_id"].strip() == sid and team_alias(str(row["group_id"]).strip()) != alias:
                print(f"  NOTE: {sid}'s key is on {alias}; --rotate moves it to their new group")

    if issued:
        # 0600 from the moment it exists, same as tokens.csv.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["student_id", "name", "group_id", "key"])
            w.writeheader()
            w.writerows(issued)

    print(f"\n{len(issued)} key(s) issued -> {args.out}")
    print(f"{kept} student(s) already had one (use --rotate to reissue)")
    if issued and kept:
        print(f"\nNOTE: {args.out} now holds ONLY those {len(issued)} key(s). If it "
              f"previously\nheld the other {kept} and you had not distributed them yet, "
              f"they are unrecoverable;\nreissue with --rotate.")
    if issued:
        print("\nThis file is the only copy of these keys. Distribute it, then delete it.")


def cmd_show(args):
    teams = sorted((t for a, t in teams_by_alias().items() if a and a.startswith("group-")),
                   key=lambda t: t["team_alias"])
    print(f"{'GROUP':<14}{'SPENT':>10}{'CAP':>10}{'LEFT':>10}  {'KEYS':<6}RESETS")
    total = 0.0
    for t in teams:
        spend, cap = t.get("spend") or 0.0, t.get("max_budget")
        total += spend
        capstr = f"{cap:.2f}" if cap is not None else "-"
        left = f"{cap - spend:.2f}" if cap is not None else "-"
        nkeys = len(team_keys(t["team_id"]))
        print(f"{t['team_alias']:<14}{spend:>10.2f}{capstr:>10}"
              f"{left:>10}  {nkeys:<6}{t.get('budget_reset_at') or 'never'}")
    print(f"\ntotal spent: ${total:.2f} across {len(teams)} group(s)")


def cmd_budget(args):
    t = teams_by_alias().get(team_alias(args.group_id))
    if not t:
        sys.exit(f"no team for group {args.group_id}")
    api("POST", "/team/update", {"team_id": t["team_id"], "max_budget": args.usd})
    print(f"group {args.group_id}: cap ${args.usd:g}, ${t.get('spend') or 0:.2f} spent so far")


def cmd_access(args):
    k = key_for(args.student_id)
    verb = "block" if args.cmd == "revoke" else "unblock"
    api("POST", f"/key/{verb}", {"key": k["token"]})
    print(f"{args.student_id}: {verb}ed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("mint", help="create teams and keys from a roster")
    p.add_argument("roster")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD,
                   help="USD cap for each NEW group's team (default: %(default)g)")
    p.add_argument("--duration", help="reset the cap every period, e.g. 30d (default: never)")
    p.add_argument("--rpm", type=int, default=120, help="requests/minute per key")
    p.add_argument("--parallel", type=int, default=16, help="concurrent requests per key")
    p.add_argument("--out", default="llm-tokens.csv")
    p.add_argument("--rotate", action="store_true", help="reissue keys that already exist")
    p.set_defaults(func=cmd_mint)

    sub.add_parser("show", help="spend and cap per group").set_defaults(func=cmd_show)

    p = sub.add_parser("budget", help="set a group's USD cap")
    p.add_argument("group_id")
    p.add_argument("--usd", type=float, required=True)
    p.set_defaults(func=cmd_budget)

    for verb in ("revoke", "restore"):
        p = sub.add_parser(verb, help=f"{verb} one student's key")
        p.add_argument("student_id")
        p.set_defaults(func=cmd_access)

    args = ap.parse_args()
    args.func(args)
