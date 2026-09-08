"""Everything that talks to EC2.

Credentials come from the control-plane host's instance profile (see
iam-policy.json). Every call is scoped to instances tagged Course=<COURSE>:
that tag is the blast radius, and nothing here should ever act on an instance
without it.

Deliberately absent: launch templates, SSM, and any IAM role on the student
instances themselves. run_instances takes its parameters inline, the session
key travels in user-data, and the instance stops itself with `poweroff`.
"""

import hashlib
import logging
import re
import socket
import threading
import urllib.request

import boto3
from botocore.exceptions import ClientError

from . import config, userdata

log = logging.getLogger("gpulease.aws")

_cache: dict = {}
_client_lock = threading.Lock()


class _LazyClient:
    """Builds the boto3 client on first use rather than at import.

    Credential resolution can be slow, and can fail outright before the host's
    IAM role is attached. Loading the roster or initialising the database has
    no business caring about either.

    Locked because the reaper thread and a request thread can reach this at the
    same moment, and building a client off the shared default session is not
    thread-safe.
    """

    def __getattr__(self, name):
        with _client_lock:
            if "client" not in _cache:
                _cache["client"] = boto3.client("ec2", region_name=config.REGION)
        return getattr(_cache["client"], name)


ec2 = _LazyClient()

STATES_ALL = ("pending", "running", "stopping", "stopped")


class RetryLater(Exception):
    """A transient condition the student should just wait out."""


# ---------------------------------------------------------------- tags


def course_tags(extra: dict | None = None) -> list[dict]:
    tags = {"Course": config.COURSE, "ManagedBy": "gpu-lease"}
    tags.update(extra or {})
    return [{"Key": k, "Value": str(v)} for k, v in tags.items()]


def tag_spec(resource_type: str, extra: dict | None = None) -> dict:
    return {"ResourceType": resource_type, "Tags": course_tags(extra)}


def instance_tag(instance: dict, key: str, default=None):
    for t in instance.get("Tags", []):
        if t["Key"] == key:
            return t["Value"]
    return default


# ---------------------------------------------------------------- lookups


def _imds(path, token=None):
    """One IMDSv2 read. None if this is not an EC2 instance, or the key is absent."""
    try:
        if token is None:
            req = urllib.request.Request(
                "http://169.254.169.254/latest/api/token",
                method="PUT",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            )
            with urllib.request.urlopen(req, timeout=1) as r:
                token = r.read().decode()
        req = urllib.request.Request(
            f"http://169.254.169.254/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(req, timeout=1) as r:
            return r.read().decode().strip() or None
    except Exception:
        return None


def _self_sources(vpc_id):
    """How our readiness probe appears to a student instance's security group.

    Returns (security_group_ids, cidrs).

    The private address is the one that matters and the easy one to get wrong:
    inside a VPC an instance's public DNS name resolves to its *private* IP, so
    a probe from this host arrives from our private address, not our public one.
    Referencing this host's own security group is better still - it survives
    this host being stopped and started, which changes its public IP.

    That reference only works within one VPC, though. Across VPCs EC2 rejects
    it, and since this runs on the path to every launch, a rejected reference
    would take the whole service down. So it is dropped unless the VPCs match.
    """
    if "self" in _cache:
        return _cache["self"]

    sgs, cidrs = set(), set()
    mac = _imds("mac")
    if mac and _imds(f"network/interfaces/macs/{mac}/vpc-id") == vpc_id:
        ids = _imds(f"network/interfaces/macs/{mac}/security-group-ids")
        if ids:
            sgs.update(ids.split())
    for key in ("local-ipv4", "public-ipv4"):
        ip = _imds(key)
        if ip:
            cidrs.add(f"{ip}/32")

    if not sgs and not cidrs:
        log.warning(
            "could not read this host's identity from IMDS; the readiness probe "
            "will only work if GPULEASE_ALLOWED_SSH_CIDRS already covers this host"
        )
    _cache["self"] = (sgs, cidrs)
    return _cache["self"]


def default_vpc_id():
    if "default_vpc" not in _cache:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise RuntimeError("no default VPC in this region; set GPULEASE_SUBNET_IDS")
        _cache["default_vpc"] = vpcs[0]["VpcId"]
    return _cache["default_vpc"]


def vpc_id():
    """The one VPC this deployment lives in.

    Taken from GPULEASE_SUBNET_IDS when it is set, because the security groups
    have to be created in the same VPC as the subnets we launch into -- pinning
    them to the *default* VPC while launching somewhere else fails every
    launch. Falls back to the default VPC, which is the untouched behaviour for
    anyone who has not set the subnets.

    Subnets spanning two VPCs is rejected outright rather than half-working: a
    group's nodes have to share a subnet, so a set we cannot reason about as
    one network is a configuration error.
    """
    if "vpc" in _cache:
        return _cache["vpc"]
    if config.SUBNET_IDS:
        subnets = ec2.describe_subnets(SubnetIds=config.SUBNET_IDS)["Subnets"]
        vpcs = {s["VpcId"] for s in subnets}
        if len(vpcs) != 1:
            raise RuntimeError(
                f"GPULEASE_SUBNET_IDS spans {len(vpcs)} VPCs ({sorted(vpcs)}); "
                f"it must name subnets in exactly one"
            )
        _cache["vpc"] = vpcs.pop()
    else:
        _cache["vpc"] = default_vpc_id()
    return _cache["vpc"]


def subnet_ids():
    """Subnets to try, in order. Trying more than one is how we survive a
    single-AZ capacity shortage on GPU types."""
    if config.SUBNET_IDS:
        return config.SUBNET_IDS
    if "subnets" not in _cache:
        subnets = ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id()]}]
        )["Subnets"]
        # Instances need a public IP for students to reach them, and we pass a
        # top-level SubnetId (which forbids a NetworkInterfaces block), so the
        # subnet has to assign one itself.
        public = [s["SubnetId"] for s in subnets if s.get("MapPublicIpOnLaunch")]
        _cache["subnets"] = public or [s["SubnetId"] for s in subnets]
        if not _cache["subnets"]:
            raise RuntimeError("no usable subnets found")
    return _cache["subnets"]


def image():
    """(ami_id, root_device_name). Root device differs between AMIs and getting
    it wrong makes the volume-size setting silently do nothing."""
    if "image" not in _cache:
        if config.AMI_ID:
            imgs = ec2.describe_images(ImageIds=[config.AMI_ID])["Images"]
        else:
            imgs = ec2.describe_images(
                Owners=["099720109477"],  # Canonical
                Filters=[
                    {
                        "Name": "name",
                        "Values": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"],
                    },
                    {"Name": "state", "Values": ["available"]},
                ],
            )["Images"]
            imgs.sort(key=lambda i: i["CreationDate"], reverse=True)
        if not imgs:
            raise RuntimeError("no matching AMI found")
        _cache["image"] = (imgs[0]["ImageId"], imgs[0].get("RootDeviceName", "/dev/sda1"))
    return _cache["image"]


# EC2 accepts only this character set in a security-group description AND in
# an individual rule's description. Note what is NOT in it: the apostrophe.
# "gpulease: this group's own nodes" reads fine, passes review, and fails every
# launch with "Invalid rule description" -- so descriptions go through
# _sg_text() rather than being trusted to be plain enough.
_SG_TEXT_BAD = re.compile(r"[^A-Za-z0-9 ._:/()#,@\[\]+=&;{}!$*\-]")


def _sg_text(text: str) -> str:
    """Make a string safe to use as a security-group or rule description."""
    return _SG_TEXT_BAD.sub("", text)[:255]


def _ssh_rule(**kw):
    return dict(IpProtocol="tcp", FromPort=22, ToPort=22, **kw)


def security_group_id():
    """Find or create the student security group, and reconcile its SSH ingress
    to match the configuration.

    Reconcile, not just add: narrowing GPULEASE_ALLOWED_SSH_CIDRS has to
    actually close the range it replaced. An operator who tightens the config,
    restarts, and is still wide open to the internet is worse off than one who
    never tightened it, because they now believe they are safe.

    Only tcp/22 IPv4 ranges are managed. Anything else on the group - another
    port you opened deliberately, an IPv6 rule - is left alone.
    """
    if "sg" in _cache:
        return _cache["sg"]

    name = f"{config.COURSE}-instances"
    vpc = vpc_id()
    found = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc]}]
    )["SecurityGroups"]
    if found:
        group = found[0]
    else:
        sg_id = ec2.create_security_group(
            GroupName=name,
            Description=_sg_text("SSH to student GPU instances"),
            VpcId=vpc,
            TagSpecifications=[tag_spec("security-group")],
        )["GroupId"]
        log.info("created security group %s (%s)", name, sg_id)
        group = {"GroupId": sg_id, "IpPermissions": []}
    sg_id = group["GroupId"]

    self_sgs, self_cidrs = _self_sources(vpc)
    want_cidrs = set(config.ALLOWED_SSH_CIDRS) | self_cidrs

    have_cidrs, have_sgs = set(), set()
    for perm in group.get("IpPermissions", []):
        if perm.get("IpProtocol") != "tcp" or perm.get("FromPort") != 22 or perm.get("ToPort") != 22:
            continue
        have_cidrs.update(r["CidrIp"] for r in perm.get("IpRanges", []))
        have_sgs.update(p["GroupId"] for p in perm.get("UserIdGroupPairs", []))
        if perm.get("Ipv6Ranges"):
            log.warning(
                "%s has IPv6 SSH rules that gpulease does not manage: %s",
                name, [r["CidrIpv6"] for r in perm["Ipv6Ranges"]],
            )

    add_cidrs = want_cidrs - have_cidrs
    drop_cidrs = have_cidrs - want_cidrs
    add_sgs = self_sgs - have_sgs

    if add_cidrs or add_sgs:
        grants = {}
        if add_cidrs:
            grants["IpRanges"] = [
                {"CidrIp": c, "Description": _sg_text("gpulease")}
                for c in sorted(add_cidrs)
            ]
        if add_sgs:
            grants["UserIdGroupPairs"] = [
                {"GroupId": g, "Description": _sg_text("gpulease control plane")}
                for g in sorted(add_sgs)
            ]
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[_ssh_rule(**grants)])
        log.info("%s: opened ssh to %s", name, sorted(add_cidrs) + sorted(add_sgs))

    if drop_cidrs:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[_ssh_rule(IpRanges=[{"CidrIp": c} for c in sorted(drop_cidrs)])],
        )
        log.info("%s: revoked ssh from %s (no longer in config)", name, sorted(drop_cidrs))

    _cache["sg"] = sg_id
    return sg_id


def _group_sg_name(group_id):
    """A security-group name derived from a group id out of the roster CSV.

    Group ids are whatever the instructor typed, so they are sanitised rather
    than trusted, and a group whose id had to be changed (or truncated) to make
    a name gets a hash of the original appended -- otherwise "team a" and
    "team/a" would collapse onto one group's network.

    The hash is joined with "." specifically. Sanitising can only ever produce
    [A-Za-z0-9_-], so a dot cannot appear in the sanitised part, so a hashed
    name cannot collide with the plain name of some other group. Joining with
    "-" left exactly that hole: group "team/a" and a group literally called
    "team-a-<that hash>" both produced "...-team-a-<hash>", and two groups
    sharing one all-traffic security group is the failure this whole function
    exists to prevent.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", group_id)[:32]
    if safe != group_id:
        safe = f"{safe}.{hashlib.sha1(group_id.encode()).hexdigest()[:6]}"
    return f"{config.COURSE}-cluster-{safe}"


def cluster_security_group_id(group_id):
    """The group's private network: one security group per group, allowing all
    traffic from itself.

    Distributed training needs the nodes to reach each other on whatever ports
    the launcher picks -- torchrun's rendezvous, NCCL's data connections, an
    mpirun daemon -- so port-by-port rules are not an option; it is all traffic
    or nothing.

    Per group rather than one shared group for the whole course, because the
    shared alternative would let every group reach every other group's nodes on
    every port. On a class network where the usual state of a training box is
    an unauthenticated rendezvous endpoint and a Jupyter server, that is not a
    theoretical difference.
    """
    key = f"cluster_sg:{group_id}"
    if key in _cache:
        return _cache[key]

    name = _group_sg_name(group_id)
    vpc = vpc_id()
    found = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc]}]
    )["SecurityGroups"]
    if found:
        group = found[0]
    else:
        try:
            sg_id = ec2.create_security_group(
                GroupName=name,
                # The sanitised name, not the raw group id: security-group
                # descriptions take a restricted character set, and a roster
                # with "Team Avila" or "grp#3" in it would otherwise fail
                # CreateSecurityGroup and so fail every launch for that group.
                Description=_sg_text(f"intra-group traffic for {name}"),
                VpcId=vpc,
                TagSpecifications=[tag_spec("security-group", {"Group": group_id})],
            )["GroupId"]
            log.info("created cluster security group %s (%s)", name, sg_id)
            group = {"GroupId": sg_id, "IpPermissions": []}
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
                raise
            group = ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [name]},
                    {"Name": "vpc-id", "Values": [vpc]},
                ]
            )["SecurityGroups"][0]
    sg_id = group["GroupId"]

    # The self-reference is what makes this a cluster network: members can talk
    # to members, and nothing else can talk to any of them through this group.
    has_self_all = any(
        perm.get("IpProtocol") == "-1"
        and any(pair.get("GroupId") == sg_id for pair in perm.get("UserIdGroupPairs", []))
        for perm in group.get("IpPermissions", [])
    )
    if not has_self_all:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "-1",
                    "UserIdGroupPairs": [
                        {
                            "GroupId": sg_id,
                            "Description": _sg_text("gpulease: nodes in this group"),
                        }
                    ],
                }
            ],
        )
        log.info("%s: allowed all traffic between the group's own nodes", name)

    _cache[key] = sg_id
    return sg_id


def course_instances(states=STATES_ALL):
    """Every instance this system is responsible for, straight from EC2.

    The reaper trusts this over the database: EC2 is what costs money."""
    out = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": "tag:Course", "Values": [config.COURSE]},
            {"Name": "instance-state-name", "Values": list(states)},
        ]
    ):
        for res in page["Reservations"]:
            out.extend(res["Instances"])
    return out


def describe_one(instance_id: str):
    try:
        return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    except (ClientError, IndexError, KeyError):
        return None


def describe_many(instance_ids) -> dict:
    """{instance_id: instance} for the ids that still exist.

    One call for the whole cluster rather than one per node. A single unknown
    id fails the entire batch, so that case falls back to describing them one
    at a time: one node that has gone missing must not blind us to the others,
    which are still running and still billing.
    """
    ids = [i for i in instance_ids if i]
    if not ids:
        return {}
    out = {}
    try:
        for page in ec2.get_paginator("describe_instances").paginate(InstanceIds=ids):
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    out[inst["InstanceId"]] = inst
    except ClientError:
        for iid in ids:
            inst = describe_one(iid)
            if inst:
                out[iid] = inst
    return out


def private_ip(instance: dict):
    ip = instance.get("PrivateIpAddress")
    if ip:
        return ip
    for nic in instance.get("NetworkInterfaces", []):
        if nic.get("PrivateIpAddress"):
            return nic["PrivateIpAddress"]
    return None


def public_host(instance: dict):
    return instance.get("PublicDnsName") or instance.get("PublicIpAddress") or None


# ---------------------------------------------------------------- readiness


def ssh_ready(host: str, timeout: float = 2.5) -> bool:
    """True once sshd is actually answering.

    EC2's own status checks go green a minute or two before sshd accepts
    connections, which is why we don't use them: a student handed an ssh
    command that fails is worse than one who waited another 30 seconds.
    """
    if not host:
        return False
    try:
        with socket.create_connection((host, 22), timeout) as s:
            s.settimeout(timeout)
            return s.recv(4).startswith(b"SSH-")
    except OSError:
        return False


# ---------------------------------------------------------------- lifecycle


def _group_instances(group_id, states):
    """Every instance tagged for one group, whatever state it is in.

    Only used to find leftovers: a session records its own nodes in the
    database at launch, so nothing has to ask EC2 what a group has.
    """
    return [i for i in course_instances(states=states) if instance_tag(i, "Group") == group_id]


def _launch(group_id, session_id, user_data, sg_ids, tags, count):
    """Launch `count` instances, all in one subnet. Returns [{instance_id, private_ip}].

    MinCount == MaxCount == count on purpose: for a distributed-training
    assignment half a cluster is not a partial success, it is an instance that
    bills while nobody can use it. EC2 either places the whole set or fails,
    and we move to the next subnet.

    One call is also what guarantees the nodes land in the same subnet, and
    therefore the same AZ: cross-AZ traffic is charged per gigabyte and an
    all-reduce moves a lot of gigabytes.
    """
    ami, root_device = image()
    subnets = subnet_ids()
    last_error = None
    for attempt, sn in enumerate(subnets):
        try:
            resp = ec2.run_instances(
                ImageId=ami,
                InstanceType=config.INSTANCE_TYPE,
                MinCount=count,
                MaxCount=count,
                SubnetId=sn,
                # Makes the launch idempotent, so a request that times out on
                # our side and gets retried by botocore cannot leave us paying
                # for two clusters. Per attempt, because a genuine retry in a
                # different subnet is a different request and reusing the token
                # would be an IdempotentParameterMismatch. The session id is
                # new for every start, so tokens never collide across sessions.
                ClientToken=f"{session_id}-{attempt}",
                SecurityGroupIds=sg_ids,
                UserData=user_data,
                # Instances are ephemeral -- a session's nodes are destroyed
                # when it ends and nothing on them outlives it -- so an
                # OS-level shutdown terminates rather than stopping. A `stop`
                # here would leave a root volume billing until the reaper's
                # idle sweep noticed it, which is the cost this design exists
                # to avoid.
                InstanceInitiatedShutdownBehavior="terminate",
                # InstanceMetadataTags is how a node with no AWS credentials
                # learns its rank and its peers' addresses: the control plane
                # writes them as tags, IMDS hands them back over link-local.
                MetadataOptions={
                    "HttpTokens": "required",
                    "HttpPutResponseHopLimit": 2,
                    "InstanceMetadataTags": "enabled",
                },
                BlockDeviceMappings=[
                    {
                        "DeviceName": root_device,
                        "Ebs": {
                            "VolumeSize": config.ROOT_VOLUME_GB,
                            "VolumeType": "gp3",
                            "Encrypted": True,
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                TagSpecifications=[tag_spec("instance", tags), tag_spec("volume", tags)],
            )
            placed = [
                {"instance_id": i["InstanceId"], "private_ip": private_ip(i), "subnet": sn}
                for i in resp["Instances"]
            ]
            log.info(
                "launched %s for group %s in %s",
                [p["instance_id"] for p in placed], group_id, sn,
            )
            return placed
        except ClientError as e:
            last_error = e
            if "InsufficientInstanceCapacity" not in e.response["Error"]["Code"]:
                raise
    raise RetryLater(
        f"AWS has no capacity for {count} x {config.INSTANCE_TYPE} in one "
        f"availability zone right now."
    ) from last_error


def launch_cluster(group_id, assignment_id, session_id, public_key, expires, nodes=1) -> list:
    """Bring up a fresh cluster for the group. Returns [{rank, instance_id, private_ip}].

    Every session gets new instances. Nothing is ever resumed, because nothing
    survives to resume: `api.stop` and every reaper path terminate, so by the
    time a group starts again there is nothing of theirs left -- and no root
    volume of theirs still billing. A session is a clean box from the AMI,
    every time.

    Every node gets the same user-data, and therefore the same session key --
    one key opens the whole cluster, which is also what lets `ssh -A` from one
    node reach the next.
    """
    common = {
        "Group": group_id,
        "Assignment": assignment_id,
        "SessionId": session_id,
        "LeaseExpiresAt": str(expires),
        "NodeCount": str(nodes),
        # Applied at launch to instances *and* volumes. The per-node create_tags
        # below overrides it on the instances with a "-n<rank>" suffix; volumes
        # keep this one, because otherwise they show up unnamed in the console
        # and there are now several per group.
        "Name": f"{config.COURSE}-{group_id}",
    }
    user_data = userdata.render(public_key)
    sg_ids = [security_group_id(), cluster_security_group_id(group_id)]

    # db.claim refuses to reach this point while the session is LIVE, so
    # anything of this group's that still exists is a leftover from an attempt
    # that died -- and it is garbage whatever state EC2 has it in: a running one
    # is an instance no session will ever stop, a stopped one is a root volume
    # nobody will ever read again. Destroy them rather than launching alongside
    # and billing for both.
    #
    # No RetryLater and no waiting: terminating is asynchronous and nothing in
    # the launch below depends on the old instances being gone. That is the one
    # thing the resume path could not do -- user-data is only rewritable on a
    # fully stopped instance, so it had to bounce the student and hope.
    strays = _group_instances(group_id, states=STATES_ALL)
    if strays:
        terminate_instances(
            [i["InstanceId"] for i in strays],
            f"leftovers from group {group_id}'s previous attempt",
        )

    placed = _launch(group_id, session_id, user_data, sg_ids, common, nodes)

    missing_ip = [p["instance_id"] for p in placed if not p["private_ip"]]
    if missing_ip:
        # Without every private address the peer list is wrong for everyone, so
        # this fails the launch rather than booting a cluster that cannot find
        # itself. api.start refunds the start.
        raise RuntimeError(f"no private IP for {missing_ip}")

    peers = ",".join(p["private_ip"] for p in placed)

    # Tag immediately: the peer list reaches the instance through IMDS, so it
    # has to be on the instance before that instance looks for it. The nodes
    # are already booting by now -- every node is freshly launched, there is no
    # resume path where the tags are already in place -- which is why the boot
    # script retries the lookup for a while and treats a miss as non-fatal.
    for rank, node in enumerate(placed):
        node["rank"] = rank
        ec2.create_tags(
            Resources=[node["instance_id"]],
            Tags=course_tags({
                **common,
                "NodeIndex": str(rank),
                "NodePeers": peers,
                "Name": f"{config.COURSE}-{group_id}-n{rank}",
            }),
        )

    return [
        {"rank": n["rank"], "instance_id": n["instance_id"], "private_ip": n["private_ip"],
         "host": None}
        for n in placed
    ]


def terminate_instances(instance_ids, reason=""):
    """Destroy instances and their root volumes. This is how every session ends.

    `api.stop`, every reaper case, the deadline sweep, `admin.py kill` and
    `admin.py terminate` all land here. There is deliberately no stop path: a
    stopped instance is a root volume billing around the clock for a session
    nobody can ever return to, since the next start launches fresh nodes.

    Logged loudly on purpose. It destroys whatever is on the box, and while
    students are told that by the CLI, the motd and the README, it should still
    be impossible to find a deleted volume without finding the line that
    deleted it.
    """
    if not instance_ids:
        return
    log.warning("terminating %s (%s) - root volumes go with them", instance_ids, reason)
    try:
        ec2.terminate_instances(InstanceIds=list(instance_ids))
    except ClientError as e:
        if "NotFound" not in e.response["Error"]["Code"]:
            log.error("terminate failed for %s: %s", instance_ids, e)


# ---------------------------------------------------------------- preflight


def preflight():
    """Prove the host's credentials can do everything the API will need to."""
    ok = True
    checks = [
        ("caller identity", lambda: boto3.client("sts", region_name=config.REGION).get_caller_identity()["Arn"]),
        ("VPC", vpc_id),
        ("subnets", lambda: ",".join(subnet_ids())),
        ("AMI", lambda: " ".join(image())),
        # This exercises create + authorize + revoke, which is the same
        # permission set the per-group cluster groups need.
        ("security group", security_group_id),
        ("nodes per group", lambda: f"{config.NODES_PER_GROUP} x {config.INSTANCE_TYPE}"),
        ("describe instances", lambda: f"{len(course_instances())} tagged Course={config.COURSE}"),
    ]
    for label, fn in checks:
        try:
            print(f"  ok    {label:<20} {fn()}")
        except Exception as e:  # noqa: BLE001 - this is the report
            ok = False
            print(f"  FAIL  {label:<20} {e}")
    return ok


if __name__ == "__main__":  # python -m gpulease.aws
    import sys

    print(f"gpulease preflight (course={config.COURSE}, region={config.REGION})")
    sys.exit(0 if preflight() else 1)
