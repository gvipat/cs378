# `gpulease` — your group's GPU nodes

`gpulease` is how your group gets GPU machines for this assignment, and how you
give them back. You do not need an AWS account, an AWS login, or any Python
packages. One file, one token, four commands.

```
gpulease login <token>    save the token your instructor gave you
gpulease start            bring up your group's nodes, print an ssh line for each
gpulease status           what you have, how long it lasts, what it has cost
gpulease stop             shut the nodes down and stop the meter
```

Your whole **group shares one lease**. If a groupmate has already run `start`,
your `start` just hands you the same nodes and the same key — it does not launch
a second set. `stop` ends it for everybody.

---

## 1. Set up (once)

You need Python 3.9 or newer. Check with `python3 --version`.

Save the `gpulease.py` file your instructor gave you somewhere you can find it,
then log in with your personal token:

```bash
python3 gpulease.py login <your-token>
```

There is nothing to configure first. The course server's address is built into
the file — `gpulease --help` prints the one it will use. (If your instructor
ever moves the service, they will give you a new address and tell you to
`export GPULEASE_API=https://...`; until then you can ignore that.)

It prints your name, your group, and the assignment that is currently active.
The token is saved to `~/.config/gpulease/credentials`, readable only by you, and
you do not need to log in again.

**Your token is yours, not your group's.** Do not share it or paste it into a
chat. It identifies you; anyone holding it can end your group's session. If you
lose it, ask your instructor to reissue — nobody can look up the old one, not
even them.

### Optional: make it shorter to type

```bash
chmod +x gpulease.py
sudo mv gpulease.py /usr/local/bin/gpulease     # then just: gpulease status
```

The rest of this document writes `gpulease`; if you skipped this step, read that
as `python3 gpulease.py` everywhere.

---

## 2. Start your nodes

```bash
$ gpulease start
Requesting 2 instances for your group.......... ready

  group        7   assignment hw4
  state        RUNNING
  node0        ec2-3-88-1-2.compute-1.amazonaws.com   10.0.1.20   [running]
  node1        ec2-3-88-4-5.compute-1.amazonaws.com   10.0.1.34   [running]
  lease ends   in 7h 52m
  gpu-hours    4.5 used / 60 allowed   (55.5 left)
               counted per node, so 2 gpu-hours per hour running
  deadline     in 6d 3h

  ssh -A -i ~/.ssh/gpulease_7 ubuntu@ec2-3-88-1-2.compute-1.amazonaws.com   # node0
  ssh -A -i ~/.ssh/gpulease_7 ubuntu@ec2-3-88-4-5.compute-1.amazonaws.com   # node1

  MASTER_ADDR=10.0.1.20  MASTER_PORT=29500  NNODES=2
```

Booting takes a couple of minutes; the dots are the CLI waiting for `sshd` on
**every** node to answer, so when it says `ready` the ssh lines actually work. It
gives up after about seven minutes and tells you to run `gpulease status` —
that is not a failure, just a slow boot.

Copy the ssh line and paste it. That is the whole login procedure.

**The key.** `start` writes your group's private key to `~/.ssh/gpulease_<group>`.
It is generated fresh for this session and only opens these nodes. Use `-i` as
shown, or add it to your agent with `ssh-add ~/.ssh/gpulease_<group>`. If the
session ends and you start a new one, you get a **new** key and the old one
stops working — always copy the ssh line from the current `status` output rather
than one you saved last week.

**Use `-A`.** It forwards your key so you can hop from node0 to node1. The
private key is deliberately never copied onto the instances, so without `-A`,
`ssh node1` from node0 will not authenticate.

---

## 3. Working on the nodes

You are `ubuntu`, and you have `sudo`. It is an Ubuntu box; what is installed on
it is whatever image the course uses — `nvidia-smi` tells you what GPUs you have,
and your assignment handout tells you what else to expect. Anything you install
yourself survives a stop, along with the rest of the disk.

### The multi-node bits

If your lease is more than one node, everything a distributed run needs is
already on the boxes:

| Where | What |
|---|---|
| `$MASTER_ADDR`, `$MASTER_PORT` | the rendezvous address, already exported |
| `$NODE_RANK`, `$NNODES` | this node's rank, and how many there are |
| `$GPUS_PER_NODE` | GPUs you are allotted on this box |
| `/etc/gpulease/peers` | `10.0.1.20 node0` / `10.0.1.34 node1` |
| `/etc/gpulease/hostfile` | the same list in mpirun / DeepSpeed format |
| `/etc/hosts` | so `ssh node1` and `ping node0` just work |

A two-node run, one shell on each node:

```bash
torchrun --nnodes=$NNODES --node_rank=$NODE_RANK --nproc_per_node=$GPUS_PER_NODE \
         --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT train.py
```

Do not set `RANK` or `WORLD_SIZE` yourself — `torchrun` works those out per
process, and a stale value in your environment produces a hang that is genuinely
unpleasant to debug.

Your nodes can reach each other on every port. Nobody else's nodes can reach
them.

### What survives, and what does not

- **A stop keeps your disk.** Files under `/home/ubuntu` are still there when
  your group starts again (if your group has starts left — see below).
- **The end of the assignment does not.** After the deadline the nodes are shut
  down, and some time after that the disks are **deleted**. `gpulease status`
  warns you when this applies. There is no recovering anything afterwards — you
  cannot reach a stopped machine.
- **So push your work to git.** Every time. The disk is the only copy otherwise,
  and it is on a machine you do not own.

---

## 4. Check on your lease

```bash
gpulease status
```

Reading the output:

- **`lease ends in ...`** — your nodes are shut down automatically at this time,
  whether or not you are using them. If it says `(the assignment deadline)` or
  `(all the gpu-hours you have left)`, that is why the lease is shorter than you
  expected.
- **`gpu-hours`** — what your group has spent and what it has left. It is
  counted **per node**: a 2-node cluster spends 2 GPU-hours for every hour it is
  up, so a 60-hour budget is 30 hours of a 2-node cluster. The number moves while
  you are running; you are billed when the session stops.
- **`sessions n used / m allowed`** — only appears if your instructor rations
  starts. If it says you have one session, read section 5 before you type `stop`.
- **`deadline`** — when the assignment's machines go away for good.

`status` works from anywhere; you do not have to be logged into a node.

---

## 5. Stop when you are done

```bash
gpulease stop
```

This shuts down every node in your group's lease and stops the charges. It waits
until the shutdown is actually confirmed before it exits — if it warns that it
could not confirm, run `gpulease status` a minute later and check.

**Stopping is how you save budget.** Nothing on the machine notices that you have
walked away; an idle cluster costs exactly as much as a busy one, right up until
the lease expires. Stop it when you go to dinner.

**If your group gets one session,** `stop` is the end, not a pause. The CLI will
tell you so and make you type `stop` to confirm:

```
This is your group's last session for hw4.
It stops all 2 of your nodes.
Stopping it is permanent - 'gpulease start' will not work again.
Your files stay on the disk, but you will not be able to reach them.
Type 'stop' to confirm:
```

Anything other than `stop` leaves it running. Do not use `-y` to skip this
prompt unless you are certain.

**Tell your group before you stop.** One lease, shared — your `stop` ends their
run too.

---

## Troubleshooting

**`error: no saved token. Run: gpulease login <your-token>`**
You have not logged in on this machine yet, or you are on a different machine
than the one you logged in on. Run `login` again with your token.

**`error: Bad or missing token. Run: gpulease login`**
The token is wrong, or your access was revoked. Re-run `login`, pasting the token
carefully; if it still fails, email your instructor.

**`error: cannot reach the lease service: ...`**
Usually the service is down or your network is blocking it — try again, then ask
on the course forum, where you are probably not the only one. Run
`gpulease --help` to see which address it is trying: if that is not the one your
instructor named, you have an old copy of the file, or a stray `GPULEASE_API`
left in your shell (`unset GPULEASE_API` clears it).

**`error: Your CLI is out of date. Reinstall it and try again.`**
Download the current `gpulease.py` from the course page, replacing your copy.
Your token and your session are unaffected.

**`Group 7 has already used its one session for hw4.`**
Your group's lease for this assignment is over and cannot be restarted. Email
your instructor — restarts can be granted, but that is their call.

**`Group 7 has used its 60 GPU-hour budget for hw4.`**
You have spent the group's hours. Same answer: email your instructor.

**`Group 7 has 0.12 of its 60 GPU-hours left ... a session needs at least 15 minutes' worth`**
There is budget left, but not enough to boot a cluster and do anything with it,
so the start is refused rather than burning the remainder on a boot.

**`error: the nodes failed to come up.`**
Usually the cloud had no capacity for your instance type at that moment. This
costs you nothing — the start is refunded and no time is billed. Wait a minute
and try again; if it keeps failing, say so on the forum.

**`timed out waiting for the nodes.`**
A slow boot, not a failure. Run `gpulease status` in a minute; the ssh lines will
be there.

**`gpulease start` printed "Session already running for your group."**
A groupmate started it. Those are your nodes; the ssh lines are right there.

**ssh says `Permission denied (publickey)`**
You are using an old key file. Run `gpulease status` and use the `ssh` line it
prints — the key changes with every session.

**ssh from node0 to node1 says `Permission denied (publickey)`**
You connected without `-A`. Log out and reconnect with the `ssh -A ...` line.

**`WARNING: UNPROTECTED PRIVATE KEY FILE!`**
Something loosened the permissions on the key. `chmod 600 ~/.ssh/gpulease_<group>`.

**My node disappeared mid-session.**
If one node of a multi-node lease dies, the service stops the rest and closes
the session — a half cluster cannot finish the job and would bill in full while
failing to. Run `gpulease status` to confirm, then talk to your instructor about
a restart.

---

## Quick reference

```bash
gpulease login <token>                        # once per machine
gpulease start                                # bring up the group's nodes
gpulease status                               # lease, budget, ssh lines
gpulease stop                                 # shut down, stop the meter

ssh -A -i ~/.ssh/gpulease_<group> ubuntu@<node0>   # -A is not optional
```

| File | What |
|---|---|
| `~/.config/gpulease/credentials` | your saved token |
| `~/.ssh/gpulease_<group>` | this session's private key |

Anything not covered here: ask on the course forum. If it involves money, a lost
disk, or a session you cannot restart, email your instructor directly.
