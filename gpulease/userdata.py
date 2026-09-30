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
on boxes that hold no AWS keys. The lookup runs in the background
(gpulease-peers) rather than in bootcmd itself, because IMDS can take minutes
to show those tags; see the comment in BOOT_SCRIPT.

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
# The control plane tags this instance with its rank (NodeIndex) and with the
# private addresses of every node in the group (NodePeers). Tags come back over
# IMDS, which is link-local and needs no credentials - which is the whole point
# on a box that deliberately holds no AWS keys, and why this is not an agent
# phoning home.
#
# Those two tags are NOT looked up here. They are added by a CreateTags call
# after RunInstances returns, and IMDS does not reflect that call straight away:
# tags passed to RunInstances itself are there from the first moment, but
# NodeIndex/NodePeers can lag well past anything bootcmd can afford to wait,
# because cloud-init -- and with it the default user and its SSH key -- waits
# for bootcmd to finish.
#
# Not theoretical. On 2026-09-30 both nodes of a two-node lease came up with no
# peer list: cloud-init's own IMDS crawl, 47s after boot, returned the launch
# tags but not NodeIndex/NodePeers (and the launch-time Name, not the per-node
# one CreateTags writes), and the old inline lookup then spent its whole ~50s
# retry budget without seeing them. The same command answered instantly some
# minutes later. An earlier version of that lookup also fetched its IMDS token
# only once, which failed the same way for a different reason; the token is
# still fetched once per attempt below.
#
# So bootcmd only clears the previous boot's cluster state and starts
# gpulease-peers in the background. That keeps asking IMDS for as long as it
# takes and writes the cluster files once the answer arrives. Until then, the
# profile.d shim tells anyone who logs in that the cluster info is not there
# yet, instead of leaving NODE_RANK silently empty.

# Clear the previous boot's cluster state BEFORE deciding whether we have a new
# one. A fresh instance has none, so this is about reboots: if the IMDS lookup
# comes up empty this time, the old files would otherwise sit there pointing at
# addresses that now belong to somebody else's cluster. Stale peers are worse
# than no peers -- no peers fails immediately, stale peers hang.
rm -f /etc/gpulease/peers /etc/gpulease/hostfile /etc/gpulease/env
sed -i '/ # gpulease$/d' /etc/hosts
install -d -m 755 /etc/gpulease

# Static on purpose: the values live in /etc/gpulease/env, which gpulease-peers
# writes last, so its presence means the peers file and /etc/hosts are ready.
cat > /etc/profile.d/gpulease-cluster.sh <<'PROFILE_EOF'
# gpulease: this node's cluster settings. POSIX sh, sourced by login shells.
if [ -r /etc/gpulease/env ]; then
  . /etc/gpulease/env
else
  case $- in
    *i*)
      echo "gpulease: cluster info (NODE_RANK, MASTER_ADDR, ...) is not available yet." >&2
      echo "gpulease: it usually appears within a few minutes of boot. Log in again, or" >&2
      echo "gpulease: run  . /etc/profile.d/gpulease-cluster.sh  once /etc/gpulease/env exists." >&2
      ;;
  esac
fi
PROFILE_EOF
chmod 644 /etc/profile.d/gpulease-cluster.sh

# Rank and cluster size are only known once IMDS has answered, so the motd is
# kept as a template and filled in by gpulease-peers.
cat > /etc/gpulease/motd.in <<'MOTD_EOF'
@@MOTD@@
MOTD_EOF
sed "s/%RANK%/?/g; s/%NODES%/?/g" /etc/gpulease/motd.in > /etc/motd

cat > /usr/local/sbin/gpulease-peers <<'PEERS_EOF'
#!/bin/sh
# Waits for this node's NodeIndex/NodePeers tags to show up in IMDS, then
# writes the cluster files. Started in the background by cloud-init's bootcmd
# on every boot; the comment there says why it is not done inline.
set -u
SLOTS=${1:-1}

IMDS=http://169.254.169.254/latest
TOKEN=""
imds_token() {
  curl -sf -X PUT --max-time 5 "$IMDS/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' || true
}
meta() {
  curl -sf --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" "$IMDS/meta-data/$1" || true
}

# 15 minutes is far past any delay seen so far, and still short enough that a
# node whose tags never arrive stops polling while its student is around to
# see the profile.d warning.
DEADLINE=$(( $(date +%s) + 900 ))
while :; do
  TOKEN=$(imds_token)
  if [ -n "$TOKEN" ]; then
    PEERS=$(meta tags/instance/NodePeers)
    RANK=$(meta tags/instance/NodeIndex)
    if [ -n "$PEERS" ] && [ -n "$RANK" ]; then break; fi
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "gave up: NodeIndex/NodePeers never appeared in IMDS" >&2
    exit 1
  fi
  sleep 3
done
echo "NodeIndex=$RANK NodePeers=$PEERS"

N=0
: > /etc/gpulease/peers.tmp
: > /etc/gpulease/hostfile.tmp
sed -i '/ # gpulease$/d' /etc/hosts
for IP in $(echo "$PEERS" | tr ',' ' '); do
  echo "$IP node$N" >> /etc/gpulease/peers.tmp
  echo "$IP slots=$SLOTS" >> /etc/gpulease/hostfile.tmp
  echo "$IP node$N # gpulease" >> /etc/hosts
  N=$((N + 1))
done
mv /etc/gpulease/peers.tmp /etc/gpulease/peers
mv /etc/gpulease/hostfile.tmp /etc/gpulease/hostfile

MASTER=$(echo "$PEERS" | cut -d, -f1)
# MASTER_ADDR/MASTER_PORT are what torch.distributed's env:// rendezvous
# reads. WORLD_SIZE and RANK are deliberately NOT set here: torchrun computes
# them per process and a stale value in the environment breaks the job in a
# way that is genuinely hard to debug.
cat > /etc/gpulease/env.tmp <<ENV_EOF
export MASTER_ADDR=$MASTER
export MASTER_PORT=@@MASTER_PORT@@
export NODE_RANK=$RANK
export NNODES=$N
export GPUS_PER_NODE=$SLOTS
ENV_EOF
chmod 644 /etc/gpulease/env.tmp
mv /etc/gpulease/env.tmp /etc/gpulease/env

sed "s/%RANK%/$RANK/g; s/%NODES%/$N/g" /etc/gpulease/motd.in > /etc/motd
PEERS_EOF
chmod 755 /usr/local/sbin/gpulease-peers

# A transient unit outlives bootcmd without cloud-init waiting on it, and its
# output lands in `journalctl -u gpulease-peers`. Stopping first makes a manual
# re-run (`cloud-init single --name bootcmd`) replace a lookup still in flight.
# The fallback detaches every fd, because cloud-init waits for its child's
# output pipe to close and would otherwise sit out the whole lookup.
systemctl stop gpulease-peers >/dev/null 2>&1
if ! systemd-run --no-block --collect --unit=gpulease-peers /usr/local/sbin/gpulease-peers "$SLOTS" >/dev/null 2>&1; then
  setsid /usr/local/sbin/gpulease-peers "$SLOTS" </dev/null >/dev/null 2>&1 &
fi
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
