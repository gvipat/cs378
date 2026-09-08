"""Student-facing control plane.

Four endpoints, one payload shape. Every policy decision (quota, lease length,
which assignment is active) lives here rather than in the CLI, so the rules can
change mid-semester without asking anyone to upgrade.

    uvicorn gpulease.api:app --host 0.0.0.0 --port 8000

Run it with a single worker: the reaper is a thread inside this process.
"""

import logging
import uuid
from contextlib import asynccontextmanager

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Header, HTTPException

from . import aws, config, db, keys, reaper

log = logging.getLogger("gpulease.api")

# 2 was multi-node: a v1 CLI reads only `host` and would show a student one of
# their two nodes with no hint that the other exists.
#
# 3 is ephemeral instances, and is the same kind of unavoidable. A v2 CLI tells
# students "Files in /home/ubuntu survive a stop" and offers a confirmation
# prompt only when starts are rationed. Both were true and neither is: a stop
# now destroys the nodes and their disks. Leaving a v2 client working would
# mean it actively reassures a group right up to the moment their work is
# deleted, which is worse than making them download the new file.
MIN_CLI_VERSION = 3


@asynccontextmanager
async def lifespan(app):
    db.init()
    if config.REAPER_ENABLED:
        reaper.start_thread()
    yield


app = FastAPI(title="gpu-lease", lifespan=lifespan)


def caller(authorization: str = Header(default=""), x_cli_version: int = Header(default=0)):
    if x_cli_version and x_cli_version < MIN_CLI_VERSION:
        raise HTTPException(426, "Your CLI is out of date. Reinstall it and try again.")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    student = db.authenticate(token)
    if not student:
        raise HTTPException(401, "Bad or missing token. Run: gpulease login")
    return student


def session_view(row, group_id):
    """The single payload the CLI polls. Everything a student needs to know."""
    quota = config.GPU_HOUR_QUOTA
    if row is None:
        return {
            "group_id": group_id,
            "assignment_id": config.ACTIVE_ASSIGNMENT,
            "state": db.STOPPED,
            "gpu_hours_used": 0.0,
            "gpu_hours_quota": quota,
            "gpu_hours_remaining": quota,
            "starts_used": 0,
            "starts_allowed": config.MAX_STARTS,
            "node_count": config.NODES_PER_GROUP,
            "deadline": config.DEADLINE or None,
        }
    # Live usage, not the raw column: the column only moves when a session
    # closes, so a group six hours into a session would otherwise watch it sit
    # at whatever it was when they last stopped. Display only -- the budget is
    # charged at stop and checked at start.
    used = db.usage_seconds(row) / 3600.0
    view = {
        "group_id": row["group_id"],
        "assignment_id": row["assignment_id"],
        "state": row["status"],
        "gpu_hours_used": round(used, 2),
        "gpu_hours_quota": quota,
        "gpu_hours_remaining": round(max(0.0, quota - used), 2),
        "starts_used": row["starts_used"],
        "starts_allowed": config.MAX_STARTS,
        # From the row, not config: a group mid-session keeps the cluster it
        # was actually given even if the setting changes under them.
        "node_count": db.node_count(row),
        "deadline": config.DEADLINE or None,
    }
    if row["status"] in db.LIVE:
        view["expires_at"] = row["expires_at"]
        view["started_at"] = row["started_at"]
        view["session_id"] = row["session_id"]
        view["ends_because"] = _ends_because(row)
    return view


def _ends_because(row):
    """Which of the three caps decided this session's expires_at.

    claim() takes the minimum of the lease length, the deadline and the budget
    and stores one number, so the reason is not recoverable from the row -- it
    is reconstructed here by asking which cap the stored value landed on. Only
    for display: a student whose cluster dies in twenty minutes should be told
    whether that is their budget or the assignment cutoff.
    """
    expires = row["expires_at"]
    if not expires:
        return None
    if config.DEADLINE and expires >= config.DEADLINE:
        return "deadline"
    # Reconstructed against started_at, which is what claim() measured from.
    lease_end = (row["started_at"] or 0) + config.MAX_SESSION_SECONDS
    return "lease" if expires >= lease_end else "budget"


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "course": config.COURSE,
        "assignment": config.ACTIVE_ASSIGNMENT,
        "nodes_per_group": config.NODES_PER_GROUP,
        "deadline": config.deadline_str(),
    }


@app.get("/whoami")
def whoami(student=Depends(caller)):
    return {
        "student_id": student["student_id"],
        "name": student["name"],
        "group_id": student["group_id"],
        "active_assignment": config.ACTIVE_ASSIGNMENT,
        "deadline": config.DEADLINE or None,
    }


def live_view(row, group_id):
    """`session_view` plus whatever EC2 currently says, and the promotion from
    PROVISIONING to RUNNING once sshd answers. This is where a session actually
    becomes usable, so both /session and /session/start go through it."""
    view = session_view(row, group_id)
    nodes = db.session_nodes(row)
    if row is None or view["state"] not in db.LIVE or not nodes:
        return view

    gid, aid = row["group_id"], row["assignment_id"]
    described = aws.describe_many([n["instance_id"] for n in nodes])
    if not described:
        return view  # the reaper will sort this out

    # Public addresses change across a stop/start, so EC2 wins over the row.
    # Private addresses do not, but a node EC2 has lost keeps the one we
    # recorded rather than becoming None and breaking the peer list.
    fresh, states = [], []
    for node in nodes:
        inst = described.get(node["instance_id"])
        states.append(inst["State"]["Name"] if inst else "missing")
        fresh.append({
            "rank": node.get("rank", 0),
            "instance_id": node["instance_id"],
            "private_ip": (aws.private_ip(inst) if inst else None) or node.get("private_ip"),
            "host": (aws.public_host(inst) if inst else None) or node.get("host"),
        })

    view["instance_state"] = states[0]
    view["instance_states"] = states

    # Readiness is sshd answering, and it has to be sshd on EVERY node: a
    # cluster whose second node is not up yet cannot run the job the student is
    # about to be told to run. `all` short-circuits, so the usual case costs one
    # probe, not one per node.
    ready = view["state"] == db.PROVISIONING and all(aws.ssh_ready(n["host"]) for n in fresh)
    if ready:
        db.set_running(gid, aid, fresh)
        view["state"] = db.RUNNING
    elif fresh != nodes:
        db.set_nodes(gid, aid, fresh)  # keep the addresses current for the reaper

    view["nodes"] = [dict(n, state=st) for n, st in zip(fresh, states)]
    if view["state"] == db.RUNNING:
        view["user"] = "ubuntu"
        view["host"] = fresh[0]["host"]
        # Rank 0's *private* address: the nodes rendezvous inside the VPC, and
        # handing students the public one would send an all-reduce out through
        # the internet gateway and back.
        view["master_addr"] = fresh[0]["private_ip"]
        view["master_port"] = config.MASTER_PORT
        if row["private_key"]:
            view["private_key"] = row["private_key"]
    return view


@app.get("/session")
def get_session(student=Depends(caller)):
    gid, aid = student["group_id"], config.ACTIVE_ASSIGNMENT
    return live_view(db.get_session(gid, aid), gid)


@app.post("/session/start", status_code=202)
def start(student=Depends(caller)):
    gid, aid = student["group_id"], config.ACTIVE_ASSIGNMENT
    session_id = str(uuid.uuid4())
    now = db.now()

    if config.DEADLINE and now >= config.DEADLINE:
        raise HTTPException(
            403,
            f"The deadline for {aid} passed on {config.deadline_str()}. "
            f"No new sessions can be started.",
        )

    # A lease may not outlive the deadline. Without this a group starting an
    # hour before the cutoff would hold an instance well past it, and the
    # deadline sweep below would be the only thing to catch them.
    #
    # This is the cap for everything that is not the budget; claim() shortens
    # it further to whatever node-hours the group has left, off the same
    # snapshot it checks the budget against.
    expires_cap = now + config.MAX_SESSION_SECONDS
    if config.DEADLINE:
        expires_cap = min(expires_cap, config.DEADLINE)

    nodes = config.NODES_PER_GROUP
    ok, reason, existing = db.claim(
        gid, aid, session_id, student["student_id"], expires_cap,
        config.QUOTA_SECONDS, config.MAX_STARTS, nodes,
        min_start_seconds=config.MIN_START_SECONDS,
    )
    if not ok:
        if reason == "spent":
            # Only reachable when an instructor has set GPULEASE_MAX_STARTS;
            # the default is unlimited and the budget below is the ration.
            used = existing["starts_used"]
            raise HTTPException(
                429,
                f"Group {gid} has already used "
                f"{'its one session' if config.MAX_STARTS == 1 else f'all {used} of its sessions'}"
                f" for {aid}. Email the instructor if you need another.",
            )
        if reason == "quota":
            raise HTTPException(
                429,
                f"Group {gid} has used its whole {config.GPU_HOUR_QUOTA} GPU-hour budget "
                f"for {aid}, so there are no more sessions. Email the instructor if you "
                f"need more.",
            )
        if reason == "exhausted":
            # Enough budget left to be billed for, not enough to boot a cluster
            # and do anything with it. Say the number, so it is obvious this is
            # a floor and not a bug.
            # `existing` is None when the whole budget is smaller than the
            # floor -- a misconfiguration rather than a group's spending, but a
            # 429 explaining it beats a 500.
            used = (existing["gpu_seconds_used"] or 0) if existing else 0
            left = (config.QUOTA_SECONDS - used) / 3600.0
            raise HTTPException(
                429,
                f"Group {gid} has {left:.2f} of its {config.GPU_HOUR_QUOTA} GPU-hours "
                f"left for {aid}, and a session needs at least "
                f"{config.MIN_START_MINUTES:g} minutes' worth "
                f"({config.MIN_START_SECONDS * nodes / 3600.0:.2f} GPU-hours for "
                f"{nodes} node{'s' if nodes != 1 else ''}). Email the instructor if "
                f"you need more.",
            )
        # Lost the race with a groupmate: hand back the session they started,
        # complete with the host and key, so the CLI can print an ssh line
        # instead of pretending it is launching something.
        return live_view(existing, gid)

    # claim() may have shortened expires_cap to whatever the group's budget
    # allows, and the LeaseExpiresAt tag has to carry the lease they actually
    # got rather than the one they asked for.
    expires = db.get_session(gid, aid)["expires_at"]

    try:
        private_key, public_key = keys.new_keypair(f"gpulease-{gid}-{session_id[:8]}")
        db.set_key(gid, aid, private_key)
        placed = aws.launch_cluster(gid, aid, session_id, public_key, expires, nodes)
        db.set_nodes(gid, aid, placed)
    # A launch that never produced a usable instance must not cost the group a
    # start: FAILED is startable, and if an instructor has set MAX_STARTS,
    # forgetting the refund would mean one capacity blip ends their assignment.
    except aws.RetryLater as e:
        db.set_failed(gid, aid, e)
        db.refund_start(gid, aid)
        raise HTTPException(503, str(e))
    except ClientError as e:
        db.set_failed(gid, aid, e)
        db.refund_start(gid, aid)
        err = e.response.get("Error", {})
        code, message = err.get("Code", "Unknown"), err.get("Message", "")
        # The Message goes to the log, never to the response. AWS puts
        # identifiers in it -- an UnauthorizedOperation names the account, the
        # role and this host's instance id -- and the caller here is a student.
        # It is still logged on its own line, because an InvalidParameterValue
        # that does not say WHICH parameter costs a round trip through
        # journalctl and the instructor is the one who can fix the config.
        #
        # The Code carries no identifiers and is the half that tells a student
        # whether to retry, so that is what goes back, with a reference tying
        # it to the log line.
        ref = session_id[:8]
        log.error("launch failed for group %s [ref %s]: %s: %s", gid, ref, code, message)
        log.exception("launch failed for group %s [ref %s]", gid, ref)
        raise HTTPException(
            500, f"Launch failed ({code}). Tell the instructor and quote ref {ref}."
        )
    except Exception as e:  # noqa: BLE001 - never strand the row in PROVISIONING
        db.set_failed(gid, aid, e)
        db.refund_start(gid, aid)
        log.exception("launch failed for group %s", gid)
        raise HTTPException(500, "Launch failed. Tell the instructor.")

    return session_view(db.get_session(gid, aid), gid)


@app.post("/session/stop")
def stop(student=Depends(caller)):
    gid, aid = student["group_id"], config.ACTIVE_ASSIGNMENT
    row = db.get_session(gid, aid)
    if not row or row["status"] not in db.LIVE:
        return {
            "stopped": False,
            "message": "Nothing was running.",
            **session_view(row, gid),
        }

    instance_ids = db.node_instance_ids(row)
    duration = db.accrue_and_close(gid, aid, f"stopped by {student['student_id']}")
    # Terminate, not stop: a stopped instance bills for its root volume around
    # the clock, and the next `start` launches fresh nodes rather than resuming
    # this one, so there would never be a reader for that disk again.
    aws.terminate_instances(instance_ids, "student requested")
    db.mark_stopped(gid, aid)

    count = len(instance_ids)
    return {
        "stopped": True,
        "session_duration_seconds": duration,
        "message": (
            f"{count} instance{'s' if count != 1 else ''} shutting down for good. "
            f"The disk{'s' if count != 1 else ''} go{'' if count != 1 else 'es'} with "
            f"{'them' if count != 1 else 'it'}; `start` gives you clean nodes."
            if count
            # A live session with nothing recorded against it: the reaper lost
            # the race, or the launch died between claiming and set_nodes.
            else "Session closed. No instances were recorded against it."
        ),
        **session_view(db.get_session(gid, aid), gid),
    }
