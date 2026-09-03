"""Reconciliation loop, running as a thread inside the API process.

Level-triggered rather than a per-session timer. It reads the world as it
actually is (EC2) and the world as we believe it to be (SQLite) and drives one
toward the other. Every action is idempotent, so a missed run, a crashed run or
a duplicate run all converge to the same place.

Five cases:

  1. session expired, instances running      -> stop them
  2. instances running, no live session      -> orphan, stop them
  3. session live, instances gone or stopped -> bookkeeping was wrong, fix it
  4. session PROVISIONING for far too long   -> launch wedged, tear it down
  5. some of a group's nodes running, not all -> broken cluster, stop the rest

Plus one that overrides all of them: once GPULEASE_DEADLINE passes, everything
tagged for the course gets stopped, whatever the database believes.

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
    instances = aws.course_instances(states=("pending", "running"))

    if config.DEADLINE and db.now() >= config.DEADLINE:
        return _sweep_deadline(instances)

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

        # (3) we think it is up, EC2 disagrees
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
            aws.stop_instances([i["InstanceId"] for i in running], reason)
            db.accrue_and_close(gid, aid, reason)
            db.mark_stopped(gid, aid)
            actions.append({"group": gid, "action": "reaped", "reason": reason})

    # (2) anything running that no live session claims
    for gid, insts in by_group.items():
        if gid in live_groups:
            continue
        ids = [i["InstanceId"] for i in insts]
        aws.stop_instances(ids, "orphan: no live session")
        actions.append({"group": gid, "action": "orphan_stopped", "instances": ids})

    # Course-tagged but ungrouped is a bug somewhere; say so loudly.
    untagged = [i["InstanceId"] for i in instances if not aws.instance_tag(i, "Group")]
    if untagged:
        log.error("course-tagged instances with no Group tag: %s", untagged)

    if actions:
        log.info(json.dumps({"actions": actions, "instances_seen": len(instances)}))
    return actions


def _sweep_deadline(instances):
    """The assignment is over: stop everything, believe nothing.

    `api.start` already caps every lease at the deadline, so in the normal case
    there is nothing here to do. This exists for the sessions that were already
    running when the deadline was set - their expires_at was never capped, and
    case (1) would happily let them run for hours past the cutoff.

    Works off EC2 rather than the session table on purpose: an instance whose
    row was lost still costs money, and after the deadline nothing tagged for
    this course has any business running.
    """
    actions = []
    ids = [i["InstanceId"] for i in instances]
    if ids:
        aws.stop_instances(ids, "assignment deadline passed")
        actions.append({"action": "deadline_stop", "instances": ids})

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
