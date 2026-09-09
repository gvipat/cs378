#!/usr/bin/env python3
"""gpulease - request and release your group's GPU nodes.

Deliberately stdlib-only so students can run it with any Python 3.9+ and no
virtualenv. Deliberately dumb: every policy decision (quota, lease length,
which assignment is active) lives server-side so you can change the rules
mid-semester without asking 60 people to upgrade.

Students need no AWS account and no AWS credentials - just the course token
and the URL of the lease service.

A group's lease may be more than one instance -- the server decides how many.
All of them accept the same session key, and they sit on one subnet so they can
reach each other on any port, which is what a distributed run needs.

This docstring is for whoever maintains the file, and is deliberately NOT what
argparse prints: it used to be, and the rewrapping turned the usage block into
one unreadable line while telling students how the instructor changes the rules
mid-semester. Student-facing help is DESCRIPTION and EPILOG below, printed
verbatim. The long version is the README next to this file; keep them in step.
"""

import argparse
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request

# 2: multi-node. A version 1 client only ever displayed one host and would
# hide half of a two-node cluster, so the server refuses it outright.
# 3: ephemeral nodes. A version 2 client tells students their files survive a
# stop. They do not - a stop destroys the nodes and their disks - so the server
# refuses it too rather than let it reassure a group on the way to deleting
# their work.
VERSION = 3
API_URL = os.environ.get("GPULEASE_API", "https://utcs378-infra.duckdns.org")
CONFIG_DIR = os.path.expanduser("~/.config/gpulease")
CRED_FILE = os.path.join(CONFIG_DIR, "credentials")
SSH_DIR = os.path.expanduser("~/.ssh")

POLL_INTERVAL = 5
POLL_TIMEOUT = 420


def _prog():
    """The command the student actually typed, for the examples in --help.

    Someone running `python3 gpulease.py` should not be told to type
    `gpulease`, and someone who dropped it on their PATH should not be told to
    type `python3`. argparse's default is the bare basename, which gets the
    first case wrong in a way that reads as a broken instruction.

    `py` on Windows for the same reason: the python.org installer ships
    python.exe and the py launcher and no python3.exe at all, so `python3` there
    hits the Microsoft Store's "Python was not found" redirect -- an error about
    installing Python, printed to someone who has just run Python.
    """
    name = os.path.basename(sys.argv[0]) or "gpulease"
    if not name.endswith(".py"):
        return name
    return f"{'py' if os.name == 'nt' else 'python3'} {name}"


PROG = _prog()

DESCRIPTION = """\
Request and release your group's GPU nodes.

Your group shares one lease. Whoever runs `start` first brings the nodes up;
everyone else's `start` hands back those same nodes and the same key. `stop`
ends the session for the whole group, so tell them before you run it.

Nodes are temporary. `stop` - and the end of your lease - DESTROYS them and
everything on their disks. `start` then gives you new, empty nodes. Keep your
work in git, and start as many times as your gpu-hour budget allows.
"""

# Printed verbatim (RawDescriptionHelpFormatter), so the alignment below is
# what the student sees. The address is echoed rather than asked for: it is
# baked into API_URL above, so there is nothing for a student to configure, and
# showing it turns "cannot reach the lease service" into a one-look diagnosis.
# GPULEASE_API is mentioned only as an override -- it exists for running the
# CLI against a local instance, which is not something a student does.
EPILOG = f"""\
first time on this machine:
  {PROG} login <your-token>

after that:
  {PROG} start     a few minutes, then an ssh command per node
  {PROG} status    check on it any time, from anywhere
  {PROG} stop      when you are done - idle nodes bill too

your nodes are temporary:
  stop destroys them and their disks, and start gives you new empty ones.
  Push your work to git before you stop.

files this keeps on your machine:
  ~/.config/gpulease/credentials    your saved token
  ~/.ssh/gpulease_<group>           this session's key, replaced every session
"""


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def load_token():
    if not os.path.exists(CRED_FILE):
        die("no saved token. Run: gpulease login <your-token>")
    with open(CRED_FILE) as f:
        return f.read().strip()


def call(method, path, token=None):
    req = urllib.request.Request(
        API_URL.rstrip("/") + path,
        method=method,
        headers={
            "Authorization": f"Bearer {token or load_token()}",
            "X-CLI-Version": str(VERSION),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except json.JSONDecodeError:
            detail = body
        if e.code == 426:
            die(f"{detail}\n(your CLI is version {VERSION})")
        die(f"{detail}")
    except urllib.error.URLError as e:
        die(f"cannot reach the lease service: {e.reason}")


def write_private(path, text):
    """Create a file that is never, even briefly, readable by anyone else.

    Opening then chmod'ing leaves a window at the umask's mercy, which on a
    shared lab machine is enough to leak a key or a token.

    `newline=""` is load-bearing on Windows. Text mode there rewrites every "\\n"
    as "\\r\\n", and an OpenSSH private key with CRLF line endings does not parse
    -- ssh fails with `Load key: error in libcrypto`, which says nothing about
    line endings and sends the student to the course forum. The bytes we write
    have to be the bytes the server sent.

    On Windows the mode and the chmod below only toggle the read-only flag; the
    ACL restriction is POSIX-only. Files land under the user's profile, which
    Windows OpenSSH accepts, but they are not locked down the way they are on
    macOS and Linux.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        f.write(text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # in case the file pre-existed


def write_key(group_id, private_key):
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(SSH_DIR, f"gpulease_{group_id}")
    write_private(path, private_key.rstrip("\n") + "\n")
    return path


def fmt_remaining(expires_at):
    left = max(0, int(expires_at) - int(time.time()))
    return f"{left // 3600}h {(left % 3600) // 60}m"


def fmt_deadline(deadline):
    left = int(deadline) - int(time.time())
    if left <= 0:
        return "passed"
    days, rem = divmod(left, 86400)
    return f"in {days}d {rem // 3600}h" if days else f"in {rem // 3600}h {(rem % 3600) // 60}m"


def show(view, key_path=None):
    nodes = view.get("nodes") or []
    print(f"  group        {view['group_id']}   assignment {view['assignment_id']}")
    print(f"  state        {view['state']}")
    if nodes:
        for node in nodes:
            # A one-instance lease is not a "cluster"; don't make it read like one.
            label = f"node{node.get('rank', 0)}" if len(nodes) > 1 else "host"
            addr = node.get("host") or "(no address yet)"
            extra = f"   {node['private_ip']}" if node.get("private_ip") else ""
            state = f"   [{node['state']}]" if node.get("state") else ""
            print(f"  {label:<12} {addr}{extra}{state}")
    elif view.get("host"):
        print(f"  host         {view['host']}")
    if view.get("expires_at"):
        # Say which cap this lease landed on. "Ends in 40m" reads like a bug
        # when the group expected eight hours; "ends in 40m (budget)" is the
        # same fact with the reason attached.
        why = {
            "budget": "  (all the gpu-hours you have left)",
            "deadline": "  (the assignment deadline)",
            "lease": "",
        }.get(view.get("ends_because"), "")
        print(f"  lease ends   in {fmt_remaining(view['expires_at'])}{why}")
    used = view.get("gpu_hours_used", 0)
    quota = view.get("gpu_hours_quota")
    left = view.get("gpu_hours_remaining")
    line = f"  gpu-hours    {used} used / {quota} allowed"
    if left is not None:
        line += f"   ({left} left)"
    print(line)
    # Counted per node: the number moves at node_count per wall-clock hour, and
    # a group that does not know that will plan against half the budget.
    count = view.get("node_count") or 1
    if count > 1:
        print(f"               counted per node, so {count} gpu-hours per hour running")
    if view.get("deadline"):
        print(f"  deadline     {fmt_deadline(view['deadline'])}")
    # Said on every status of a live session, not once at start: this is the
    # line that decides whether a group has their work in git when they stop.
    if view.get("state") in ("PROVISIONING", "RUNNING"):
        print("  NOTE: stopping destroys these nodes and their disks. Push to git first.")
    # Only shown when an instructor has rationed starts as well as hours. The
    # usual case is unlimited, where the gpu-hours line above is the whole
    # story and a "sessions used" counter would just be noise.
    allowed = view.get("starts_allowed") or 0
    if allowed:
        left = max(0, allowed - view.get("starts_used", 0))
        print(f"  sessions     {view.get('starts_used', 0)} used / {allowed} allowed")
        if left == 0 and view.get("state") in ("PROVISIONING", "RUNNING", "STOPPING"):
            print("  NOTE: this is your last session. Once you stop it, there is no next one.")
    if key_path and (nodes or view.get("host")):
        user = view.get("user", "ubuntu")
        print()
        # -A forwards the agent, which is how you hop from one node to the
        # next: every node accepts this same key, but the key itself stays on
        # your machine rather than being copied onto a shared instance.
        for node in nodes or [{"rank": 0, "host": view.get("host")}]:
            if node.get("host"):
                tail = f"   # node{node.get('rank', 0)}" if len(nodes) > 1 else ""
                print(f"  ssh -A -i {key_path} {user}@{node['host']}{tail}")
        if view.get("master_addr") and len(nodes) > 1:
            print()
            print(f"  MASTER_ADDR={view['master_addr']}  MASTER_PORT={view.get('master_port')}"
                  f"  NNODES={len(nodes)}")
            print("  (already exported on the nodes, along with NODE_RANK; peers are in")
            print("   /etc/gpulease/peers and in /etc/hosts as node0, node1, ...)")
        print()


# ------------------------------------------------------------------ commands


def cmd_login(args):
    # Tokens get copied out of a spreadsheet or an email, so they arrive with
    # stray whitespace often enough to be worth handling here rather than in a
    # traceback.
    token = args.token.strip()
    if "." not in token:
        die("that does not look like a course token")
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    write_private(CRED_FILE, token)
    who = call("GET", "/whoami", token=token)
    print(f"logged in as {who['student_id']} ({who.get('name')}), group {who['group_id']}")
    print(f"active assignment: {who['active_assignment']}")


def cmd_start(args):
    view = call("POST", "/session/start")
    if view["state"] == "RUNNING" and view.get("host"):
        print("Session already running for your group.")
        show(view, write_key(view["group_id"], view["private_key"]) if view.get("private_key") else None)
        return

    count = view.get("node_count") or 1
    what = "an instance" if count == 1 else f"{count} instances"
    print(f"Requesting {what} for your group", end="", flush=True)
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        print(".", end="", flush=True)
        view = call("GET", "/session")
        if view["state"] == "RUNNING" and view.get("private_key"):
            print(" ready\n")
            show(view, write_key(view["group_id"], view["private_key"]))
            print("  Note: this key is valid only for this session, on every node.")
            print("  These nodes are brand new and temporary. Nothing on them survives")
            print("  a stop - clone your repo on, and push before you finish.")
            return
        if view["state"] in ("STOPPED", "FAILED"):
            die("\nthe nodes failed to come up. Try again, then ask on the course forum.")
    die("\ntimed out waiting for the nodes. Run 'gpulease status' in a minute.")


def cmd_status(args):
    view = call("GET", "/session")
    key_path = os.path.join(SSH_DIR, f"gpulease_{view['group_id']}")
    show(view, key_path if os.path.exists(key_path) else None)


def cmd_stop(args):
    # Every stop destroys the nodes and their disks, so every stop is confirmed
    # -- not just the last one of a rationed set, which is all the old policy
    # asked about. A student who has not pushed their work has seconds to
    # realise it, and the prompt is the only thing that gives them those
    # seconds.
    if not args.yes:
        view = call("GET", "/session")
        if view.get("state") != "STOPPED":
            count = view.get("node_count") or 1
            noun = "node" if count == 1 else f"all {count} nodes"
            print(f"This DESTROYS your group's {noun} and everything on their disks.")
            print("Anything you have not pushed to git is gone for good.")
            allowed = view.get("starts_allowed") or 0
            if allowed and view.get("starts_used", 0) >= allowed:
                print(f"It is also your group's last session for "
                      f"{view.get('assignment_id')}: 'start' will not work again.")
            else:
                left = view.get("gpu_hours_remaining")
                print(f"'gpulease start' will give you new, empty nodes"
                      f"{'' if left is None else f' ({left} gpu-hours left)'}.")
            if input("Type 'stop' to confirm: ").strip() != "stop":
                print("Left running.")
                return

    result = call("POST", "/session/stop")
    if not result.get("stopped"):
        print("Nothing was running for your group.")
        show(result)
        return
    secs = result.get("session_duration_seconds", 0)
    print(f"Stopped group {result['group_id']} ({result['assignment_id']})")
    print(f"  session length   {secs // 3600}h {(secs % 3600) // 60}m")
    print(f"  gpu-hours used   {result.get('gpu_hours_used')} / {result.get('gpu_hours_quota')}")
    print(f"  {result.get('message')}")

    # Do not exit until EC2 has actually acknowledged it. A stop that silently
    # failed is a 48-hour bill.
    for _ in range(12):
        time.sleep(5)
        view = call("GET", "/session")
        state = view.get("instance_state")
        if state in ("shutting-down", "terminated", "stopping", "stopped") \
                or view["state"] == "STOPPED":
            print("  confirmed: the nodes are being destroyed.")
            return
    print("  warning: could not confirm shutdown. Run 'gpulease status' shortly.")


RAW = argparse.RawDescriptionHelpFormatter


def main():
    p = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=RAW,
    )
    # metavar keeps `{login,start,status,stop}` out of the usage line; the four
    # commands are listed underneath with their help anyway.
    sub = p.add_subparsers(dest="cmd", required=True, title="commands", metavar="<command>")

    lg = sub.add_parser(
        "login",
        help="save your course token (once per machine)",
        formatter_class=RAW,
        description=(
            "Save your course token on this machine.\n\n"
            "The token is yours personally, not your group's: do not share it or\n"
            "paste it into a chat. It is stored in ~/.config/gpulease/credentials,\n"
            "readable only by you. Nobody can look up a lost token, not even your\n"
            "instructor - they reissue instead."
        ),
    )
    lg.add_argument("token", help="the token from your instructor")
    lg.set_defaults(func=cmd_login)

    sub.add_parser(
        "start",
        help="bring up your group's nodes",
        formatter_class=RAW,
        description=(
            "Bring up your group's nodes and print an ssh command for each.\n\n"
            "This waits until every node is actually accepting ssh, which takes a\n"
            "few minutes, so the commands it prints work as soon as you see them.\n"
            "If it gives up waiting, the nodes are usually still on their way up:\n"
            "run `status` a minute later.\n\n"
            "If your group already has a session, this hands you that one rather\n"
            "than starting a second.\n\n"
            "Nodes are always new and empty - there is nothing left over from your\n"
            "last session, so expect to clone your repo and set up again. Start as\n"
            "often as you like; each session spends gpu-hours from your budget."
        ),
    ).set_defaults(func=cmd_start)

    sub.add_parser(
        "status",
        help="show the lease, the budget and the ssh commands",
        formatter_class=RAW,
        description=(
            "Show your group's lease: every node with its ssh command, when the\n"
            "lease ends and which limit ended it, the gpu-hours your group has\n"
            "used and has left, and the assignment deadline.\n\n"
            "Gpu-hours are counted per node, so a two-node session spends two of\n"
            "them for every hour it is up."
        ),
    ).set_defaults(func=cmd_status)

    st = sub.add_parser(
        "stop",
        help="destroy the nodes and stop the charges",
        formatter_class=RAW,
        description=(
            "Destroy every node in your group's lease and stop the billing.\n\n"
            "The nodes and their disks are deleted. Anything you have not pushed\n"
            "to git is gone - there is no undo and no copy. This ends the session\n"
            "for your whole group, not just for you, so tell them first.\n\n"
            "You can `start` again afterwards and get new, empty nodes, for as\n"
            "long as your group's gpu-hour budget lasts.\n\n"
            "You will be asked to type 'stop' to confirm."
        ),
    )
    st.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip that confirmation - there is no undo",
    )
    st.set_defaults(func=cmd_stop)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
