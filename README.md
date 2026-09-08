# gpu-lease

Lets student groups take GPU EC2 instances for a while and hands them back
automatically, so nobody has an AWS account and nobody leaves a box running
over the weekend.

A group's lease is `GPULEASE_NODES_PER_GROUP` instances (default 2, for the
distributed-training assignment). They land in one subnet, share one session
SSH key, and can reach each other on every port.

**Instances are disposable.** Every `start` launches brand new ones; every stop
— by the student, by the reaper, at the deadline — terminates them and their
root volumes. Nothing of a group's survives between sessions, so the course
pays for EBS only while a session is actually running, and groups start again
as often as their GPU-hour budget allows.

The whole control plane is one process on one EC2 instance: a FastAPI service,
a SQLite file, and a reconciliation loop running as a thread inside it. Install
it with one script.

```
students ──HTTP+token──▶  lease host  ──boto3──▶  group 7: node0 ── node1
   (cli/gpulease.py)      (api + sqlite + reaper)   group 8: node0 ── node1
                                                    (no AWS creds of their own)
```

---

## Before you start

- An AWS account, and the AWS CLI on your laptop with credentials that can
  create IAM roles and EC2 instances. Everything in step 1 and 2 runs from your
  laptop; everything after runs on the lease host.
- A region with a **default VPC** whose subnets auto-assign public IPs. That is
  the out-of-the-box setup. Without one, set `GPULEASE_SUBNET_IDS` to subnets
  that do assign public IPs.
- **The lease host must live in that same region and VPC** as the instances it
  launches. It probes port 22 to detect readiness and authorizes itself into
  the student security group by its own group id, which only works in one VPC.
- **Check your vCPU quota for the GPU family you intend to use.** A fresh
  account often has zero, and `RunInstances` fails with `VcpuLimitExceeded`.
  Service Quotas → EC2 → "Running On-Demand G and VT instances". The quota
  counts vCPUs, not instances: `g4dn.xlarge` is 4 each, so 20 groups × 2 nodes
  needs 160. Requests can take a day, so do this first.

Pick your values once; the commands below use them:

```bash
COURSE=utcs378
REGION=us-west-2
KEYPAIR=gpu-lease         # NAME of an EC2 key pair, not a path to a .pem
KEYFILE=~/.ssh/gpu-lease.pem   # the matching private key on this machine
```

Everything below builds on these, and step 2 adds `$HOST_ID`, `$HOST_IP` and
`$HOST_SG`. They live in this shell only — if you come back in a new terminal,
re-derive them:

```bash
HOST_ID=$(aws ec2 describe-instances --region $REGION \
  --filters 'Name=tag:Name,Values=gpulease-host' \
            'Name=instance-state-name,Values=pending,running,stopped' \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
HOST_IP=$(aws ec2 describe-instances --region $REGION --instance-ids $HOST_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
HOST_SG=$(aws ec2 describe-security-groups --region $REGION \
  --group-names gpulease-host --query 'SecurityGroups[0].GroupId' --output text)
```

`KEYPAIR` is the *registered name* of an EC2 key pair, which is what
`--key-name` wants — passing `~/gpulease.pem` gets you
`InvalidKeyPair.NotFound`. Key pairs are per-region, so it must exist in
`$REGION`. Check, or make one:

```bash
aws ec2 describe-key-pairs --region $REGION --query 'KeyPairs[].KeyName' --output text

# only if you need a new one:
mkdir -p ~/.ssh
(umask 077; aws ec2 create-key-pair --region $REGION --key-name $KEYPAIR \
   --query KeyMaterial --output text > $KEYFILE)
```

Write it to `$KEYFILE` rather than to a path you pick here, or the rest of this
guide's `ssh -i $KEYFILE` lines point at a file that does not exist. The `umask`
is not decoration either: a plain redirect creates the file world-readable and
only narrows it afterwards, and on a shared machine that window is enough to
lose the key. This leaves it `0600`, which is what `ssh` wants.

This key is yours and only lets you into the lease host. gpulease never reads
or manages it. **Student access uses a completely separate mechanism** — see
"Two key systems" below — so do not reuse this key for anything else.

## Install

**1. Create the role the lease host will run as.**

```bash
sed "s/REPLACE_COURSE_TAG/$COURSE/" iam-policy.json > /tmp/gpulease-policy.json

cat > /tmp/gpulease-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

aws iam create-role --role-name gpulease-host \
  --assume-role-policy-document file:///tmp/gpulease-trust.json
aws iam put-role-policy --role-name gpulease-host \
  --policy-name gpulease --policy-document file:///tmp/gpulease-policy.json
aws iam create-instance-profile --instance-profile-name gpulease-host
aws iam add-role-to-instance-profile \
  --instance-profile-name gpulease-host --role-name gpulease-host
```

This is the only AWS credential in the system. Students get none, and neither
do the instances it launches.

The conditions in that policy are the containment story, so read them before
loosening anything: every terminate and tag action requires
`ec2:ResourceTag/Course` to match, and `RunInstances` requires
`aws:RequestTag/Course` to be set. Together they mean the service cannot create
anything outside the scope of what it is allowed to destroy, and cannot touch a
single instance in the account that it did not create.

**2. Launch the lease host.** `t3.small` is plenty — this thing is idle 99% of
the time. Ubuntu 22.04 or 24.04.

```bash
MYIP=$(curl -s https://checkip.amazonaws.com)

HOST_SG=$(aws ec2 create-security-group --region $REGION \
  --group-name gpulease-host --description "gpu-lease control plane" \
  --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region $REGION --group-id $HOST_SG \
  --protocol tcp --port 22 --cidr $MYIP/32          # you
aws ec2 authorize-security-group-ingress --region $REGION --group-id $HOST_SG \
  --protocol tcp --port 80 --cidr 0.0.0.0/0         # ACME challenge + redirect
aws ec2 authorize-security-group-ingress --region $REGION --group-id $HOST_SG \
  --protocol tcp --port 443 --cidr 0.0.0.0/0        # your students

AMI=$(aws ec2 describe-images --region $REGION --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*' \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)

HOST_ID=$(aws ec2 run-instances --region $REGION --image-id $AMI \
  --instance-type t3.small \
  --key-name $KEYPAIR --security-group-ids $HOST_SG \
  --iam-instance-profile Name=gpulease-host \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=gpulease-host}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region $REGION --instance-ids $HOST_ID
HOST_IP=$(aws ec2 describe-instances --region $REGION --instance-ids $HOST_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "lease host $HOST_ID at $HOST_IP"
```

If `run-instances` returns `Invalid IAM Instance Profile name`, that is just
propagation lag on the profile you made a moment ago — wait a few seconds and
retry that one command.

Note there is no rule for port 8000. The service binds to localhost and Caddy
is the only thing that talks to it (step 5); a plaintext port left open beside
the TLS one would defeat the point of having TLS. Narrowing 443 to your campus
range is still worth doing if every student is on it, but once TLS terminates
here it is no longer the thing holding the door shut.

The port-22 rule pins your *current* address; if SSH starts hanging weeks
later, that rule is the first thing to check, not the host.

`$HOST_IP` changes if you ever stop and start the host. Before you hand the URL
to students, give it a stable address:

```bash
aws ec2 associate-address --region $REGION --instance-id $HOST_ID \
  --allocation-id $(aws ec2 allocate-address --region $REGION --domain vpc \
                      --query AllocationId --output text)
HOST_IP=$(aws ec2 describe-instances --region $REGION --instance-ids $HOST_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "lease host now reachable at $HOST_IP"
```

**3. Copy this tree onto the host.** It is not a git repo yet, so either
publish it and clone, or just copy it:

```bash
rsync -a -e "ssh -i $KEYFILE" --exclude .venv --exclude var --exclude '*.tar.gz' \
  ./ ubuntu@$HOST_IP:~/gpulease/
```

Or, if you built a tarball: `scp -i $KEYFILE gpulease.tar.gz ubuntu@$HOST_IP:~`
then `tar xzf gpulease.tar.gz` on the host.

Passing `-i $KEYFILE` matters — without it `ssh` offers your default keys and
the host answers `Permission denied (publickey)`. To stop repeating it, add a
`Host` block to `~/.ssh/config` with `HostName`, `User ubuntu` and
`IdentityFile`, and the rest of this section becomes `ssh lease`.

**4. Configure, then install.** Write the config *before* the first
`setup.sh` — it creates the security group for student instances named after
`GPULEASE_COURSE`, and changing the course tag later just leaves a stale one
behind.

```bash
ssh -i $KEYFILE ubuntu@$HOST_IP
cd gpulease
cp gpulease.env.example gpulease.env
nano gpulease.env         # course, region, instance type, quotas, assignment
sudo ./setup.sh
```

`GPULEASE_COURSE` and `GPULEASE_REGION` **must match the `$COURSE` and
`$REGION` you used above**. The course tag is what the IAM policy conditions
on, so a mismatch means every EC2 call is denied — that is the first thing to
check if preflight fails. Likewise, if you change `GPULEASE_PORT`, point the
`reverse_proxy` line in your Caddyfile at the new port.

**Read `GPULEASE_DEADLINE` before you save the file.** It ships blank, and
`setup.sh` will happily start the service on the example verbatim, so whatever
is in there on first boot is live. Once that timestamp passes, every `gpulease
start` is refused with a 403 and the reaper destroys the whole course's fleet on
its next pass — correct behaviour, deeply confusing if you did not know it was
set. Put your own date in it, once, after checking the UTC offset. Every setting
is listed under "Configuration reference" below.

`setup.sh` installs dependencies, creates the database, checks the host's AWS
permissions, and starts the service under systemd. It ends by printing the API
URL. It is idempotent: re-run it after pulling new code, and it will never
touch `gpulease.env` or the database.

If the permissions check reported failures, fix them and
`sudo systemctl restart gpulease`. `./admin.py preflight` re-runs just that
check.

`/healthz` needs no token and is the fastest way to confirm the service is up
and reading the config you think it is. Run it **on the host** — the service is
bound to localhost until you put Caddy in front of it in the next step:

```bash
curl -s http://127.0.0.1:8000/healthz
# {"ok":true,"course":"utcs378","assignment":"hw1","nodes_per_group":2,
#  "deadline":"2026-09-16 04:59 UTC"}
```

Check `deadline` in that output now rather than discovering it on the day.

**5. Put TLS in front.** The service is bound to `127.0.0.1`, so nothing can
reach it yet. That is deliberate. Two secrets cross this connection, not one:
the bearer token in every request header, and the group's SSH *private key*,
which `/session` returns in the response body and the CLI polls for every five
seconds while a session comes up. Anyone on-path — the same lecture-hall Wi-Fi
will do — can use the first to destroy another group's nodes and the second to
log into them first.

Caddy closes that with three lines, and nothing in gpulease or the CLI has to
change: the student CLI validates certificates through Python's default trust
store, so an ordinary Let's Encrypt certificate just works.

First point a DNS name at the Elastic IP from step 2. Three things that look
like they should serve as one and cannot:

- **The EC2 public hostname.** Let's Encrypt will not issue for
  `*.compute.amazonaws.com` — AWS owns it and it is on the Public Suffix List —
  so `ec2-1-2-3-4...` fails the challenge.
- **A bare IP address.** Caddy does not request a public certificate for one; it
  falls back to its internal CA, and the student CLI validates through Python's
  default trust store, so it refuses the result. Telling students to skip
  verification is worse than plain HTTP, not better: an unverified connection
  accepts *any* certificate, so an on-path attacker presents their own and reads
  everything while the students believe they are safe.
- **The course's GitHub Pages URL.** Pages is static-only and cannot proxy to
  EC2, and `username.github.io` is not a zone you can add a record to. Serving
  `cli/gpulease.py` to students from the course page is a fine idea; the API
  cannot live there.

A department subdomain or a cheap domain both work. If you have neither and do
not want to buy one, a free dynamic-DNS name is a real answer rather than a
compromise: Let's Encrypt issues for one exactly as it would for a domain you
paid for. This deployment uses [duckdns.org](https://duckdns.org), which takes
about two minutes:

1. Sign in at duckdns.org with GitHub, Google, Twitter or Reddit. There is no
   password to manage and nothing to pay.
2. Type a name into the **sub domain** box and click **add domain**. That claims
   `<name>.duckdns.org` — for this course, `utcs378-infra.duckdns.org`.
3. **Replace the pre-filled `current ip` with the lease host's Elastic IP** and
   click **update ip**. This is the step to be careful about: the box arrives
   pre-filled with the address *you are browsing from*, so claiming the name and
   walking away points it at your laptop, and Caddy's certificate request then
   goes to whatever machine that is. The error it produces looks like a Caddy
   problem and is not.
4. Confirm it before installing Caddy:

   ```bash
   dig +short utcs378-infra.duckdns.org     # must print the Elastic IP
   ```

No updater cron is needed, because the Elastic IP is static — the record is set
once and the token DuckDNS shows you is only for automated updates you will not
be making. Do check step 4 rather than assuming, since a name that does not
resolve yet and a port 80 that is still closed produce similar-looking failures.

The name has to match `API_URL` in `cli/gpulease.py`, which ships pointing at
`utcs378-infra.duckdns.org` so students who never set `GPULEASE_API` still reach
the right host. If you use a different name, change it there too.

If you ever rebuild the lease host, that DNS record is the one piece of this
system that lives outside the repo and has to be updated by hand.

```bash
# Caddy is not in Ubuntu 22.04's repos; add the official one.
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

sudo cp Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # only if you claimed a name other than
                                      # utcs378-infra.duckdns.org
sudo systemctl reload caddy
```

Caddy requests the certificate on that first reload and renews it on its own.
Confirm from your own machine, not the host:

```bash
curl -s https://utcs378-infra.duckdns.org/healthz
```

A hang means port 80 or 443 is still closed on the host security group; a
certificate error means DNS is not pointing here yet. `journalctl -u caddy -n
30` says which of the two it is.

**`GPULEASE_HOST` and `GPULEASE_PORT` are the two settings a restart will not
apply.** `setup.sh` bakes both into the systemd unit's `ExecStart` at install
time, so if you ever change either, re-run `sudo ./setup.sh`. `systemctl
restart` alone leaves the old values and the change looks silently ignored.

A stale port is the one that wastes an afternoon: the service comes up healthy
on the old number, Caddy keeps proxying to the one in its Caddyfile, and the
only symptom is a 502 that looks like a TLS or firewall problem. If you get one,
check `journalctl -u gpulease | grep "Uvicorn running"` against the
`reverse_proxy` line in `/etc/caddy/Caddyfile` before you look at anything
else.

**6. Load the roster.** On the lease host — `admin.py` reads the local
database.

```bash
cp roster.example.csv roster.csv     # columns: student_id,name,group_id
nano roster.csv
./admin.py roster roster.csv
```

That writes `tokens.csv` (mode `0600`), the only copy of the secrets. Only
`sha256(secret)` is stored server-side, so a stolen database gives nobody
access — and nobody, including you, can recover a lost token. Reissue instead:
`./admin.py roster roster.csv --rotate`.

**7. Hand out the CLI.** Give students `cli/gpulease.py` — one stdlib-only
file, Python 3.9+, no AWS account and no dependencies — their own row from
`tokens.csv`, and the URL:

Print the line to send them, so the address is never retyped:

```bash
echo "export GPULEASE_API=https://utcs378-infra.duckdns.org"   # the name from step 5
```

What each student then runs — no AWS account, no dependencies, their own token
from their row of `tokens.csv`:

```bash
export GPULEASE_API=https://...    # the line you just printed; put it in their shell rc
python3 gpulease.py login <their-token>
python3 gpulease.py start          # prints an ssh command per node when ready
python3 gpulease.py status
python3 gpulease.py stop
```

Then delete `tokens.csv`.

### What students do with two nodes

`start` prints one ssh line per node. Everything a distributed run needs is
already on the boxes, so the two-node version of "hello world" is:

```bash
ssh -A -i ~/.ssh/gpulease_7 ubuntu@<node0>      # -A forwards your key to node1
cat /etc/gpulease/peers                          # 10.0.1.20 node0 / 10.0.1.34 node1
echo $MASTER_ADDR $MASTER_PORT $NODE_RANK $NNODES

# on node0 (NODE_RANK=0) and node1 (NODE_RANK=1), one shell each:
torchrun --nnodes=$NNODES --node_rank=$NODE_RANK --nproc_per_node=$GPUS_PER_NODE \
         --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT train.py
```

`ssh node1` works from node0 (it is in `/etc/hosts`), but only if you connected
with `ssh -A` — the session's private key is deliberately never copied onto an
instance. `/etc/gpulease/hostfile` is the same list in mpirun/deepspeed format.

Worth telling them up front: the two nodes are one lease, and both are
temporary. `stop` destroys both, and if one dies the reaper destroys the other,
because a half cluster bills in full and cannot finish the job. Whatever is on
either disk goes with them — work in a git repo and push before stopping.

---

## Operating it

```bash
./admin.py sessions          # what the database believes, and what it has cost
./admin.py instances         # what EC2 actually has
./admin.py reap              # run one reconciliation pass by hand
./admin.py kill --group 7    # end group 7's session now; the nodes are destroyed
./admin.py disable abc123    # revoke one token; `enable` puts it back
./admin.py budget 7 --hours 0  # give group 7 their hour budget back
./admin.py grant 7           # another start (only when GPULEASE_MAX_STARTS is set)
./admin.py terminate         # sweep up anything left over; irreversible
./admin.py preflight         # check the host's AWS permissions

curl -s localhost:8000/healthz      # course, assignment, node count, deadline
journalctl -u gpulease -f
sudo systemctl restart gpulease     # after editing gpulease.env
```

Between assignments, bump `GPULEASE_ACTIVE_ASSIGNMENT` and restart. The hour
budget and the start limit are both per `(group, assignment)`, so everyone
starts fresh. No CLI change.

### Rationing: the hour budget

**`GPULEASE_GPU_HOUR_QUOTA` is the ration.** A group starts and stops as often
as it likes and spends from a pool of node-hours for the assignment — 60 in
`gpulease.env.example`. Node-hours, so a two-node session spends two per
wall-clock hour and a group's 60 is 30 hours of a two-node cluster. When the
pool is empty, `gpulease start` returns a 429 and that is the end of the
assignment for them unless you top them up. Three consequences:

- **Stopping is how a group saves money**, so it is worth telling them that. A
  group that walks away from a running cluster keeps paying for it until the
  lease expires — there is no idle watchdog. Stopping is also cheap for them
  now: the cost is re-provisioning a fresh box, not losing a session they
  cannot get back.
- **A start's lease is capped at what is left.** A group with 40 node-minutes
  gets a 20-minute two-node lease, and the reaper's ordinary "lease expired"
  path is what ends it. Nothing accrues mid-session, so `admin.py sessions` and
  `gpulease status` compute the live figure for display and the group is
  actually billed when the session stops.
- **`GPULEASE_MIN_START_MINUTES` is a floor under the lease**, not under the
  budget. A start is refused when the lease it would buy — remaining
  node-hours divided by the node count — falls below it, rather than handing
  out a cluster that expires before it has booted, because booting itself
  costs budget. At two nodes a group needs twice this many node-minutes left.

```bash
./admin.py sessions                   # GPU-HRS includes the live session
./admin.py budget 7 --hours 0         # give group 7 their whole budget back
./admin.py budget 7 --hours 45        # or leave them 15 of 60
```

**Capping the number of sessions as well** is `GPULEASE_MAX_STARTS=N`. It
defaults to `0`, unlimited, and that is almost always what you want: sessions
are disposable, so a group that hits a capacity error or fat-fingers `stop`
just starts again. Set it only if you specifically want to limit attempts, and
expect to grant restarts when you do:

```bash
./admin.py grant 7                    # one more start for group 7
./admin.py grant 7 --starts 2         # or more
```

**A failed launch costs nothing.** If the instances never came up — capacity, a
bad AMI — the start is refunded and no time is billed, because a session that
never existed is not charged.

**Everything a group does with `stop` is destructive**, so `gpulease stop`
always makes the student type `stop` to confirm (`-y` skips it). Tell them
early and often: the nodes and their disks are deleted, and the only thing that
survives is what they pushed to git.

Loading a roster never disables anyone who has dropped off it — a CSV edit
silently revoking access is not a good default — so use `./admin.py disable`
for that.

The student security group is reconciled against `GPULEASE_ALLOWED_SSH_CIDRS`
on every start, so narrowing that setting genuinely closes the range it
replaced. Only `tcp/22` IPv4 rules are managed; another port you opened
deliberately is left alone.

Students run whatever copy of `cli/gpulease.py` you gave them, and the server
refuses one that is too old: the CLI sends `X-CLI-Version`, and anything below
`MIN_CLI_VERSION` (currently 3) gets a 426 telling the student to reinstall.
That gate is used only when an old client would actively mislead — v1 displayed
a single `host` and would have shown a group one of their two nodes with no
hint the other existed; v2 told them their files survived a stop, which stopped
being true when instances became disposable. If you bump it, redistribute the
file.

Back up `var/gpulease.db` — it is the whole system state, including live
session keys, so it is also the file to protect (it is created `0600`).

### The assignment deadline

`GPULEASE_DEADLINE` is a hard cutoff for the active assignment, written in ISO
8601 **with a UTC offset** — a bare timestamp is read as UTC, which is almost
never what a syllabus means. Blank means no deadline. A malformed value stops
the service at boot on purpose: a cost control that silently never fires is
worse than one that fails loudly.

```
GPULEASE_DEADLINE=2026-09-15T23:59:00-05:00
```

It is enforced in three places, and it needs all three:

- **`start` refuses** once it has passed — a 403 naming the date.
- **Every lease is capped** at it, so a group starting an hour before the
  cutoff gets a one-hour lease rather than an eight-hour one that outlives the
  assignment.
- **The reaper sweeps** everything tagged for the course once it passes. This
  is not redundant with the cap: sessions already running when you *set* the
  deadline have an uncapped `expires_at`, and the sweep works off EC2 rather
  than the session table, so an instance whose row was lost still gets caught.

The sweep terminates, like every other disposal path, and that is the whole of
end-of-assignment cleanup: no grace period to configure, no volumes left to
reclaim afterwards, nothing to run by hand. There is no student work on those
disks to protect, because a session's disk is destroyed when the session ends
and the students were told so at every `start`, `status` and `stop`.

The old `GPULEASE_TERMINATE_AT_DEADLINE` and `GPULEASE_TERMINATE_GRACE_HOURS`
are gone. If they are still in your `gpulease.env` they are silently ignored;
delete the lines.

`curl -s localhost:8000/healthz` prints the deadline the service is actually
running with; `./admin.py sessions` prints it too and says whether it has
passed. Changing it means editing `gpulease.env` and restarting — it is parsed
once, at boot.

### Configuration reference

All of it lives in `gpulease.env` on the lease host, server-side on purpose so
the rules can change mid-semester without asking anyone to upgrade a CLI.
`gpulease.env.example` is the annotated template. Restart after editing.

The "default" column is what the *code* falls back to when a key is absent; the
shipped example deliberately differs where a safe default and a useful one are
not the same thing.

| Setting | Default | Notes |
|---|---|---|
| `GPULEASE_COURSE` | `cs378` | The tag on every instance, and the blast radius: the service only ever touches instances carrying it. Must match the IAM policy's `REPLACE_COURSE_TAG`. **This deployment's tag is `utcs378`**, which is what the example ships; the code default is deliberately something else so an unconfigured install cannot inherit the live blast radius. |
| `GPULEASE_REGION` | `us-west-2` | Must be the lease host's own region. |
| `GPULEASE_HOST` / `GPULEASE_PORT` | `127.0.0.1` / `8000` | Localhost because the API belongs behind a TLS terminator — see "Put TLS in front". Change the port and the Caddyfile's `reverse_proxy` must follow. **Changing either needs `sudo ./setup.sh`, not a restart:** both are baked into the systemd unit at install time. |
| `GPULEASE_DB` | `<repo>/var/gpulease.db` | The whole system state. |
| `GPULEASE_AMI` | blank → latest Ubuntu 22.04 | **Stock Ubuntu has no NVIDIA driver.** Set a golden image for real GPU work; see below for the two DLAMI variants and how to look up current ids. Cached in-process, so restart after changing. |
| `GPULEASE_INSTANCE_TYPE` | `t3.micro` | The example ships `g4dn.xlarge`. The code default is a cheap smoke-test type on purpose — a typo should not launch a GPU fleet. |
| `GPULEASE_ROOT_GB` | `30` | Must be ≥ the AMI's own root snapshot, or every `RunInstances` fails with `InvalidBlockDeviceMapping`. The example ships `100` for the DL AMIs. Billed only while a session runs — the volume is destroyed with its instance. |
| `GPULEASE_SUBNET_IDS` | blank → public subnets of the VPC | Tried in order on capacity errors. Must be in one VPC; spanning two is rejected at startup. Sets the VPC the security groups are created in. |
| `GPULEASE_ALLOWED_SSH_CIDRS` | `0.0.0.0/0` | Reconciled, not merely added to — narrowing it closes the old range. The lease host adds its own address automatically for the readiness probe. |
| `GPULEASE_ACTIVE_ASSIGNMENT` | `hw1` | Every limit is per `(group, assignment)`. Bump it between homeworks and everyone starts fresh. |
| `GPULEASE_DEADLINE` | blank → none | See above. **The example ships it blank on purpose**: `setup.sh` starts the service on a copy of the example, so a date left in it would be live on first boot. Stamp it at deploy time. |
| `GPULEASE_MAX_STARTS` | `0` (unlimited) | Cap on the number of sessions a group gets per assignment, on top of the hour budget. Unlimited is the sane setting — sessions are disposable, so a lost one is not a catastrophe. `1` restores the old one-lease-per-group policy. |
| `GPULEASE_MAX_SESSION_HOURS` | `8` | The bound on any single session. `0` removes the cap, leaving the hour budget as the only bound — a session then runs until the group's quota is spent. |
| `GPULEASE_GPU_HOUR_QUOTA` | `30` | **The ration.** Cumulative **node**-hours per group per assignment. Charged when a session stops, checked when one starts — and a start's lease is capped at whatever is left, so the reaper's ordinary expiry is what ends a session that runs it out. The example ships `60`. |
| `GPULEASE_MIN_START_MINUTES` | `15` | The smallest lease worth handing out. A group whose remaining budget buys a shorter lease than this — node-hours left ÷ `NODES_PER_GROUP` — is refused rather than given a cluster that expires before it finishes booting. |
| `GPULEASE_NODES_PER_GROUP` | `2` | Instances per group per session, max 8. Multiplies the bill directly. |
| `GPULEASE_GPUS_PER_NODE` | `1` | GPUs exposed per node via `CUDA_VISIBLE_DEVICES`, and the `slots=` count in the mpirun hostfile. The older name `GPULEASE_GPUS_PER_GROUP` still works. |
| `GPULEASE_MASTER_PORT` | `29500` | Exported to the nodes as `MASTER_PORT`. torch.distributed's default. |
| `GPULEASE_REAPER` | `1` | `0` disables the reconciliation thread. Only for local development — nothing else stops an expired lease. |
| `GPULEASE_REAPER_INTERVAL` | `120` | Seconds between passes. Also the worst-case lag on an expiry. |

Two variables are not settings *in* that file. `GPULEASE_ENV` chooses which
file to read — `GPULEASE_ENV=gpulease.env.test ./admin.py sessions` — replacing
`gpulease.env` rather than layering on top of it, so anything the chosen file
omits falls back to the code defaults rather than to production's value. It
affects only the fallback load; systemd passes `gpulease.env` as an
`EnvironmentFile` regardless, and real environment variables always win.
`GPULEASE_API` is the one variable that is genuinely client-side: students set
it in their own shell to point the CLI at your lease host.

### Ending an assignment

Mostly, nothing. Every session ends by terminating its instances, and their
root volumes go with them, so an assignment does not leave a fleet of stopped
boxes billing for disks nobody will read again. When the last group stops, the
course's EC2 and EBS spend for that assignment is zero.

**At the deadline**, the reaper terminates anything still tagged for the course
on its next pass, whatever the database believes — the backstop for a session
that was already running when you set the deadline, and for an instance whose
session row was lost.

**On demand, from the host**, if you are finishing early or never set a
deadline:

```bash
./admin.py kill                      # end live sessions now
./admin.py terminate                 # sweep up everything for the assignment
./admin.py terminate --group 7       # or one group
./admin.py terminate --all-assignments
```

`terminate` lists what it will destroy, says how many are still running, and
makes you type the course tag to confirm. `kill` is the lighter one: it only
touches what is running, which is all there normally is.

**By hand, with your own credentials**, if you would rather the service never
had `ec2:TerminateInstances` at all. That is a bigger change than it used to
be — terminating is now how *every* session ends, so removing the
`TerminateOnlyInstancesTaggedForThisCourse` statement from `iam-policy.json`
breaks `stop`, not just cleanup. If you do, the equivalent sweep is:

```bash
aws ec2 terminate-instances --region $REGION --instance-ids $(
  aws ec2 describe-instances --region $REGION \
    --filters "Name=tag:Course,Values=$COURSE" \
              "Name=instance-state-name,Values=running,stopped,stopping" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
```

Students do not need warning before any of this the way they used to: they are
told at every `start`, `status` and `stop` that the nodes are temporary, and by
the deadline there is nothing of theirs on any disk that they were not already
told to push to git. Bump `GPULEASE_ACTIVE_ASSIGNMENT` and restart; the budget
is per `(group, assignment)`, so the next assignment starts everyone at zero
and the old rows stay for reporting.

### Tearing it all down

```bash
# 1. terminate the student fleet, as above
# 2. keep var/gpulease.db if you want the usage history; it is the only record
# 3. delete the leftovers. There is one security group per group as well as
#    the shared ssh one, so delete them by tag rather than by name.
for SG in $(aws ec2 describe-security-groups --region $REGION \
      --filters "Name=tag:Course,Values=$COURSE" \
      --query 'SecurityGroups[].GroupId' --output text); do
  aws ec2 delete-security-group --region $REGION --group-id $SG
done
aws ec2 terminate-instances  --region $REGION --instance-ids $HOST_ID
aws ec2 wait instance-terminated --region $REGION --instance-ids $HOST_ID
aws ec2 delete-security-group --region $REGION --group-id $HOST_SG
aws iam remove-role-from-instance-profile \
  --instance-profile-name gpulease-host --role-name gpulease-host
aws iam delete-instance-profile --instance-profile-name gpulease-host
aws iam delete-role-policy --role-name gpulease-host --policy-name gpulease
aws iam delete-role --role-name gpulease-host

# 4. if you allocated an Elastic IP, release it - an unassociated one bills
aws ec2 describe-addresses --region $REGION \
  --query 'Addresses[?AssociationId==null].[PublicIp,AllocationId]' --output text
# aws ec2 release-address --region $REGION --allocation-id <from above>
```

The `wait instance-terminated` is not decoration: a security group cannot be
deleted while anything still references it, and a terminating instance counts.
For the same reason the per-group cluster groups only delete once the student
fleet in step 1 is fully terminated, not merely stopped.

---

## How it works

**Two things can spend money, and only one of them is reachable by students.**
`api.py` is the only module that starts instances. `reaper.py` has no code path
that starts anything: it reads EC2 (what costs money) and SQLite (what we
believe) and drives one toward the other. Every action is idempotent, so a
missed pass, a crashed pass and a duplicate pass all converge. It handles an
expired lease, orphan instances with no session row, a session we think is
live whose instances are gone, a launch stuck in `PROVISIONING`, a cluster that
has lost a node, and anything it finds `stopped` rather than terminated — which
under this design is a root volume billing for a session nobody can return to.
Every one of those disposals terminates.

**Session state machine** (`db.py`):
`STOPPED → PROVISIONING → RUNNING → STOPPING → STOPPED`, plus `FAILED`.
`STARTABLE = (STOPPED, FAILED)` are the only states a start is allowed from.
`STOPPED` means the group has no instances at all — they were terminated on the
way out — so a start from there builds a new cluster rather than resuming one.

**One transaction does four jobs.** `db.claim()` runs under `BEGIN IMMEDIATE`
and in one place enforces the GPU-hour quota, enforces any start limit, stops a
second group member from launching a duplicate cluster, and claims the session.
All four are decided against one snapshot — two simultaneous requests each
concluding the group had budget for a session and launching one apiece is
exactly the bug this prevents. It also records
`node_count`, so a group mid-session keeps the cluster it was actually given
even if the setting changes under them, and is billed for that many. The race loser is
handed the existing session (a 200, not an error). It is the most load-bearing
code here.

**Student instances hold no AWS credentials.** No instance profile, no SSM, no
AWS CLI on the box.

- The session's public key travels in cloud-init user-data, in `bootcmd`, the
  one cloud-init module that runs on *every* boot. Every instance is new, so
  the first boot is the one that matters; `bootcmd` also puts the key and the
  peer files back after a reboot, which a student may well do mid-session.
- Nothing gpulease-specific runs on the instance at all — no agent, no timer,
  nothing that phones home. Instances are launched with
  `InstanceInitiatedShutdownBehavior=terminate`, so a student running `sudo
  poweroff` destroys their own node instead of leaving a stopped one billing
  for a disk. They can start again; that is the whole cost of the mistake.

**Readiness is sshd answering on every node**, checked by opening port 22 from
the lease host. EC2's own status checks go green a minute or two earlier, and
handing a student an ssh command that fails is worse than making them wait. A
cluster is not `RUNNING` until all of it is: telling a group to launch a
two-node job while node 1 is still booting just produces a rendezvous timeout.

### Multi-node clusters

A group's session is `GPULEASE_NODES_PER_GROUP` instances. Four things make
them usable as a cluster, and each is somewhere it is easy to break:

**One subnet, one `RunInstances`.** All of a group's nodes are launched in a
single call with `MinCount == MaxCount == n`. That is what guarantees they
share a subnet and therefore an availability zone — cross-AZ traffic is billed
per gigabyte and an all-reduce moves a lot of gigabytes. It is also
all-or-nothing: EC2 places the whole set or fails, and we try the next subnet.
Half a cluster is not a partial success, it is an instance that bills while
nobody can use it.

**A security group per group.** `{course}-cluster-{group}` — the group id
sanitised, plus a dot and a hash of the original when sanitising changed
anything — allows all traffic from itself, which is the only workable rule when the ports are whatever
torchrun, NCCL or mpirun decide to pick. Per group rather than one shared
group for the course: the shared version would let every group reach every
other group's nodes on every port, and the normal state of a training box is an
unauthenticated rendezvous endpoint next to a Jupyter server. Port 22 stays on
the separate `{course}-instances` group, which is what
`GPULEASE_ALLOWED_SSH_CIDRS` narrows.

**Peers arrive through IMDS, not through a credential.** After launch the
control plane tags each instance with `NodeIndex` and `NodePeers` (the private
addresses of the whole group). The instance reads them back from instance
metadata with `InstanceMetadataTags` enabled — link-local, no AWS keys, no call
home — and `bootcmd` turns them into `/etc/gpulease/peers`, an
`/etc/gpulease/hostfile` for mpirun, `node0`/`node1` entries in `/etc/hosts`,
and `MASTER_ADDR` / `MASTER_PORT` / `NODE_RANK` / `NNODES` in
`/etc/profile.d`. The tags are written moments after `RunInstances` returns, so
a boot can lose that race and the boot script retries for ~40s — and every boot
is a first boot now. `WORLD_SIZE` and `RANK` are deliberately *not*
exported — torchrun sets those per process and a stale value in the environment
breaks the job confusingly.

**One key opens all of them.** Every node gets the same user-data and therefore
the same session key. To hop from node0 to node1, connect with `ssh -A` and use
the forwarded agent; the private key stays on the student's machine rather than
being copied onto a shared box.

`MASTER_ADDR` is rank 0's **private** address. Handing out the public one would
route an all-reduce out through the internet gateway and back.

**A cluster that loses a node is destroyed, not nursed.** The reaper compares
the running instance count against the session's `node_count`; a two-node
session down to one is terminated and closed with reason
`partial cluster (1/2 nodes)`. The survivor bills at the full rate and cannot
run the job on its own — a two-node all-reduce with one node hangs, it does not
run slowly. The group is billed for the time it ran and simply starts again;
`./admin.py budget <group> --hours N` refunds the hours if the failure was not
theirs.

**Two key systems, which is worth being explicit about.**

| | Lease host | Student instances |
|---|---|---|
| Key | an EC2 key pair you already own | generated per session by `keys.py` |
| Made by | you, once, via `create-key-pair` | `ssh-keygen -t ed25519` on the lease host |
| Lives in | your `~/.ssh` and EC2's key-pair registry | SQLite, and the instance's `authorized_keys` |
| Reaches the box via | `--key-name` at launch | cloud-init user-data |
| Who uses it | you | one student group, for one session |

Student instances are launched with **no `--key-name` at all**. The session key
has to be different every session and belong to exactly one group, which an EC2
key pair — registry-wide, fixed at launch — cannot do. Involving them would
also leave a per-region pile to clean up.

**Keys are per session, not per group.** Stopping a session deletes the private
key, and the instance it opened no longer exists, so access is revoked twice
over without anyone rotating anything.

**Instances are ephemeral: every disposal terminates.** A start launches new
instances; `stop`, lease expiry, a partial cluster, an orphan, a wedged launch
and the deadline sweep all terminate them, and `DeleteOnTermination` takes the
root volume with them. Nothing persists between a group's sessions, which is
the point — a stopped instance costs the course its full EBS bill around the
clock for a disk that the next start would not have looked at anyway. What
students get instead of persistence is as many sessions as their budget allows,
and a clean box each time. Tell them to work in git.

**The lease window is the only thing bounding a session.** There is no idle
auto-stop: a group that starts an instance has it until they stop it or the
lease runs out, whether or not they are using it. That is a deliberate trade —
see "What a session can cost you". The lease is
`GPULEASE_MAX_SESSION_HOURS`, or less when the group's remaining hour budget
is shorter, which is how `GPULEASE_GPU_HOUR_QUOTA` gets enforced without
anything running on the instance. Set it to `0` and there is no lease cap at
all — the same mechanism with the first term removed, so every session runs
until the budget is spent.

**IAM scoping is the invariant worth preserving.** Every terminate and tag
action is conditioned on `ec2:ResourceTag/Course`, and `RunInstances` on
`aws:RequestTag/Course` — so nothing this service creates can escape the scope
of what it is allowed to destroy. The host role no longer asks for
`ec2:StartInstances`, `ec2:StopInstances`, `ec2:ModifyInstanceAttribute` or
`ec2:ModifyInstanceMetadataOptions` at all: those existed for the resume path,
and there is no resume path. Re-apply `iam-policy.json` when you upgrade.

### Layout

```
gpulease/
  config.py     all configuration, from gpulease.env
  db.py         schema, auth, the session state machine, the atomic claim
  aws.py        every EC2 call; nothing else imports boto3
  userdata.py   the cloud-init payload for a student instance
  keys.py       per-session ed25519 keys, via ssh-keygen
  api.py        FastAPI: /healthz /whoami /session /session/start /session/stop
  reaper.py     reconciliation, as a thread inside the API process
cli/gpulease.py student CLI, stdlib-only, Python 3.9+, no AWS
admin.py        instructor tools
setup.sh        the installer
iam-policy.json permissions for the lease host
gpulease.env    your config; gpulease.env.example is the annotated template
var/gpulease.db the whole system state, created 0600
```

Three tables in that database: `students` (token hashes, group membership),
`sessions` (one row per group per assignment, however many instances that is),
and `session_log` (append-only history, for end-of-semester reporting and for
settling a dispute about who ran what).

---

## Verifying a deployment

There is no automated test suite. This is the checklist.

Run it under `gpulease.env.test` rather than your real config. It is the same
control plane — launch, readiness, peers over IMDS, the reaper, the budget,
termination all work identically — on `t3.micro` instead of `g4dn.xlarge`, with
minutes where production has hours, so the whole list costs cents. What it
cannot cover is anything needing a GPU: stock Ubuntu has no driver, so the boot
script's GPU count is 0.

```bash
export GPULEASE_ENV=gpulease.env.test      # replaces gpulease.env; does not merge
export GPULEASE_API=http://127.0.0.1:8001  # EVERY CLI command below needs this
python -m gpulease.db init                 # setup.sh only ever inits the real one
./admin.py roster test-roster.csv          # a couple of fake students, two groups
.venv/bin/uvicorn gpulease.api:app --port 8001
python3 cli/gpulease.py login <token>
```

Three things about it are deliberate and worth not undoing:

- **`GPULEASE_API` is exported for the whole session, not set on one command.**
  `cli/gpulease.py` defaults to the production host, so a checklist command run
  without it does not fail with "connection refused" — it reaches the live
  deployment. Whether that is a 401 or a real action then depends on which token
  happens to be in `~/.config/gpulease/credentials`, since the CLI keeps one
  credential file with no per-host separation and a test `login` and a
  production `login` overwrite each other. A stray `stop` that lands on
  production destroys a real group's nodes and everything on their disks. They
  can start again, but whatever they had not pushed is gone. Do not rely on the
  token mismatch to save you; export the variable.

- **It uses a different `GPULEASE_COURSE` (`utcs378-test`).** That tag is the
  blast radius for every describe, stop and terminate. Sharing it with the live
  deployment would let a test run's `admin.py terminate`, or a deliberately
  passed deadline, destroy real students' disks. The price is that the host's
  IAM role must allow both tags — `StringEquals` takes a list:

  ```bash
  sed 's/"REPLACE_COURSE_TAG"/["utcs378","utcs378-test"]/' iam-policy.json \
    > /tmp/gpulease-policy.json
  aws iam put-role-policy --role-name gpulease-host \
    --policy-name gpulease --policy-document file:///tmp/gpulease-policy.json
  ```

  Run that from your laptop — the host's role cannot edit its own policy. The
  same `--policy-name` overwrites in place and takes effect on the next call, so
  there is nothing to restart.

  Skip it and `./admin.py preflight` fails at **security group**, not at
  `RunInstances`: the first thing the test config asks for is a security group
  tagged `Course=utcs378-test`, and `CreateSecurityGroup` is conditioned on
  `aws:RequestTag/Course` like everything else. The message reads "no
  identity-based policy allows the ec2:CreateSecurityGroup action", which sounds
  like a missing permission and is really a tag that does not match.
- **`GPULEASE_GPU_HOUR_QUOTA=0.05`** — three node-minutes, a 90-second lease at
  two nodes. That is exactly what item 6 checks, and it will cut every *other*
  test short. Raise it to `0.5` while you work through the rest.

1. **Preflight.** `./admin.py preflight` — every line `ok`.
2. **Happy path.** `login`, `start`, ssh in, `stop`. You should get
   `GPULEASE_NODES_PER_GROUP` ssh lines, and none of them printed before that
   node actually accepts connections.
3. **Stop destroys, and restart is clean.** After the `stop` in item 2, watch
   the instances go to `shutting-down` and then disappear from
   `./admin.py instances` — `aws ec2 describe-volumes` should show their root
   volumes gone too. Then `start` again: **new** instance ids, a new key, and
   the file you left in `/home/ubuntu` is *not* there. This is the whole change;
   if instance ids are reused, something is still resuming.
4. **Concurrent start.** Two group members run `start` at the same moment. One
   cluster, both get the same session.
5. **Start limit (optional).** Only meaningful if you have set
   `GPULEASE_MAX_STARTS`. With `=1`: `stop` a session and `start` again,
   expecting a clean 429 saying the group has used its one session, not a 500.
   `claim()` checks the limit before the budget, so with a limit set the budget
   429 below becomes unreachable. Leave it at `0` for the rest of the list.
6. **The budget — the ration.** Set `GPULEASE_GPU_HOUR_QUOTA=0.05`
   (3 node-minutes) and `GPULEASE_MIN_START_MINUTES=0`, restart, then check four
   things:
   - `start`, then `gpulease status`. With two nodes the lease should end in
     about 90 seconds, annotated "all the gpu-hours you have left" — *not* in
     `MAX_SESSION_HOURS`. That cap is the whole of budget enforcement, so if it
     is wrong nothing else here matters.
   - `./admin.py sessions` while it runs: GPU-HRS climbs at `NODES_PER_GROUP`
     per wall-clock hour. It is computed for display; the row is only written
     when the session stops.
   - Leave it. Within a reaper interval it ends as `lease expired`, the
     instances terminate, and `./admin.py sessions` shows about 2x the
     wall-clock time consumed.
   - `start` again: a 429 naming hours used and left, and **no instances
     launched**. This is the ration doing its job — with unlimited starts it is
     the only thing that ever says no. Raise `GPULEASE_MIN_START_MINUTES` above
     what remains and the message changes to the "needs at least N minutes'
     worth" one.

   Then `./admin.py budget <group> --hours 0` and confirm they can start again.
7. **Reaper.** `./admin.py kill --group N` behind the service's back, then
   `./admin.py reap` twice. First pass reconciles the row, second is a no-op.
8. **Orphan.** Launch an instance tagged `Course=$COURSE` by hand with no session
   row; `./admin.py reap` should terminate it.
9. **Stopped instances are reclaimed.** Stop an instance by hand
   (`aws ec2 stop-instances`) — a session's node, or a hand-launched one — and
   wait for `stopped`. The next `./admin.py reap` must terminate it: nothing in
   this design ever wants a stopped instance, and one left behind is an EBS
   bill with no session attached. Run `reap` again: no-op.
10. **Lease expiry.** Set `GPULEASE_MAX_SESSION_HOURS=0.05`, restart, start a
   session and leave it alone. Within a reaper interval the instances should be
   terminated and the session `STOPPED` with reason `lease expired`. This is the
   *only* automatic end to a session, so test it rather than assuming. Then set
   it to `0` and start again: the lease must be the group's whole remaining
   budget (`./admin.py sessions`, and `status` says it ends because of the
   budget), *not* an instantly-expired session that the next reaper pass
   terminates. `0` is the one value that means "no cap" rather than "no time".
11. **Narrowing SSH.** Set `GPULEASE_ALLOWED_SSH_CIDRS` to your own `/32`,
   restart, and confirm in the console that `0.0.0.0/0` is *gone* from the
   `$COURSE-instances` group — and that `start` still reaches `RUNNING`
   (readiness is a port-22 probe from the lease host, so it needs its own way
   in; the service adds that automatically).
12. **Revocation.** `./admin.py disable <student>`, then that student's
    `gpulease status` must fail with a 401.
13. **Deadline.** Set `GPULEASE_DEADLINE` a minute or two ahead (with your UTC
    offset) and restart. Three things must hold: a session started before it
    has an `expires_at` capped at the deadline rather than a full
    `MAX_SESSION_HOURS` lease; `start` afterwards is a 403 naming the date; and
    within one reaper interval everything tagged for the course is
    **terminated** — including an instance you launched by hand with no session
    row, and one you left `stopped`, since the sweep works off EC2 rather than
    the database and covers every state. Run `reap` twice; the second pass must
    find nothing. Blank the deadline out and restart before you carry on, or
    nothing else will start.
14. **The old CLI is refused.** Run a v2 `cli/gpulease.py` (git will give you
    one) against the service: every command must fail with the 426 upgrade
    message. A v2 client tells students their files survive a stop, which is
    now the opposite of true.
15. **Students are told.** `gpulease start` and `gpulease status` on a live
    session both say the nodes are destroyed on stop, and `gpulease stop` makes
    them type `stop` to confirm. Check all three, then tell the class in a way
    that does not depend on them reading it.

With `GPULEASE_NODES_PER_GROUP` above 1, also:

16. **One subnet.** `./admin.py instances` — every node of a group shows the
    same `NODE` ranks 0..n-1, and in the console they are in the same
    availability zone. If they are not, the single-`RunInstances` path was
    bypassed somewhere.
17. **Peers on the box.** ssh to node0: `cat /etc/gpulease/peers` lists every
    node's private address, `ping node1` resolves, and
    `echo $MASTER_ADDR $NODE_RANK $NNODES` is populated. On node1, `NODE_RANK`
    is 1 and `MASTER_ADDR` is unchanged. If the peers file is missing, the
    instance did not get tags through IMDS — check that `RunInstances` still
    passes `InstanceMetadataTags: enabled`. Every node is freshly launched, so
    every boot races the tag write; the boot script retries for ~40s, and this
    is the item that catches a regression in that retry.
18. **They can actually talk.** From node0, `nc -vz node1 29500` after
    `nc -l 29500` on node1. This is the per-group security group doing its job;
    it is the thing that silently breaks if the cluster group is not attached.
19. **Isolation.** From group A's node0, the same `nc` to a group B node must
    *fail*. If it succeeds, the groups are sharing a security group.
20. **Agent hop.** `ssh -A` to node0, then `ssh node1` from there. Without
    `-A` it should fail — the private key is deliberately not on the instance.
21. **A new session's key opens every node.** After the restart in item 3,
    confirm the *old* key is rejected by node1 as well as node0, and the new one
    is accepted by both. The payload is identical per node, so a failure on one
    and not the other means the user-data did not reach it.
22. **Partial cluster.** Terminate one node of a live session behind the
    service's back (`aws ec2 terminate-instances`), then `./admin.py reap`. The
    other node must be terminated too and the session closed with reason
    `partial cluster (1/2 nodes)`. Run `reap` again: no-op.
23. **Capacity.** Set `GPULEASE_NODES_PER_GROUP` to something the account
    cannot place (or a scarce type) and `start` — a clean 503 telling the
    student to retry, the group's start refunded (`./admin.py sessions` shows
    `starts_used` unchanged), and no stray instances in `./admin.py instances`.

## Things to know before this is student-facing

- **The API must stay behind TLS.** Two secrets cross that connection, not
  one: the bearer token in every header, and the group's SSH *private key* in
  every `/session` response, which the CLI polls every five seconds while a
  session starts. Whoever captures the first can destroy another group's
  session and everything on its disks, and whoever captures the second gets a
  shell on their nodes. Step 5 sets this up. A campus VPN is a weaker
  substitute than it looks: it shrinks who can be on-path, but the hop from the
  VPN concentrator to AWS is still cleartext across the public internet.
- **Private keys sit in SQLite in plaintext** on the lease host. That host also
  holds credentials that can launch instances, so it is already the trust
  boundary; just don't put the database anywhere else.
- **Students have root on their instance**, which is the point. Nothing that
  bounds cost runs there — the lease window is enforced by the reaper on the
  lease host, which students cannot reach. That is why there is no on-instance
  watchdog to disable.
- **Nothing a group leaves on a node survives their session.** Say this in the
  assignment text, not just in the tool: `stop`, the lease expiring, a node
  failing and the deadline all destroy the instances and their disks. The CLI
  says it at `start`, at `status` and again at `stop` (where it makes them type
  `stop` to confirm), and the motd says it on the box — but a group that loses
  a week of work to a `stop` will not be consoled by having been told by a
  program. Give them a repo and tell them to push.
- **A golden AMI is doing more work than it used to.** Every session starts
  from the image, so whatever a group installs by hand they install again next
  time. Anything the assignment needs — driver, CUDA, torch, datasets — belongs
  in `GPULEASE_AMI` or in a setup script they can run in one line.
- **The session key is installed by cloud-init `bootcmd`.** If that ever stops
  running — a golden AMI with cloud-init disabled, say — a launched instance
  never accepts the key and `start` times out waiting for sshd. Test #2 above
  catches it.
- **`GPULEASE_AMI` must be a golden image** for real GPU work. The default
  (stock Ubuntu) has no NVIDIA driver, and installing one on first boot is slow
  enough that students will think it is broken. On a GPU instance type it is
  worse than slow: with no `nvidia-smi` the boot script counts zero GPUs and
  every job fails. Two `amazon`-owned Ubuntu 22.04 DLAMIs have the driver baked
  in, and the choice between them is a choice about CUDA:

  | Image | Snapshot | CUDA |
  |---|---|---|
  | Deep Learning Base AMI with Single CUDA | 35 GB | one toolkit (13.2 as of 2026-09) |
  | Deep Learning Base OSS Nvidia Driver GPU AMI | 75 GB | four side by side, `/usr/local/cuda-12.6`…`-13.0`, default 12.9 |

  The single-CUDA image is the cheaper one to run — 40 GB less per volume,
  times `groups x NODES_PER_GROUP` volumes, though now only for as long as
  sessions are actually running — and for kernel work `nvcc` plus a profiler is
  the whole requirement. Take the
  four-toolkit image instead when an assignment needs a 12.x toolchain or a
  cu12 torch wheel, since the single-CUDA one has no `/usr/local/cuda-12.x` to
  fall back on. The PyTorch DLAMI adds torch and NCCL on top of either. Note
  that CUDA 13 dropped Maxwell, Pascal and Volta but **not** Turing, so a T4
  (`g4dn`, `sm_75`) is fine on both.

  These print the current ids and the root sizes to use as the floor for
  `GPULEASE_ROOT_GB`:

  ```bash
  for NAME in 'Deep Learning Base AMI with Single CUDA (Ubuntu 22.04)' \
              'Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)'; do
    aws ec2 describe-images --region $REGION --owners amazon \
      --filters "Name=name,Values=$NAME*" 'Name=state,Values=available' \
      --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name,BlockDeviceMappings[0].Ebs.VolumeSize]' \
      --output text
  done
  ```

  AWS also publishes the latest id of each as a public SSM parameter, which is
  the better thing to read at deploy time than an id pasted into
  `gpulease.env` months ago:

  ```bash
  aws ssm get-parameter --region $REGION --output text --query Parameter.Value \
    --name /aws/service/deeplearning/ami/x86_64/base-with-single-cuda-ubuntu-22.04/latest/ami-id
  ```

  The id is cached in the running process, so `sudo systemctl restart gpulease`
  after changing it.
- **`GPULEASE_ROOT_GB` must be at least the AMI's own root snapshot size.** The
  launch passes `VolumeSize` explicitly, so a smaller value fails every
  `RunInstances` with `InvalidBlockDeviceMapping`. Both DL AMIs are larger than
  the 30 GB default — 35 GB and 75 GB respectively — so read the size out of
  `describe-images` and use it as the floor. Switching from the four-toolkit
  image to the single-CUDA one does not need this changed; switching back does,
  before the AMI, or every launch fails.
- **Cost.** Instances and their volumes are destroyed when a session ends, so
  between sessions a group costs nothing — no idle EBS, no idle public IPv4.
  What is left is the running cost, `GPULEASE_NODES_PER_GROUP` at a time, and
  the lease host itself. Set an AWS Budget anyway.
- **Students can reach their groupmates' nodes on every port**, by design: that
  is what distributed training needs. They cannot reach another group's. Nodes
  are not hardened against their own group.

## What a session can cost you

There is no idle watchdog. A group that starts an instance holds it until they
stop it or the lease expires, whether they are training or asleep. That is the
deliberate trade — students get uninterrupted machines — and it means one
setting carries the whole cost model:

```
worst case per assignment  ≈  groups × GPULEASE_GPU_HOUR_QUOTA × $/hour
                              (the quota is already counted in node-hours)

worst case for one session ≈  GPULEASE_NODES_PER_GROUP
                              × GPULEASE_MAX_SESSION_HOURS × $/hour
```

`GPULEASE_GPU_HOUR_QUOTA` is the number that matters, because starts are
unlimited: a group can come back as often as they like until the pool is empty,
and then they are done. For 26 groups on a `g4dn.xlarge` at about $0.53/hour
with a 60 node-hour budget, that is `26 × 60 × 0.53` ≈ **$830 per assignment**
if every group spends every hour. `MAX_SESSION_HOURS` bounds a single sitting —
2 nodes × 8 hours × $0.53 ≈ $8.50 — which is what stops one forgotten session
eating a group's whole allowance in a weekend. Setting it to `0` gives that up
knowingly: with no lease cap and no idle watchdog, the worst case for a single
session is the group's entire quota.

`GPULEASE_NODES_PER_GROUP` multiplies the burn rate, not the total: a two-node
group spends its budget twice as fast in wall-clock terms but cannot spend more
of it. It does raise the peak — twice as many instances running at once.

`GPULEASE_DEADLINE` bounds the calendar rather than the money: past it nothing
starts and the reaper destroys the fleet, so it stops the sum above from
repeating into next month.

**EBS is now only a running cost.** Volumes are destroyed with their instances,
so the storage bill tracks the compute bill instead of accumulating: 26 groups
× 2 nodes × 100 GB of gp3 costs about $0.57/hour while all of them happen to be
running, and nothing at all overnight. Under the old stop-don't-terminate model
those same volumes stood at roughly $416 a month whether anyone used them or
not.

So: pick `MAX_SESSION_HOURS` and `GPU_HOUR_QUOTA` by doing that arithmetic
against a number you are willing to pay, set an AWS Budget with an alert as a
second line of defence, and watch `./admin.py sessions` during the first
assignment to see what groups actually do.

---

`legacy-serverless.tar.gz` is the previous Lambda + DynamoDB + Terraform
implementation, kept only so nothing is lost. Delete it once you're happy.
