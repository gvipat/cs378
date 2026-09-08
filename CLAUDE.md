# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

`gpu-lease`: student groups run `gpulease start` and get `GPULEASE_NODES_PER_GROUP`
EC2 GPU instances (default 2, for the distributed-training assignment);
they stop themselves when the group stops using them. The entire control plane is **one
process on one EC2 instance** — FastAPI + a SQLite file + a reaper thread —
installed by `setup.sh`.

**Instances are ephemeral.** Every start launches new ones; every way a session
can end terminates them, and `DeleteOnTermination` takes the root volumes with
them. Nothing of a group's persists between sessions, groups start again as
often as they like, and `GPULEASE_GPU_HOUR_QUOTA` is the only thing rationing
anything. If you find a claim that instances are stopped-not-terminated, that a
session resumes a group's existing box, that student files survive a stop, or
that `GPULEASE_MAX_STARTS` is the ration, it is stale — fix it.

An earlier version of this was Lambda + DynamoDB + Terraform. It is archived in
`legacy-serverless.tar.gz` and is **not** the current design. If you find a
reference to Lambda, DynamoDB, Terraform, SSM parameters, launch templates, or
a "backstop" Lambda anywhere in this tree, it is stale — fix it.

## Layout

```
gpulease/
  config.py     all config, from gpulease.env; nothing else reads os.environ
  db.py         schema, auth, the session state machine, the atomic claim
  aws.py        every EC2 call; nothing else imports boto3
  userdata.py   the cloud-init payload for a student instance
  keys.py       per-session ed25519 keys, via ssh-keygen (no crypto dep)
  api.py        FastAPI: /healthz /whoami /session /session/start /session/stop
  reaper.py     reconciliation; a thread inside the API process
cli/gpulease.py student CLI. stdlib-only, Python 3.9+, MUST stay AWS-free
admin.py        instructor tools (roster, sessions, instances, kill, reap,
                disable/enable)
setup.sh        the installer; idempotent, safe to re-run
iam-policy.json permissions for the lease host
Caddyfile.example the TLS terminator in front of the API; copy to /etc/caddy
gpulease.env    local, gitignored; gpulease.env.example is the template
gpulease.env.test  t3.micro + minutes, for the manual checklist. Selected with
                GPULEASE_ENV=gpulease.env.test, which REPLACES gpulease.env
                rather than layering on it. Its GPULEASE_COURSE is deliberately
                not the production one -- that tag is the blast radius for every
                describe and terminate -- which means the host's IAM policy needs
                both tags (StringEquals takes a list). Do not "fix" the mismatch
                by pointing it at the real course.
var/gpulease.db the whole system state
```

## Deployment shape

One EC2 "lease host" (`t3.small`, Ubuntu) runs the service under systemd as the
`ubuntu` user, out of the repo directory. It carries an instance profile built
from `iam-policy.json`; that role is the only AWS credential anywhere in the
system. The student instances it launches carry **no** instance profile.

The API binds to `127.0.0.1` and **Caddy terminates TLS in front of it**
(`Caddyfile.example` → `/etc/caddy/Caddyfile`, README → "Put TLS in front").
Only 22, 80 and 443 are open on the lease host; 8000 is not. This is not
optional polish: `/session` returns the group's SSH *private key* in the
response body and the CLI polls it every five seconds, so a plaintext listener
hands an on-path observer a shell on a group's nodes and a token that can
destroy another group's nodes and everything on their disks. If you ever find
`GPULEASE_HOST=0.0.0.0` and no terminator, that is the bug.

Three constraints that are easy to violate and annoying to debug:

- **The lease host must be in the same region and VPC as the instances it
  launches.** It probes them on port 22 and authorizes itself into their
  security group by its own group id, which is a single-VPC reference.
  `_self_sources()` checks the VPCs match and drops the group reference if they
  do not, because a rejected reference would fail every launch.
- **The subnets must auto-assign public IPs.** `run_instances` passes a
  top-level `SubnetId`, which forbids a `NetworkInterfaces` block, so the
  instance cannot request a public IP for itself.
- **`GPULEASE_HOST` and `GPULEASE_PORT` are not applied by a restart.**
  `setup.sh` interpolates *both* into the systemd unit's `ExecStart` at install
  time, so changing either in `gpulease.env` and restarting leaves the old
  value. Re-run `sudo ./setup.sh`. Every *other* setting in that file is a
  restart. The port is the nastier of the two: a stale bind address usually
  fails loudly, while a stale port leaves the service healthy on one number and
  Caddy proxying to another, which surfaces only as a 502 with no clue in
  either service's log as to which end is wrong.
- **Capacity has to exist for a group's whole cluster in one AZ.** All of a
  group's nodes come from one `RunInstances` with `MinCount == MaxCount`, so a
  subnet that can place one but not two fails and we move to the next.

Full install and teardown steps are in `README.md`; keep them in sync when the
config surface changes.

## Commands

```bash
# Local dev (no AWS needed for /healthz and auth; anything touching EC2 will fail)
GPULEASE_DB=/tmp/dev.db GPULEASE_REAPER=0 .venv/bin/uvicorn gpulease.api:app --reload

# The manual checklist, on t3.micro. `db init` first: setup.sh only ever
# initialises gpulease.env's database, so admin reads otherwise die with
# `no such table: sessions`.
export GPULEASE_ENV=gpulease.env.test
python -m gpulease.db init

# On the lease host
sudo ./setup.sh                     # install or re-install after a git pull
sudo systemctl restart gpulease     # after editing gpulease.env (except HOST/PORT)
journalctl -u gpulease -f

./admin.py roster roster.csv        # mint tokens -> tokens.csv
./admin.py sessions                 # database view
./admin.py instances                # EC2 view
./admin.py reap                     # one reconciliation pass
./admin.py kill --group 7           # end a live session now; terminates
./admin.py disable abc123           # revoke one token; `enable` restores it
./admin.py budget 7 --hours 0       # reset a group's used node-hours
./admin.py grant 7                  # only meaningful if MAX_STARTS is set
./admin.py terminate                # sweep up leftovers; irreversible
./admin.py preflight                # check the host's IAM permissions

curl -s localhost:8000/healthz      # on the host; the API binds to localhost
sudo systemctl reload caddy         # after editing /etc/caddy/Caddyfile

# CLI
GPULEASE_API=https://host python3 cli/gpulease.py login <token> | start | status | stop
```

**There is no automated test suite.** `README.md` → "Verifying a deployment" is
the manual checklist: concurrent start, quota exhaustion, reaper idempotency,
orphan cleanup, reclaiming a *stopped* instance, deadline enforcement, budget
exhaustion and the budget-capped lease, that a stop really destroys the nodes
and the next start builds new ones, readiness timing, and the multi-node cases
(one subnet, peers over IMDS, cross-group isolation, partial cluster).

## Invariants worth preserving

**The student CLI never touches AWS.** Students have no AWS accounts. It is
stdlib-only, authenticates with a course bearer token, and gets its SSH key from
the API. Do not add boto3, the `aws` CLI, or a subprocess call to it.

**Only `api.py` starts instances.** `reaper.py` must stay structurally incapable
of increasing spend — it has no launch path. It is level-triggered and every
action is idempotent, so a missed, crashed or duplicated pass all converge.

**`db.claim()` is the load-bearing function.** One `BEGIN IMMEDIATE`
transaction enforces the GPU-hour budget and any start limit, decides how long
the lease may be, blocks a duplicate launch by a second group member, records
how many nodes the session gets, and claims the session. All of it must be
decided against one snapshot — two simultaneous requests each concluding the
group had budget for a session, and launching one apiece, is exactly the bug
this prevents. The race loser gets the existing session (200, not an error).
This is the most likely place for a real concurrency bug.

**The hour budget is enforced by the lease, not by a reaper case.** `claim()`
stores `expires_at = min(expires_cap, now + remaining // node_count)`, so a
group with forty node-minutes left gets a twenty-minute two-node lease and the
reaper's ordinary "lease expired" path (case 1) ends and bills it. `api.start`
owns `expires_cap` — `MAX_SESSION_HOURS` ∧ the deadline — and `claim()`
shortens it, because the budget must come off the same snapshot as the quota
check. Nothing accrues mid-session: `db.accrue_and_close` is still the only
writer of `gpu_seconds_used`, charged once when the session ends. Do not add a
periodic flush or a reaper case that reads the quota; the cap is what makes
neither necessary, and a second writer is how the column starts double-counting.

`db.usage_seconds()` is display only — `session_view` and `admin.py sessions`,
so a live session's hours are not frozen at the last stop. Its arithmetic must
stay identical to `accrue_and_close`, or students watch a number they are not
billed. Nothing that makes a decision may call it.

`GPULEASE_MIN_START_MINUTES` refuses a start whose *lease* would be shorter
than it, returning reason `"exhausted"` rather than `"quota"`. Booting costs
budget, so a lease shorter than this is billable and useless. It is compared
against `remaining // node_count`, not against `remaining`: at two nodes a
group needs twice the node-minutes to clear the same floor, which is the figure
`api.start`'s refusal message and `admin.py sessions` have always quoted.

**`db.set_usage()` is the instructor's budget override** (`admin.py budget`) and
the only other thing that writes `gpu_seconds_used`. It refuses while the
session is LIVE, because `accrue_and_close` is about to add that session's time
to whatever it finds and would undo the correction.

**`GPULEASE_MAX_STARTS` is an optional cap on top of the budget**, default 0 =
unlimited. Sessions are disposable, so a group that loses one to a capacity
error or a mistaken `stop` simply starts again; the hour budget is what
eventually says no. `starts_used` still increments inside `claim()` and
`db.refund_start()` still gives it back on every launch-failure path in
`api.start` — mostly so the counter means "sessions this group actually got",
but load-bearing the moment somebody sets a limit, so refund on any new failure
path you add. `admin.py grant` is the override when a limit is set;
`admin.py budget` is the one you actually reach for.

**`db.init()` runs `_migrate()`.** `CREATE TABLE IF NOT EXISTS` does nothing to
an existing table, so every added column needs a line there. There are live
databases; a schema change without a migration breaks them on the next
`setup.sh`.

**Student instances hold no AWS credentials.** No instance profile, no SSM, no
AWS CLI on the box. Three mechanisms make that possible, and all are easy to
break by accident:

- The session public key travels in cloud-init user-data, inside `bootcmd` —
  the one cloud-init module with `ALWAYS` frequency. Every instance is new, so
  the first boot is the one that matters and a per-instance module would now do;
  `bootcmd` stays because it also restores the key and the peer files after a
  student reboots mid-session. Keep it there.
- Instances launch with `InstanceInitiatedShutdownBehavior=terminate`, so a
  student's `sudo poweroff` destroys their own node rather than leaving a
  stopped one billing for a volume nothing will ever read. They start again.
- The peer list reaches the box as *instance tags read over IMDS*, which needs
  no credential. See the multi-node invariants below.

**Nothing gpulease-specific runs on a student instance.** There was an idle
watchdog; it was removed deliberately. The instance gets an SSH key, an motd,
and a few static files describing its peers (`/etc/gpulease/peers`,
`/etc/gpulease/hostfile`, `/etc/hosts` entries, `/etc/profile.d`) — data
written once at boot, not a process. No agent, no timer, nothing that phones
home; the peer data comes from link-local IMDS, not from the control plane. So:

- **`GPULEASE_MAX_SESSION_HOURS` bounds a session**, enforced by
  the reaper on the lease host — as does whatever is left of
  `GPULEASE_GPU_HOUR_QUOTA`, through the same `expires_at`. Do not move any cost control onto the instance:
  students have root there and can stop anything that runs on it. The setting
  is a wall-clock lease length and `NODES_PER_GROUP` does not change it — what
  it multiplies is the *bill*: the worst case is
  `groups x nodes x hours x $/hour`, so raising the node count raises the bill
  by exactly that factor with no change to how long a lease lasts.
- **`GPULEASE_GPU_HOUR_QUOTA` is the hour budget and the ration.** Starts are
  unlimited by default, so this is the only thing that ever refuses a group.
  It is counted in node-hours, which is why the worst case for the course is
  `groups x QUOTA x $/hour` with no node factor — a second node makes a group
  spend the budget twice as fast, not spend twice as much of it. It goes inert
  only if somebody sets `MAX_STARTS=1`, where a group that may start once is
  checked once; do not present it as a live control in that config.

**`bootcmd` runs before cloud-init's `users-groups` module**, so on a *first*
boot the `ubuntu` user does not exist yet — hence the `id -u ubuntu` guard, with
first-boot key installation covered by `ssh_authorized_keys` in the same
cloud-config. `set -u` **without** `-e` for the same family of reasons: the
steps are independent and one failure should not skip the rest — including the
IMDS peer lookup, which is allowed to come up empty and leave a working
single-node box rather than aborting the whole script.

**Readiness is a port-22 probe from the lease host**, not EC2 status checks,
which go green before sshd accepts connections. `aws.security_group_id()`
therefore authorizes this host into the student security group — by its *own
security group id*, plus its private and public IPs. The private address is the
one that matters: inside a VPC a public DNS name resolves to the private IP, so
the probe arrives from the private address. It also **revokes** tcp/22 IPv4
rules that are no longer in `GPULEASE_ALLOWED_SSH_CIDRS`, so narrowing that
setting actually closes the old range. Keep the reconcile; add-only was a bug.

**Terminating is how every session ends, and `aws.terminate_instances()` is the
only disposal primitive.** There is no `stop_instances`; do not add one back.
`api.stop`, all six reaper cases, the deadline sweep, `admin.py kill` and
`admin.py terminate` all go through it, and `DeleteOnTermination` takes the root
volume every time. A stopped instance is the failure mode this design exists to
prevent: it bills for a disk that the group's next start — which launches fresh
nodes — will never look at.

That is why `iam-policy.json` no longer carries `ec2:StartInstances`,
`ec2:StopInstances`, `ec2:ModifyInstanceAttribute` or
`ec2:ModifyInstanceMetadataOptions`. All four existed for the resume path.
`ec2:TerminateInstances` is scoped on `ec2:ResourceTag/Course` like everything
else, and is now a routine permission rather than a guarded one.

**Reaper case (6) terminates anything found `stopped` or `stopping`.** Nothing
in the system creates a stopped instance, so one has been stopped by hand or
shut down from inside a box predating `InstanceInitiatedShutdownBehavior=
terminate` — either way it is a volume nobody will ever read again. `run_once()`
makes one `STATES_ALL` describe and splits it: pending/running drive the session
logic, stopping/stopped go to case (6). Do not narrow that describe back to
pending+running; the stopped ones would then be invisible and bill forever.

**`aws.describe_many()` falls back to describing one at a time.** A single
unknown instance id fails the whole `DescribeInstances` batch, so a node that
has been terminated behind the service's back would otherwise hide the rest of
its cluster — which is still running and still billing, and which the reaper
needs to see in order to destroy. Keep the fallback.

**Files that hold credentials are created `0600` at `os.open` time**, never
opened-then-chmod'ed: `var/gpulease.db`, `tokens.csv`, the CLI's saved token,
and the CLI's session key. The window in between is enough to leak on a shared
lab machine.

**`run_instances` passes a `ClientToken`** so a timed-out-then-retried launch
cannot bill for two clusters. It is `{session_id}-{subnet attempt}`: per
attempt, because reusing one across a genuine cross-AZ retry is an
`IdempotentParameterMismatch`, and safe across sessions because `session_id` is
new for every start. If you ever add a second `RunInstances` under one session,
it needs its own salt.

**IAM scoping in `iam-policy.json`**: every terminate and tag action is
conditioned on `ec2:ResourceTag/Course`, and `RunInstances` on
`aws:RequestTag/Course`, so nothing this service creates can escape the scope of
what it may destroy.

**`GPULEASE_DEADLINE` is parsed at import, not on use.** A malformed value is
a `SystemExit` at boot rather than a cost control that silently never fires,
and a value with no UTC offset is read as UTC — which a syllabus date almost
never is. Consequences: it is a restart to change, and
`gpulease.env.example` ships it **blank** on purpose. `setup.sh` copies that
file to `gpulease.env` when there is none and then starts the service on it, so
a date filled in there is live on first boot — and a wrong one is a date on
which every group's session ends and no new one may start. Do not "helpfully"
fill it in; stamp it at deploy time.

**`GPULEASE_DEADLINE` is enforced in three places, and needs all three.**
`api.start` refuses new sessions past it *and* caps `expires_at` so no lease
outlives it; `reaper._sweep_deadline()` terminates everything tagged for the
course once it passes. The sweep is not redundant with the cap: sessions already
running when the deadline was configured have an uncapped `expires_at`, and it
works off EC2 rather than the session table because an instance whose row was
lost still costs money. It is handed the full `STATES_ALL` describe, so a
stopped leftover goes with the rest.

**A group's nodes come from ONE `run_instances` call**, `MinCount == MaxCount
== n`. That single call is what guarantees they share a subnet and an AZ, and
it makes the launch all-or-nothing. Do not turn it into a loop of single
launches to "improve" capacity handling: the nodes would scatter across AZs
(cross-AZ traffic is billed per GB, and an all-reduce moves a lot of GB), and a
partial launch would bill for instances that cannot run the job. There is now
exactly one `RunInstances` per session — no top-up path — so this is the only
place a node is ever created.

**Nodes learn about each other through IMDS tags, not credentials.** After
launch, `aws.launch_cluster` tags each instance with `NodeIndex` and
`NodePeers`, and instances launch with `InstanceMetadataTags: enabled` so
`bootcmd` can read them back over link-local. This is what keeps "student
instances hold no AWS credentials" true while still giving them a peer list.
Do not replace it with a call back to the control plane, an instance profile,
or an agent. One consequence worth remembering: the tags land a moment *after*
`RunInstances` returns, so a boot can lose the race — and every boot is a first
boot now, since nothing is ever resumed. The boot script retries for ~40s and
treats a miss as non-fatal.

**Every node gets identical user-data**, so one session key opens the whole
cluster and `ssh -A` hops between them. Do not personalise the payload per
node — rank comes from IMDS at boot, and identical payloads are what let a
single `RunInstances` produce the whole cluster.

**`{course}-cluster-{group}` is one security group per group**, allowing all
traffic from itself; `{course}-instances` still carries only ssh. Do not merge
them into one shared allow-all group: that would let every group reach every
other group's nodes on every port. `_group_sg_name()` sanitises the group id
out of the roster CSV and appends a hash when sanitising changed anything, so
two different group ids cannot collapse onto one network. The hash is joined
with a **dot**, which the sanitiser can never emit — joining with `-` left
group `team/a` colliding with a group literally named `team-a-<that hash>`,
i.e. two groups sharing one all-traffic network. Keep the separator outside the
sanitised alphabet. The security-group *description* uses the sanitised name
too: descriptions take a restricted character set, and a roster id with `#` in
it would otherwise fail every launch for that group.

**`aws.vpc_id()` derives the VPC from `GPULEASE_SUBNET_IDS`** when it is set,
falling back to the default VPC. Security groups have to be created in the same
VPC as the subnets we launch into; pinning them to the default VPC while
launching elsewhere fails every launch. Subnets spanning two VPCs is rejected
outright — a group's nodes must share a subnet, so a set we cannot reason about
as one network is a config error.

**The reaper destroys a partial cluster.** `len(running) < row["node_count"]`
after the grace period means terminate the survivors and close the session. A
two-node all-reduce with one node hangs rather than running slowly, so the
survivor bills in full for nothing. `db.node_count(row)` reads the row, never
config: a group mid-session keeps the cluster it was given. The way back in is
simply `gpulease start`, which builds a new cluster; refund the hours with
`admin.py budget` if the failure was not theirs.

**Billing is node-seconds.** `db.accrue_and_close` multiplies elapsed wall
clock by the row's `node_count` before adding to `gpu_seconds_used`, and
returns the wall-clock figure for display. A quota counted in wall-clock hours
would stop meaning anything the moment a session became two instances.

**`db.set_nodes()` is the only writer of `nodes`, `instance_id` and `host`.**
`instance_id`/`host` are rank 0, kept in step with the JSON list; two
independent representations of one fact is how they drift. `db.session_nodes()`
falls back to the singular columns for rows written before multi-node, which is
why the migration adds columns without rewriting any data.

**Config lives server-side.** All policy (quota, lease length, active
assignment) is in `gpulease.env`. The CLI is intentionally dumb;
`MIN_CLI_VERSION` + the `X-CLI-Version` header force an upgrade only when
genuinely unavoidable — v1 read only `host` and would show a group one of their
two nodes with no hint the other exists; it is at **3** because a v2 CLI tells
students their files survive a stop, and reassuring a group right up to the
moment their disks are deleted is worse than making them download the new file.

Every setting is tabulated in README → "Configuration reference"; keep that
table and `gpulease.env.example` in step with `config.py` when you add one.
Two deliberate mismatches live there and are not bugs to tidy. The code
defaults to `t3.micro` and a 30 GB root while the example ships `g4dn.xlarge`
and 100 GB, because a missing config file should not launch a GPU fleet. For
the same reason `COURSE` defaults to something that is *not* this deployment's
tag (`utcs378`, which is what the example and the README walkthrough ship): an
unconfigured install must not inherit the live course's blast radius, and since
the host's IAM policy is scoped to the real tag it is denied at
`CreateSecurityGroup` instead of quietly destroying real students' instances.
That denial reads "no identity-based policy allows the
ec2:CreateSecurityGroup action" and is almost always a tag mismatch rather than
a missing permission.
`NODES_PER_GROUP` is validated at import against `MAX_NODES_PER_GROUP` (8) and
refuses to start outside it — a typo there is a bill, not a warning.

## Data model

Three tables in `var/gpulease.db` (`db.SCHEMA`):

- `students` — pk `student_id`; `token_sha256`, `group_id`, `name`, `disabled`.
  Tokens are `<student_id>.<secret>`; the prefix makes lookup a primary-key hit.
  Only `sha256(secret)` is stored, compared with `hmac.compare_digest`.
- `sessions` — pk `(group_id, assignment_id)`. **One row per group per
  assignment**, however many instances that session is. Holds the live
  session's key, its `node_count`, and `nodes`: a JSON list of
  `{rank, instance_id, private_ip, host}`. `instance_id`/`host` mirror rank 0.
- `session_log` — append-only history, written on start and on every stop.

State machine: `STOPPED → PROVISIONING → RUNNING → STOPPING → STOPPED`, plus
`FAILED`. `STARTABLE = (STOPPED, FAILED)`; `LIVE = (PROVISIONING, RUNNING,
STOPPING)`. `STOPPED` means the group has **no instances**, not instances that
are switched off — they are terminated on the way out, so a start from there
builds a new cluster. The name is kept because it is what students, the CLI and
every existing row already say.
