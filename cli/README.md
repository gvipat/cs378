# UT CS 378 — GPU node request script

`gpulease.py` gets your group an AWS **g4dn.xlarge** (one NVIDIA T4) on demand
and hands it back when you are done. Your group shares a budget of **20
GPU-hours** for the assignment.

You will receive a unique access token by email. **It is yours personally — do
not share it or paste it into a group chat.** Anyone holding it can start and
stop your group's node and destroy what is on it.

You do not need an AWS account, and this script never asks for one.

---

## 1. Install

`gpulease.py` is a single file with no dependencies. It runs on **Python 3.9 or
newer**. Download it from the course page, then check what you have:

```bash
python3 --version
```

To install or upgrade: `brew install python3` on macOS, `sudo apt install
python3` on Ubuntu/Debian, or the python.org installer on Windows.

> **Windows:** tick *"Add python.exe to PATH"* during install, and type **`py`**
> wherever this file says `python3`. The python.org installer does not give you
> a `python3` command — typing it opens the Microsoft Store instead.

## 2. Save your token

Once per machine:

```bash
python3 gpulease.py login <your-token>
```

If you lose it, ask the course staff to reissue. Nobody can look up an existing
token, not even them — only a hash of it is stored.

## 3. Start, check, stop

```
python3 gpulease.py start     bring your group's node up
python3 gpulease.py status    lease, budget, and the ssh command
python3 gpulease.py stop      destroy the node and stop the charges
```

**`start`** waits until the node is actually accepting ssh — a few minutes — so
the command it prints works as soon as you see it. If it gives up waiting, the
node is usually still on its way up; run `status` a minute later. If your group
already has a session, `start` hands you that one rather than making a second.

**`status`** shows the node, its ssh command, when the lease ends and which
limit ends it, and what is left of your budget. Run it any time, from anywhere.

**`stop`** destroys the node. Read the next section before you run it.

---

## Your node is temporary. Read this part.

`stop`, the end of your lease, and the assignment deadline all **destroy the
node and its disk**. There is no snapshot, no backup, no undo. `start` then
gives you a brand-new, empty machine, so you will clone your repo and set up
again each session.

**Work in git and push before you stop.** That is the whole strategy.

For anything not in git, pull it down first:

```bash
# on the node
tar czf /tmp/hw.tar.gz --exclude=__pycache__ --exclude='*.pt' -C ~ code results

# on your own machine, using the key and host that `status` printed
scp -i ~/.ssh/gpulease_<group> ubuntu@<host>:/tmp/hw.tar.gz .
```

Check the file actually arrived before you run `stop`.

## The budget

Your group shares **20 GPU-hours**, spent by wall-clock time while the node is
up — whether or not anyone is using it. A node left running overnight costs
about eight of the twenty.

- **Start as often as you like.** Sessions cost nothing to create; only running
  time is charged. A mistaken `stop` costs you a few minutes of setup, not the
  assignment.
- **`stop` as soon as you are done.** It is the only thing that stops the meter.
  Nothing on the node does it for you, and there is no idle timeout.
- `status` shows what is left. When your remaining budget is shorter than the
  lease, it prints `lease ends in Xh Ym (all the gpu-hours you have left)` —
  that is the budget running out, not a fault.
- Below roughly 15 minutes of budget, `start` refuses rather than handing you a
  node that would die while it was still booting.

When the budget is gone that is the end of the assignment for your group, unless
you ask the course staff.

**Everything ends at the assignment deadline** regardless of budget: nothing
starts after it, and anything still running is destroyed. `status` shows the
deadline and how long is left.

## One lease per group

Your group shares one lease. Whoever runs `start` first brings the node up;
everyone else's `start` hands back that same node and the same key. **`stop`
ends the session for your whole group, not just for you** — tell them first.

Each group is on its own private network and cannot reach any other group's
node.

> A later assignment may give your group more than one node at once. Nothing
> here changes if it does: `status` will list `node0`, `node1`, … each with its
> own ssh command, and they all accept the same key, so `ssh -A` hops between
> them. Budget is counted **per node**, so two nodes spend the twenty hours
> twice as fast.

---

## Connecting from VS Code

VS Code's Remote-SSH ignores the `ssh` command that `status` prints — it reads
`~/.ssh/config`. Add an entry:

```
Host cs378
    HostName <the address status printed>
    User ubuntu
    IdentityFile ~/.ssh/gpulease_<group>
    IdentitiesOnly yes
    ServerAliveInterval 30
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
```

Then *Remote-SSH: Connect to Host…* → `cs378`.

Three things worth knowing:

1. **`HostName` changes every session.** The node is destroyed on `stop` and
   rebuilt on `start` with a new address. After each `start`, run `status` and
   paste the new address in. A stale address is the most common reason
   Remote-SSH appears to hang.
2. **The last two lines are deliberate.** These nodes are single-use and AWS
   reuses addresses, so the host key legitimately differs every session. Without
   them you will eventually hit `REMOTE HOST IDENTIFICATION HAS CHANGED` and
   Remote-SSH fails with no useful message. They apply to this one host only.
   On Windows write `UserKnownHostsFile NUL` instead of `/dev/null`.
3. **VS Code installs ~100 MB of its own server onto the node** on the first
   connect of every session, because the disk is new each time. That first
   connect is slow; later ones in the same session are not.

> **Windows + WSL:** if you run `gpulease.py` **inside WSL** but VS Code on
> **Windows**, the two do not share a `.ssh` directory and Remote-SSH cannot
> read your key. Either run the script in Windows PowerShell (`py gpulease.py
> start`), or copy the key across after each `start`:
>
> ```bash
> cp ~/.ssh/gpulease_<group> /mnt/c/Users/<you>/.ssh/
> ```
>
> and point `IdentityFile` at `C:/Users/<you>/.ssh/gpulease_<group>`.

## If something goes wrong

| What you see | What it means |
|---|---|
| `Your CLI is out of date` | Download `gpulease.py` from the course page again. |
| `Bad or missing token` | Run `login` again; check you copied the whole token. |
| `cannot reach the lease service` | Check your network; if it persists, ask on the forum. |
| `Launch failed (…)` | Tell the course staff and quote the `ref` in the message. |
| ssh: `Permission denied (publickey)` | Run `start` again — it rewrites the key file. |
| ssh: `error in libcrypto` | Old copy of this script. Download it again, then `start`. |
| Remote-SSH hangs | Stale `HostName`. Run `status` and update `~/.ssh/config`. |

`start` taking several minutes is normal. `start` refusing outright is about
budget or the deadline, not a fault — the message says which.

## Files this keeps on your machine

```
~/.config/gpulease/credentials    your saved token
~/.ssh/gpulease_<group>           this session's key, replaced every session
```

Both are readable only by you. Run `python3 gpulease.py --help` for the same
summary at the terminal.
