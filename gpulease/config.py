"""All configuration, in one place, read from the environment.

systemd hands the service `gpulease.env` via EnvironmentFile. Anything run by
hand (`admin.py`, `python -m gpulease.reaper`) would otherwise see nothing, so
we load the same file here as a fallback -- without overriding real env vars,
so systemd and an interactive shell agree.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# GPULEASE_ENV picks a different file, which is how gpulease.env.test is used:
#
#     GPULEASE_ENV=gpulease.env.test ./admin.py sessions
#
# It REPLACES gpulease.env rather than layering on top of it, so a test run
# cannot inherit half of a production setting -- anything the chosen file omits
# falls back to the code defaults. systemd is unaffected either way: the unit
# passes gpulease.env as an EnvironmentFile and real environment variables
# always win over anything read here.
_load_env_file(Path(os.environ.get("GPULEASE_ENV") or ROOT / "gpulease.env"))


def _str(name, default=""):
    return (os.environ.get(name) or default).strip()


def _int(name, default):
    return int(float(_str(name) or default))


def _float(name, default):
    return float(_str(name) or default)


def _list(name, default=""):
    return [x.strip() for x in (_str(name) or default).split(",") if x.strip()]


# The offset GPULEASE_DEADLINE was written in, kept so deadline_str() can show
# students the wall clock they actually live in. None until _deadline() runs.
DEADLINE_TZ = None


def _deadline(name):
    """An absolute cutoff, as a unix timestamp. 0 when unset.

    Parsed at startup rather than on use so a typo is a loud failure at boot,
    not a cost control that silently never fires. A bare timestamp is read as
    UTC, which is rarely what a syllabus means - write the offset.
    """
    global DEADLINE_TZ
    raw = _str(name)
    if not raw:
        return 0
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"{name}={raw!r} is not an ISO-8601 timestamp. "
            f"Write it like 2026-09-15T23:59:00-05:00 (with your UTC offset)."
        ) from e
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    DEADLINE_TZ = stamp.tzinfo
    return int(stamp.timestamp())


def deadline_str():
    """The cutoff in the offset it was configured in, with UTC alongside.

    Both halves, because this string is quoted at students -- /healthz,
    api.start's refusal, the token mail -- and the two forms can name different
    DAYS. A cutoff at 23:59 on the 22nd US Central is 04:59 on the 23rd in UTC,
    and a mail that shows only the second tells a class the assignment runs a
    day longer than it does. UTC-only when that is what was configured, since
    printing it twice helps nobody.
    """
    if not DEADLINE:
        return "none"
    utc = datetime.fromtimestamp(DEADLINE, timezone.utc)
    if DEADLINE_TZ is None or DEADLINE_TZ.utcoffset(utc) == timezone.utc.utcoffset(utc):
        return utc.strftime("%Y-%m-%d %H:%M UTC")
    local = datetime.fromtimestamp(DEADLINE, DEADLINE_TZ)
    return f"{local:%Y-%m-%d %H:%M} {local:%Z} ({utc:%Y-%m-%d %H:%M} UTC)"


# --- identity -------------------------------------------------------------
# COURSE is the tag every instance carries. It is also the blast radius: the
# service only ever describes, starts or stops instances carrying this tag.
#
# The default is deliberately NOT the production tag (`utcs378`, which is what
# gpulease.env.example and the README walkthrough ship). Same reasoning as
# t3.micro vs g4dn.xlarge below: a missing or unreadable config file must not
# silently inherit the live course's blast radius and start stopping real
# students' instances. It fails loudly instead -- the lease host's IAM policy
# is scoped to the real tag, so an unconfigured install is denied at
# CreateSecurityGroup rather than doing something quiet and wrong.
COURSE = _str("GPULEASE_COURSE", "cs378")
REGION = _str("GPULEASE_REGION", "us-west-2")

# --- storage / serving ----------------------------------------------------
DB_PATH = _str("GPULEASE_DB") or str(ROOT / "var" / "gpulease.db")
# Localhost by default: the API carries bearer tokens and hands back session
# private keys, so it belongs behind a TLS terminator (see Caddyfile.example)
# rather than on the network itself. Set 0.0.0.0 only for a deliberately
# plain-HTTP deployment, and read README -> "Put TLS in front" first.
HOST = _str("GPULEASE_HOST", "127.0.0.1")
PORT = _int("GPULEASE_PORT", 8000)

# --- what we launch -------------------------------------------------------
AMI_ID = _str("GPULEASE_AMI")  # blank => latest Ubuntu 22.04 LTS from Canonical
INSTANCE_TYPE = _str("GPULEASE_INSTANCE_TYPE", "t3.micro")
ROOT_VOLUME_GB = _int("GPULEASE_ROOT_GB", 30)
SUBNET_IDS = _list("GPULEASE_SUBNET_IDS")  # blank => every default-VPC subnet
ALLOWED_SSH_CIDRS = _list("GPULEASE_ALLOWED_SSH_CIDRS", "0.0.0.0/0")

# --- policy ---------------------------------------------------------------
# Server-side on purpose: change the rules mid-semester without asking 60
# people to upgrade their CLI.
ACTIVE_ASSIGNMENT = _str("GPULEASE_ACTIVE_ASSIGNMENT", "hw1")

# Hard cutoff for the active assignment. Once it passes, no group may start,
# every lease is capped to end no later than this, and the reaper terminates
# anything still tagged for the course. Unset (blank) means no deadline.
DEADLINE = _deadline("GPULEASE_DEADLINE")

# The budget, and the ration: cumulative *node*-hours a group may burn on one
# assignment, charged at stop by db.accrue_and_close and checked at start by
# db.claim. db.claim also caps a session's expires_at at whatever is left, so
# the reaper's ordinary "lease expired" path is what ends a session that runs
# the budget out -- there is no separate mid-session accounting anywhere.
#
# Groups start as many sessions as they like (MAX_STARTS defaults to unlimited
# below); this number is the only thing that stops them. Set it with the bill
# in mind: the worst case for the course is
#
#     groups x GPU_HOUR_QUOTA x $/instance-hour
#
# with no NODES_PER_GROUP factor -- the quota is *already* counted in
# node-hours, so a second node makes a group spend it twice as fast rather than
# letting them spend twice as much.
GPU_HOUR_QUOTA = _float("GPULEASE_GPU_HOUR_QUOTA", 30)

# The smallest lease worth handing out. Booting a node burns several minutes of
# budget before a student can do anything with it, so a group with two minutes
# left is better told they are out than given a two-minute cluster.
#
# A lease length, not a budget: claim() checks it against remaining // nodes,
# so at two nodes a group needs twice this many node-minutes to be allowed to
# start. That is the same figure api.start quotes when it refuses them.
MIN_START_MINUTES = _float("GPULEASE_MIN_START_MINUTES", 15)

# How many times a group may start a session for one assignment. 0, the
# default, is unlimited: a session is cheap to start and destroys itself when
# it ends, so what a group spends is bounded by GPU_HOUR_QUOTA rather than by a
# count of attempts. A positive number caps the attempts as well, which is a
# blunt instrument -- a group that loses a session to a capacity error or a
# fat-fingered `stop` needs `admin.py grant` to get back in -- but it is there
# if you want it.
MAX_STARTS = _int("GPULEASE_MAX_STARTS", 0)
# With no idle watchdog on the instances, this is the only thing that bounds a
# single session's length. What bounds the total bill is GPU_HOUR_QUOTA. See
# the cost arithmetic in README -> "What a session can cost you".
#
# 0 means no lease cap, leaving the budget as the only bound. That is coherent
# under MAX_STARTS=0 -- claim() already shortens a lease to the group's
# remaining node-hours, so a session just runs until the budget is spent -- but
# know what it gives up. This is the only thing standing between a forgotten
# session and a group's whole quota: with nothing watching for an idle cluster,
# a group that starts on Friday and walks away burns one lease's worth of
# node-hours with a cap, and all of them without. Under MAX_STARTS=0 there is
# no start left to grant them either, so the way back in is
# `admin.py budget <group> --hours 0`.
MAX_SESSION_HOURS = _float("GPULEASE_MAX_SESSION_HOURS", 8)
# Per *node*. The old name meant the same thing back when a group got one box;
# it is still honoured so an existing gpulease.env keeps working.
GPUS_PER_NODE = _int("GPULEASE_GPUS_PER_NODE", _int("GPULEASE_GPUS_PER_GROUP", 1))

# How many instances a group's session consists of. The distributed-training
# assignment needs two; one is a single box exactly as before.
#
# This multiplies the bill directly -- a group now burns NODES_PER_GROUP
# instance-hours per wall-clock hour -- so it is capped. A typo that turns into
# a hundred GPU instances is not a mistake you find out about gently.
NODES_PER_GROUP = _int("GPULEASE_NODES_PER_GROUP", 2)
MAX_NODES_PER_GROUP = 8
if not 1 <= NODES_PER_GROUP <= MAX_NODES_PER_GROUP:
    raise SystemExit(
        f"GPULEASE_NODES_PER_GROUP must be between 1 and {MAX_NODES_PER_GROUP}, "
        f"got {NODES_PER_GROUP}"
    )

# Rendezvous port exported to the instances as MASTER_PORT. torch.distributed's
# default; only worth changing if something else on the image wants it.
MASTER_PORT = _int("GPULEASE_MASTER_PORT", 29500)

# There is deliberately nothing here about reclaiming disks. Instances are
# ephemeral: a session's nodes are terminated when it ends, by any route, and
# their root volumes go with them (DeleteOnTermination). Nothing accumulates
# between sessions, so there is no end-of-assignment cleanup to schedule and no
# GPULEASE_TERMINATE_AT_DEADLINE / _GRACE_HOURS to get wrong. If either name is
# still in your gpulease.env it is now ignored; delete the lines.

# A lease cap of 0 is "no cap": ten years stands in for never, which keeps this
# an ordinary integer everywhere it is used and needs no special case at either
# call site. api.start adds it to now() for the cap it hands claim(), which
# then takes the min with the remaining budget; _ends_because() adds it to
# started_at to name whichever cap bound the session, and against a lease end a
# decade out the budget always wins -- which is the truth when there is no cap.
#
# Compared against the *hours*, not the seconds: a positive value that rounds
# down to zero seconds is a mistyped lease, not a request for an unlimited one.
NO_LEASE_CAP_SECONDS = 10 * 365 * 24 * 3600
MAX_SESSION_SECONDS = (
    NO_LEASE_CAP_SECONDS if MAX_SESSION_HOURS <= 0 else int(MAX_SESSION_HOURS * 3600)
)
QUOTA_SECONDS = int(GPU_HOUR_QUOTA * 3600)
MIN_START_SECONDS = int(MIN_START_MINUTES * 60)

# --- reaper ---------------------------------------------------------------
REAPER_ENABLED = _str("GPULEASE_REAPER", "1") not in ("0", "no", "false")
REAPER_INTERVAL = _int("GPULEASE_REAPER_INTERVAL", 120)
