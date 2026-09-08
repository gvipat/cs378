# UT CS 378 Assignment GPU Resource Management Script

## You can use `gpulease.py` to request two AWS g4dn.xlarge instances per group, with a budget of 40 GPU-hours.

You will receive a unique access token in your email. **Do not share it with
anyone.** With it, `gpulease.py` brings your group's nodes up, tells you how
much of your group's budget you have used, and shuts the nodes down again when
you are done.

## Running it

`gpulease.py` is a single file that runs on Python 3.9+. Download it from the
course page, then check what you have:

```bash
python3 --version
```

To install or upgrade: `brew install python3` on macOS, `sudo apt install
python3` on Ubuntu/Debian, or the python.org installer on Windows — tick "Add
python.exe to PATH", then type `py` wherever this file says `python3`.

## Your token

Save it once per machine:

```bash
python3 gpulease.py login <your-token>
```

If you lose it, ask your instructor to reissue — nobody can look up an existing
token, not even them.

## Requesting and releasing nodes

```
python3 gpulease.py start     bring up your group's nodes
python3 gpulease.py status    show the lease, the budget and the ssh commands
python3 gpulease.py stop      shut the nodes down and stop the charges
```

**`start`** prints an ssh command per node. It waits until every node is
accepting ssh, which takes a few minutes, so the commands work as soon as you
see them. If it gives up waiting, run `status` a minute later.

**`status`** shows every node with its ssh command, when the lease ends and
which limit ended it, the gpu-hours your group has used and has left, and the
assignment deadline. Gpu-hours are counted per node, so a two-node session
spends two of them for every hour it is up.

**`stop`** stops every node in the lease and ends the charges. **Your nodes are
ephemeral**: stopping discards the machine and everything on its disk, and the
next `start` gives your group a brand-new one. Idle nodes bill too, so stop
them as soon as you are done — but copy your work off first.

## Copy your files before you stop

Nothing on a node survives `stop`. Bundle what you want to keep, then pull it
down from your own machine:

```bash
# on the node
tar czf /tmp/hw.tar.gz --exclude=__pycache__ --exclude='*.pt' -C ~ code results

# on your own machine, using the key and host `status` printed
scp -i ~/.ssh/gpulease_<group> ubuntu@<node0>:/tmp/hw.tar.gz .
```

Check the tarball actually arrived before you run `stop`, and repeat for any
node you wrote to — each one has its own disk. Better still, push to git as you
go: the node is the only copy otherwise.

## One lease per group

Your group shares one lease. Whoever runs `start` first brings the nodes up;
everyone else's `start` hands back those same nodes and the same key. `stop`
ends the session for the whole group, so tell them before you run it.

Every node accepts the same key, so use `ssh -A` to hop between them.

## Files this keeps on your machine

```
~/.config/gpulease/credentials    your saved token
~/.ssh/gpulease_<group>           this session's key, replaced every session
```

Run `python3 gpulease.py --help` for the same summary at the terminal.
