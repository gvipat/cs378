# Testing the two-node lease (assignment 2)

You need your token and `~/.ssh/gpu-lease.pem`. Run everything below from the
repo root.

```bash
git clone https://github.com/gvipat/cs378.git
cd cs378
```

## Connect

Terminal 1, leave running:

```bash
chmod 600 ~/.ssh/gpu-lease.pem
ssh -i ~/.ssh/gpu-lease.pem -N -L 8001:127.0.0.1:8001 ubuntu@32.186.114.60
```

Terminal 2, from the repo root:

```bash
export GPULEASE_API=http://127.0.0.1:8001
python3 cli/gpulease.py login <token>
```

## Start

```bash
python3 cli/gpulease.py start
```

Prints an `ssh -A -i …` line per node once both are up. `status` reprints it.

## Check, on node0

| command | expected |
| --- | --- |
| `nvidia-smi` | one Tesla T4 |
| `cat /etc/gpulease/peers` | two lines, `node0` and `node1` |
| `cat /etc/gpulease/hostfile` | both IPs, `slots=1` |
| `echo $NODE_RANK $NNODES` | `0 2` |
| `echo $MASTER_ADDR` | node0's private IP |
| `ssh -A node1` | lands on node1, `$NODE_RANK` is `1` |

## Stop

```bash
python3 cli/gpulease.py stop
```

Confirm with `stop`. Both nodes and their disks are destroyed.
