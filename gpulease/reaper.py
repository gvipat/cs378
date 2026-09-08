"""Reconciliation loop, running as a thread inside the API process.

Level-triggered rather than a per-session timer. It reads the world as it
actually is (EC2) and the world as we believe it to be (SQLite) and drives one
toward the other. Every action is idempotent, so a missed run, a crashed run or
a duplicate run all converge to the same place.

Six cases:

  1. session expired, instances running       -> terminate them
  2. instances running, no live session       -> orphan, terminate them
  3. session live, instances gone             -> bookkeeping was wrong, fix it
  4. session PROVISIONING for far too long    -> launch wedged, tear it down
  5. some of a group's nodes running, not all -> broken cluster, kill the rest
  6. anything stopped rather than terminated  -> reclaim the root volume

Plus one that overrides all of them: once GPULEASE_DEADLINE passes, everything
tagged for the course is terminated, whatever the database believes.

Everything here terminates; nothing stops. Instances are ephemeral -- a start
always launches new ones -- so an instance the reaper is disposing of will
never be booted again, and leaving it `stopped` would only mean paying for its
root volume until somebody noticed. That is what case (6) is for: an instance
can still reach `stopped` without us (a student's `sudo poweroff` on a box
launched by an older version, a hand-stop in the console), and the volume bills
just the same.

Note what is *not* here: the GPU-hour budget, which is now the only thing
rationing anything. `db.claim` refuses a start once the budget is spent and
caps a new session's expires_at at whatever node-hours are left, so a group
that is nearly out simply gets a short lease and case (1) ends it. Budget
enforcement is that check plus that cap plus `db.accrue_and_close`; nothing
accrues mid-session and no case below reads the quota.

It can only ever *reduce* spend: nothing in here starts an instance.
"""

import json
import logging
import threading
import time

from . import aws, config, db

log = logging.getLogger("gpulease.reaper")

PROVISION_TIMEOUT = 900  # 15 min; a launch that slow is wedged, not slow
GRACE_AFTER_START = 120  # don't call an instance missing until EC2 catches up


def run_once():
    actions = []
    # One describe covering every state, split below. The stopped ones take no
    # part in the session logic -- a session's nodes are pending or running or
    # they are gone -- but they are exactly what case (6) reclaims, and asking
    # EC2 twice for the same answer would be the only other way to see them.
    everything = aws.course_instances(states=aws.STATES_ALL)
    instances = [i for i in everything if i["State"]["Name"] in ("pending", "running")]
    idle = [i for i in everything if i["State"]["Name"] in ("stopping", "stopped")]

    if config.DEADLINE and db.now() >= config.DEADLINE:
        return _sweep_deadline(everything)

    by_group = {}
    for inst in instances:
        gid = aws.instance_tag(inst, "Group")
        if gid:
            by_group.setdefault(gid, []).append(inst)

    live_groups = set()
    now = db.now()

    for row in db.all_sessions():
        if row["status"] not in db.LIVE:
            continue
        gid, aid = row["group_id"], row["assignment_id"]
        live_groups.add(gid)
        running = by_group.get(gid, [])
        started = row["started_at"] or now

        # (3) we think it is up, EC2 disagrees. Under ephemeral instances this
        # is usually a node that has genuinely gone: terminated by hand, or by
        # a student's `sudo poweroff`. Either way the session is over.
        if not running:
            if row["status"] == db.STOPPING or started < now - GRACE_AFTER_START:
                db.accrue_and_close(gid, aid, "instance not running")
                db.mark_stopped(gid, aid)
                actions.append({"group": gid, "action": "reconciled_stopped"})
            continue

        overdue = row["expires_at"] and now > row["expires_at"]
        wedged = row["status"] == db.PROVISIONING and now - started > PROVISION_TIMEOUT

        # (5) a cluster that has lost a node. The survivors bill at the full
        # rate and cannot run the job on their own -- a two-node all-reduce with
        # one node is a hang, not a slow run -- so the session ends rather than
        # quietly costing money for nothing. The grace period is what keeps a
        # still-launching cluster out of this: during PROVISIONING the nodes are
        # `pending`, which counts as running here.
        expected = db.node_count(row)
        partial = (
            len(running) < expected
            and row["status"] != db.STOPPING
            and started < now - GRACE_AFTER_START
        )

        # (1), (4) and (5), plus finishing a stop that did not complete
        if overdue or wedged or partial or row["status"] == db.STOPPING:
            reason = (
                "lease expired" if overdue
                else "provision timeout" if wedged
                else f"partial cluster ({len(running)}/{expected} nodes)" if partial
                else "stop requested"
            )
            aws.terminate_instances([i["InstanceId"] for i in running], reason)
            db.accrue_and_close(gid, aid, reason)
            db.mark_stopped(gid, aid)
            actions.append({"group": gid, "action": "reaped", "reason": reason})

    # (2) anything running that no live session claims
    for gid, insts in by_group.items():
        if gid in live_groups:
            continue
        ids = [i["InstanceId"] for i in insts]
        aws.terminate_instances(ids, "orphan: no live session")
        actions.append({"group": gid, "action": "orphan_terminated", "instances": ids})

    # (6) anything stopped instead of terminated. Nothing here creates a stopped
    # instance -- every disposal path terminates -- so one has either been
    # stopped by hand or shut down from inside a box old enough to predate
    # InstanceInitiatedShutdownBehavior=terminate. It cannot be resumed into a
    # session (a start launches fresh nodes) and its root volume bills by the
    # hour, so it is pure waste. `stopping` is included: it is on its way to the
    # same place, and terminating from there is legal and saves a pass.
    if idle:
        ids = [i["InstanceId"] for i in idle]
        aws.terminate_instances(ids, "stopped instance: nothing will ever resume it")
        actions.append({"action": "idle_terminated", "instances": ids})

    # Course-tagged but ungrouped is a bug somewhere; say so loudly.
    untagged = [i["InstanceId"] for i in instances if not aws.instance_tag(i, "Group")]
    if untagged:
        log.error("course-tagged instances with no Group tag: %s", untagged)

    if actions:
        log.info(json.dumps({"actions": actions, "instances_seen": len(instances)}))
    return actions


def _sweep_deadline(instances):
    """The assignment is over: terminate everything, believe nothing.

    `api.start` already caps every lease at the deadline, so in the normal case
    there is nothing here to do. This exists for the sessions that were already
    running when the deadline was set - their expires_at was never capped, and
    case (1) would happily let them run for hours past the cutoff.

    Works off EC2 rather than the session table on purpose: an instance whose
    row was lost still costs money, and after the deadline nothing tagged for
    this course has any business existing.

    There is no grace period and no opt-in setting any more, because there is
    nothing left to protect: a session's disk is destroyed when the session
    ends, so by the deadline there is no student work on any of these volumes
    to lose. What used to be GPULEASE_TERMINATE_AT_DEADLINE plus a day of
    grace is now what happens every time anybody stops.
    """
    actions = []
    ids = [i["InstanceId"] for i in instances]
    if ids:
        aws.terminate_instances(ids, "assignment deadline passed")
        actions.append({"action": "deadline_terminate", "instances": ids})

    for row in db.all_sessions():
        if row["status"] not in db.LIVE:
            continue
        gid, aid = row["group_id"], row["assignment_id"]
        db.accrue_and_close(gid, aid, "assignment deadline passed")
        db.mark_stopped(gid, aid)
        actions.append({"group": gid, "action": "deadline_closed"})

    if actions:
        log.info("deadline %s passed: %s", config.deadline_str(), json.dumps(actions))
    return actions


def run_forever(interval=None):
    interval = interval or config.REAPER_INTERVAL
    log.info("reaper started, every %ss", interval)
    while True:
        try:
            run_once()
        except Exception:  # never let one bad pass kill the loop
            log.exception("reaper pass failed")
        time.sleep(interval)


def start_thread():
    t = threading.Thread(target=run_forever, name="reaper", daemon=True)
    t.start()
    return t


if __name__ == "__main__":  # python -m gpulease.reaper  -> one pass, for testing
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps({"actions": run_once()}, indent=2))
