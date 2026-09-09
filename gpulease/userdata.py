"""The cloud-init user-data handed to a student instance.

Its one real job is installing this session's SSH key. That happens in
`bootcmd`, the one cloud-init module with ALWAYS frequency, so it re-runs on
every boot rather than only the first.

That used to be load-bearing: the control plane rewrote a *stopped* instance's
user-data and the next boot picked up a fresh key, which is how a resumed
instance got a new key. There are no resumes any more -- every session is a new
instance from the AMI -- so a per-instance module would now be enough. It stays
in bootcmd anyway: it is idempotent, it costs nothing, and it puts the key and
the peer files back after a reboot, which a student may well do mid-session.

Its other job is telling the node who its peers are. The control plane tags
each instance with its rank and the private addresses of the whole group, and
the node reads them back from IMDS -- link-local, no credentials, no call to
the control plane. That is what makes a distributed-training cluster possible
on boxes that hold no AWS keys.

Student instances hold no AWS credentials and run no gpulease agent. Nothing
here phones home, and nothing here ends a session on a schedule. Enforcing the
end of one is entirely the control plane's job (`api.stop`, or the reaper at
lease expiry), which is the only place it can be enforced against a student who
has root. A student can of course destroy their own node from inside it -- an
OS-level shutdown terminates -- but that only ever spends less money.

Templating is by @@SENTINEL@@ replacement rather than str.format, because the
payload is full of shell ${braces}.
"""

import re
import textwrap

from . import config

MOTD = """
  @@COURSE@@ GPU instance - group lease, node %RANK% of %NODES%
  ------------------------------------------------------------
  THIS MACHINE IS TEMPORARY. It is destroyed - disk and all -
  when your lease ends or anyone in your group runs
  'gpulease stop'. Nothing here is backed up or kept.
  Push your work to git before you stop.

  Run 'gpulease status' on your own machine to see how long is
  left. 'gpulease start' after that gives you a NEW, empty node.

  Your group's nodes:   cat /etc/gpulease/peers
  They are also in /etc/hosts as node0, node1, ...
  MASTER_ADDR / MASTER_PORT / NODE_RANK / NNODES are exported for
  you; `torchrun --nnodes=$NNODES --node_rank=$NODE_RANK
  --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT ...`

  ssh between nodes uses the same key you used to get here, so
  connect with `ssh -A` and it will be forwarded for you.
  ------------------------------------------------------------
"""

BOOT_SCRIPT = """#!/bin/sh
# Runs on every boot (cloud-init bootcmd). POSIX sh: cloud-init may hand this
# to dash, so no bashisms here.
#
# `set -u` but deliberately NOT `set -e`. The steps below are independent and
# aborting the whole script on one failure helps nobody.
set -u

# Install this session's key.
#
# bootcmd runs early in cloud-init's init stage - BEFORE the users-groups
# module - so on a first boot the default user does not exist yet and there is
# nothing to install a key for. That case is covered by ssh_authorized_keys in
# the cloud-config above, which the ssh module applies a few moments later.
# On a reboot the user exists and this is what puts the key back.
#
# Every node in the group gets this same payload, so one key opens all of them.
if id -u ubuntu >/dev/null 2>&1; then
  install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
  printf '%s\\n' '@@PUBLIC_KEY@@' > /home/ubuntu/.ssh/authorized_keys
  chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
  chmod 600 /home/ubuntu/.ssh/authorized_keys
fi

# Pin the group to its allotted GPUs when the box has more than they are owed.
# Not a security boundary, just a default that matches the spec.
TOTAL=$(nvidia-smi --list-gpus 2>/dev/null | wc -l || echo 0)
if [ "$TOTAL" -gt "@@GPUS@@" ]; then
  echo "export CUDA_VISIBLE_DEVICES=$(seq -s, 0 $((@@GPUS@@ - 1)))" \\
    > /etc/profile.d/gpulease-gpus.sh
  SLOTS=@@GPUS@@
elif [ "$TOTAL" -gt 0 ]; then
  SLOTS=$TOTAL
else
  SLOTS=1
fi

# Who else is in this cluster.
#
# The control plane tags this instance with its rank and with the private
# addresses of every node in the group. Tags come back over IMDS, which is
# link-local and needs no credentials - which is the whole point on a box that
# deliberately holds no AWS keys, and why this is not an agent phoning home.
#
# The tags are written moments after RunInstances returns, so this boot can
# genuinely lose the race -- and every boot is now a first boot, since each
# session launches new instances. Hence the retry, and hence nothing below
# being fatal when the tags never turn up: a student with no peers file still
# has a working machine.
IMDS=http://169.254.169.254/latest
TOKEN=""
imds_token() {
  curl -sf -X PUT --max-time 5 "$IMDS/api/token" \\
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' || true
}
meta() {
  curl -sf --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" "$IMDS/meta-data/$1" || true
}

# The token is fetched INSIDE the loop, once per attempt. It used to be fetched
# once above it, and that was a silent single point of failure: bootcmd runs
# while the network is still coming up, so the token PUT can time out, and an
# empty token makes every later read a 401. The twenty retries below then could
# not recover -- they kept presenting the same empty token -- and the node came
# up with no peer list, no MASTER_ADDR and a motd claiming it was alone.
#
# Not theoretical. Caught in the act on a live instance: cloud-init logged
# `config-bootcmd ran successfully and took 60.838 seconds`, the full retry
# budget, having read nothing, on a box whose NodePeers tag answered instantly
# from the same command a few minutes later.
#
# 5s rather than 2s for the same reason -- early boot is exactly when the stack
# is slowest and a 2s ceiling is tightest.
PEERS=""
TRIES=0
while [ "$TRIES" -lt 20 ]; do
  TOKEN=$(imds_token)
  if [ -n "$TOKEN" ]; then
    PEERS=$(meta tags/instance/NodePeers)
    if [ -n "$PEERS" ]; then break; fi
  fi
  TRIES=$((TRIES + 1))
  sleep 2
done

RANK=$(meta tags/instance/NodeIndex)
if [ -z "$RANK" ]; then RANK=0; fi

# Clear the previous boot's cluster state BEFORE deciding whether we have a new
# one. A fresh instance has none, so this is about reboots: if the IMDS lookup
# above came up empty this time, the old files would otherwise sit there
# pointing at addresses that now belong to somebody else's cluster. Stale peers
# are worse than no peers -- no peers fails immediately, stale peers hang.
rm -f /etc/gpulease/peers /etc/gpulease/hostfile /etc/profile.d/gpulease-cluster.sh
sed -i '/ # gpulease$/d' /etc/hosts

N=0
if [ -n "$PEERS" ]; then
  install -d -m 755 /etc/gpulease

  for IP in $(echo "$PEERS" | tr ',' ' '); do
    echo "$IP node$N" >> /etc/gpulease/peers
    echo "$IP slots=$SLOTS" >> /etc/gpulease/hostfile
    echo "$IP node$N # gpulease" >> /etc/hosts
    N=$((N + 1))
  done

  MASTER=$(echo "$PEERS" | cut -d, -f1)
  # MASTER_ADDR/MASTER_PORT are what torch.distributed's env:// rendezvous
  # reads. WORLD_SIZE and RANK are deliberately NOT set here: torchrun computes
  # them per process and a stale value in the environment breaks the job in a
  # way that is genuinely hard to debug.
  cat > /etc/profile.d/gpulease-cluster.sh <<CLUSTER_EOF
export MASTER_ADDR=$MASTER
export MASTER_PORT=@@MASTER_PORT@@
export NODE_RANK=$RANK
export NNODES=$N
export GPUS_PER_NODE=$SLOTS
CLUSTER_EOF
  chmod 644 /etc/profile.d/gpulease-cluster.sh
fi
if [ "$N" -eq 0 ]; then N=1; fi

cat > /etc/motd <<'MOTD_EOF'
@@MOTD@@
MOTD_EOF
# Filled in here rather than by the control plane: rank and cluster size are
# only known once IMDS has answered, above.
sed -i "s/%RANK%/$RANK/g; s/%NODES%/$N/g" /etc/motd
"""

# The key is interpolated into single-quoted shell and double-quoted YAML, so a
# quote or newline in it would break out of both. ssh-keygen never produces one,
# but the comment field carries a group_id that came from the instructor's
# roster CSV, so this is checked rather than assumed.
PUBKEY_RE = re.compile(r"^ssh-(ed25519|rsa) [A-Za-z0-9+/]+=* ?[\w.@-]*$")


def render(public_key: str) -> str:
    """Build the full cloud-config document for one session."""
    public_key = public_key.strip()
    if not PUBKEY_RE.match(public_key):
        raise ValueError(f"refusing to embed a malformed public key: {public_key[:60]!r}")

    subs = {
        "@@PUBLIC_KEY@@": public_key,
        "@@GPUS@@": str(config.GPUS_PER_NODE),
        "@@MASTER_PORT@@": str(config.MASTER_PORT),
        "@@COURSE@@": config.COURSE,
    }

    def fill(text):
        for k, v in subs.items():
            text = text.replace(k, v)
        return text

    script = fill(BOOT_SCRIPT.replace("@@MOTD@@", fill(MOTD.strip("\n"))))

    # The bootcmd entry is a YAML literal block: every line indented by four,
    # which YAML strips again, so heredoc terminators land back in column 0.
    return (
        "#cloud-config\n"
        # First boot also gets the key the normal way, in case anything in
        # cloud-init reorders around bootcmd.
        "ssh_authorized_keys:\n"
        f'  - "{public_key}"\n'
        "\n"
        "bootcmd:\n"
        "  - |\n" + textwrap.indent(script, "    ")
    )
