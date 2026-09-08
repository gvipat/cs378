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


def _deadline(name):
    """An absolute cutoff, as a unix timestamp. 0 when unset.

    Parsed at startup rather than on use so a typo is a loud failure at boot,
    not a cost control that silently never fires. A bare timestamp is read as
    UTC, which is rarely what a syllabus means - write the offset.
    """
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
    return int(stamp.timestamp())


def deadline_str():
    if not DEADLINE:
        return "none"
    return datetime.fromtimestamp(DEADLINE, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
# every lease is capped to end no later than this, and the reaper stops
# anything still running. Unset (blank) means no deadline.
DEADLINE = _deadline("GPULEASE_DEADLINE")

# The budget. Cumulative *node*-hours a group may burn on one assignment,
# charged at stop by db.accrue_and_close and checked at start by db.claim.
# db.claim also caps a session's expires_at at whatever is left, so the reaper's
# ordinary "lease expired" path is what enforces the budget -- there is no
# separate mid-session accounting anywhere.
#
# It only rations anything when a group may start more than once: see
# MAX_STARTS.
GPU_HOUR_QUOTA = _float("GPULEASE_GPU_HOUR_QUOTA", 30)

# The smallest lease worth handing out. Booting a node burns several minutes of
# budget before a student can do anything with it, so a group with two minutes
# left is better told they are out than given a two-minute cluster.
MIN_START_MINUTES = _float("GPULEASE_MIN_START_MINUTES", 15)

# How many times a group may start a session for one assignment. 1 means a
# group gets a single lease: once it ends, for any reason, they are done until
# you move to the next assignment or run `admin.py grant`. 0 means unlimited,
# which is the setting that makes GPU_HOUR_QUOTA the actual ration.
MAX_STARTS = _int("GPULEASE_MAX_STARTS", 1)
# With no idle watchdog on the instances, this is the only thing that bounds a
# session's length, and so the only thing that bounds the bill. See the cost
# arithmetic in README -> "What a session can cost you".
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

# --- reclaiming disks after the deadline -----------------------------------
# Instances are stopped for the life of an assignment, because the root volume
# is the only persistence students get. Once the assignment is over those
# volumes are pure cost -- 30 groups x 2 nodes x 100 GB is several hundred
# dollars a month -- so the reaper may destroy them, but only after the
# deadline and only if you ask for it.
#
# Default OFF here and ON in gpulease.env.example, the same deliberate mismatch
# as t3.micro vs g4dn.xlarge: a missing config file should not destroy data for
# the same reason it should not launch a GPU fleet. Note that DEADLINE is
# parsed at import and the example ships a real date, so an install that takes
# the example verbatim inherits both a deadline and this behaviour.
TERMINATE_AT_DEADLINE = _str("GPULEASE_TERMINATE_AT_DEADLINE", "0") in ("1", "yes", "true")
# Breathing room between "stop everything" and "destroy the volumes". This is a
# window for the *instructor* to notice a mistyped deadline, not for students:
# once the deadline passes nobody can start a session, and a stopped instance
# cannot be sshed into. Warn students before the cutoff, not during this.
TERMINATE_GRACE_HOURS = _float("GPULEASE_TERMINATE_GRACE_HOURS", 24)

MAX_SESSION_SECONDS = int(MAX_SESSION_HOURS * 3600)
QUOTA_SECONDS = int(GPU_HOUR_QUOTA * 3600)
MIN_START_SECONDS = int(MIN_START_MINUTES * 60)
# 0 when there is no deadline, which is also the "never terminate" case: the
# reaper checks TERMINATE_AT_DEADLINE and a live DEADLINE before using this.
TERMINATE_AT = DEADLINE + int(TERMINATE_GRACE_HOURS * 3600) if DEADLINE else 0

# --- reaper ---------------------------------------------------------------
REAPER_ENABLED = _str("GPULEASE_REAPER", "1") not in ("0", "no", "false")
REAPER_INTERVAL = _int("GPULEASE_REAPER_INTERVAL", 120)
