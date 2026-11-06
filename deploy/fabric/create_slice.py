"""Build a k3s cluster on a FABRIC slice and deploy the site onto it.

Run from a terminal in the FABRIC JupyterHub (https://jupyter.fabric-testbed.net),
where FABlib is installed and configured with your project, bastion and slice
keys already. See deploy/fabric/README.md for the walkthrough.

    python create_slice.py up --site MAX            # create + install + deploy
    python create_slice.py status                   # nodes, pods, bootstrap progress
    python create_slice.py tunnel                   # print the SSH tunnel command
    python create_slice.py deploy                   # roll out the branch's latest commit
    python create_slice.py deploy --rerun-bootstrap # ... and restart the first load on it
    python create_slice.py renew --days 14          # extend the slice's lease
    python create_slice.py delete                   # tear it all down

The slice (profile `standard`, docs/KUBERNETES.md "Node layout"):

    cp1     4 cores /  8 GB / 60 GB   k3s server: control plane, Traefik, operators
    data1   8 cores / 32 GB / 150 GB  safety.io/tier=data, taint dedicated=data:NoSchedule
    data2   8 cores / 32 GB / 150 GB    -> database primary / standby, ETL jobs, backups
    app1    8 cores / 16 GB / 60 GB   safety.io/tier=app
    app2    8 cores / 16 GB / 60 GB     -> API replicas, Valhalla routing

`--profile small` drops data2 and app2 (one database instance, no standby) for
a project with a smaller allocation; it deploys the `fabric-small` overlay.

Both need the project to hold FABRIC's VM.NoLimit tag (or VM.NoLimitCPU,
-RAM and -Disk): without it a VM is capped at 2 cores, 10 GB RAM and 10 GB of
disk. `--profile minimal` fits those caps -- cp1, data1, app1, app2 at
2 cores / 8 GB / 10 GB -- and deploys the `fabric-minimal` overlay: the whole
pipeline, but Philadelphia over one month and no routing pods, since the disk
holds little more than the OS and the images.

`--profile ram-capped` is for a project with VM.NoLimitCPU and VM.NoLimitDisk
but not VM.NoLimitRAM: cores and disk as needed, 10 GB of RAM per VM. The
standard layout's shape -- one database, a standby, failover, backups -- on
six VMs (the `fabric-ram-capped` overlay):

    cp1         4 cores /  8 GB /  40 GB   control plane, Traefik
    db1, db2    8 cores / 10 GB / 100 GB   database primary + standby
                                           tier=db, taint dedicated=db
    etl1        4 cores / 10 GB /  80 GB   ETL jobs, bronze, backups
                                           tier=data, taint dedicated=data
    app1, app2  8 cores / 10 GB /  60 GB   API pods, Valhalla; tier=app

`--profile sharded` also fits the caps and keeps everything: all six cities
with a year of data, and routing. No 10 GB disk holds the whole database, so
the cities are split across three database VMs, two each, and the API sends
each request to its city's database (safety/shards.py; the `fabric-sharded`
overlay). Eight VMs:

    cp1             control plane, Traefik
    db1, db2, db3   one database each: lax+dc, aus+phl, chi+sea
                    safety.io/tier=db, safety.io/db-shard=N, taint dedicated=db
    etl1            ETL jobs and bronze; tier=data, taint dedicated=data
    app1, app2      one API pod each; tier=app
    route1          Valhalla; tier=route, taint dedicated=route

All five VMs sit at one site on one L2 bridge (10.20.0.0/24). Kubernetes
traffic -- the API server, flannel's VXLAN, the database's replication -- uses
that dataplane network, never the management network, which FABRIC reserves
for SSH.

Any site works, IPv4 or IPv6-only. The pods are on an IPv4-only cluster
network and the ETL pulls from city portals, census.gov and GitHub, most of
them IPv4-only. At a site whose management network gives the VMs IPv4, pods
reach them directly. At an IPv6-only site the VMs themselves still reach
IPv4-only hosts by name, through FABRIC's NAT64 and DNS64, but pods behind them
cannot: so `up` installs a small forward proxy (tinyproxy) on cp1, listening
on the private bridge, and deploy.sh points every pod's HTTP(S) at it
(EGRESS_PROXY; the `safety-egress` ConfigMap). The proxy resolves names with
the VM's DNS and connects over IPv6, and NAT64 does the rest. `up` checks
which case it is right after the VMs boot, and stops before installing
anything only if neither works.
"""

from __future__ import annotations

import argparse
import ipaddress
import secrets
import shlex
import sys
from datetime import datetime, timedelta, timezone

REPO_URL = "https://github.com/ryanfio15/Crime-Prevention-and-Personal-Safety-Application.git"
DEFAULT_BRANCH = "horizontal-scaling"
DEFAULT_IMAGE = "ghcr.io/ryanfio15/crime-prevention-and-personal-safety-application:horizontal-scaling"
# Inside CloudNativePG 1.30's supported range (1.34-1.36).
K3S_VERSION = "v1.36.5+k3s1"
NETWORK = "k8s-net"
SUBNET = ipaddress.IPv4Network("10.20.0.0/24")
REMOTE_REPO = "/home/ubuntu/safety"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"

# name: (cores, ram GB, disk GB, role)
PROFILES = {
    "standard": {
        "cp1": (4, 8, 60, "control"),
        "data1": (8, 32, 150, "data"),
        "data2": (8, 32, 150, "data"),
        "app1": (8, 16, 60, "app"),
        "app2": (8, 16, 60, "app"),
    },
    "small": {
        "cp1": (4, 8, 60, "control"),
        "data1": (8, 32, 150, "data"),
        "app1": (8, 16, 60, "app"),
    },
    # FABRIC's caps for a project without the VM.NoLimit tag: 2 cores,
    # 10 GB RAM, 10 GB disk per VM.
    "minimal": {
        "cp1": (2, 8, 10, "control"),
        "data1": (2, 8, 10, "data"),
        "app1": (2, 8, 10, "app"),
        "app2": (2, 8, 10, "app"),
    },
    # VM.NoLimitCPU and VM.NoLimitDisk but not VM.NoLimitRAM: only memory
    # stops at 10 GB, so the database and the ETL each get their own VMs.
    "ram-capped": {
        "cp1": (4, 8, 40, "control"),
        "db1": (8, 10, 100, "db"),
        "db2": (8, 10, 100, "db"),
        "etl1": (4, 10, 80, "data"),
        "app1": (8, 10, 60, "app"),
        "app2": (8, 10, 60, "app"),
    },
    # Also inside the caps (RAM is capped at "more than 10 GB", so 10 is
    # allowed; the databases and the ETL get it).
    "sharded": {
        "cp1": (2, 8, 10, "control"),
        "db1": (2, 10, 10, "db"),
        "db2": (2, 10, 10, "db"),
        "db3": (2, 10, 10, "db"),
        "etl1": (2, 10, 10, "data"),
        "app1": (2, 8, 10, "app"),
        "app2": (2, 8, 10, "app"),
        "route1": (2, 8, 10, "route"),
    },
}
OVERLAY = {
    "standard": "fabric",
    "small": "fabric-small",
    "minimal": "fabric-minimal",
    "sharded": "fabric-sharded",
    "ram-capped": "fabric-ram-capped",
}
# Profiles whose VMs have 10 GB disks: the OS is trimmed before k3s goes on,
# and the kubelet is told to evict only when a disk is nearly full -- its
# default (10% free, image clean-up from 85% used) would start clearing a
# database node with gigabytes still free.
CAPPED = {"minimal", "sharded"}
CAPPED_KUBELET_ARGS = " ".join(
    shlex.quote(arg)
    for arg in (
        "--kubelet-arg=eviction-hard=memory.available<200Mi,nodefs.available<4%,"
        "imagefs.available<4%",
        "--kubelet-arg=image-gc-high-threshold=96",
        "--kubelet-arg=image-gc-low-threshold=92",
    )
)


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def fablib_manager():
    try:
        from fabrictestbed_extensions.fablib.fablib import FablibManager
    except ImportError:
        sys.exit(
            "FABlib is not installed here. Run this from the FABRIC JupyterHub, or "
            "`pip install fabrictestbed-extensions` and configure it (fabric_rc)."
        )
    return FablibManager()


def run(node, command: str, check: bool = True, quiet: bool = True) -> str:
    """Run `command` on `node` over SSH; raise if it exits non-zero."""
    wrapped = f"bash -c {shlex.quote('set -o pipefail; ' + command)}; echo __rc=$?"
    stdout, stderr = node.execute(wrapped, quiet=quiet)
    lines = stdout.rstrip().splitlines()
    rc = 0
    if lines and lines[-1].startswith("__rc="):
        rc = int(lines.pop()[5:])
    out = "\n".join(lines)
    if check and rc != 0:
        raise RuntimeError(
            f"{node.get_name()}: `{command[:120]}` exited {rc}\n{out[-2000:]}\n{stderr[-2000:]}"
        )
    return out


def node_ip(index: int) -> str:
    return str(SUBNET.network_address + 10 + index)


# --------------------------------------------------------------------------- create


def choose_image(fablib) -> str:
    names = set(fablib.get_image_names())
    for image in ("default_ubuntu_24", "default_ubuntu_22"):
        if image in names:
            return image
    sys.exit(f"no Ubuntu 22/24 image among {sorted(names)}")


def create(fablib, args) -> None:
    profile = PROFILES[args.profile]
    image = choose_image(fablib)
    log(f"creating slice {args.slice} at {args.site}: {', '.join(profile)} ({image})")
    slice_ = fablib.new_slice(name=args.slice)
    interfaces = []
    for name, (cores, ram, disk, _role) in profile.items():
        node = slice_.add_node(
            name=name, site=args.site, cores=cores, ram=ram, disk=disk, image=image
        )
        interfaces.append(node.add_component(model="NIC_Basic", name="nic1").get_interfaces()[0])
    slice_.add_l2network(name=NETWORK, interfaces=interfaces)
    try:
        slice_.submit(wait=True, progress=True)
    except Exception as exc:
        if "NoLimit" not in str(exc):
            raise
        # FABRIC's policy check refused the request before creating anything.
        sys.exit(
            f"FABRIC refused the slice: {_policy_detail(exc)}\n\n"
            "Your project caps each VM at 2 cores, 10 GB RAM and 10 GB disk until it is "
            "granted the VM.NoLimit tag (or the CPU/RAM/Disk ones separately). With "
            "VM.NoLimitCPU and VM.NoLimitDisk but not RAM, use --profile ram-capped. Else\n"
            "  * ask the project owner (your PI or instructor) to request VM.NoLimit for the "
            "project in the FABRIC portal, then rerun this command; or\n"
            "  * run within the caps: python create_slice.py up --profile sharded "
            f"--site {args.site}\n"
            "    (eight 2-core VMs: all six cities with a year of data and routing, the "
            "cities split across three databases; README, 'Without the VM.NoLimit tag')."
        )
    log("slice is up")


def _policy_detail(exc: Exception) -> str:
    """The orchestrator's own words from its JSON error body, if it gave one."""
    text = str(exc)
    marker = "Policy Violation:"
    if marker in text:
        return text[text.index(marker) + len(marker):].split('"')[0].strip()
    return text[-500:]
    log("slice is up")


def configure_network(slice_, profile) -> dict[str, str]:
    """Give each VM its 10.20.0.x address on the bridge, persistently (netplan)."""
    ips = {}
    for index, name in enumerate(profile):
        node = slice_.get_node(name)
        iface = node.get_interface(network_name=NETWORK)
        mac = iface.get_mac().lower()
        ip = node_ip(index)
        ips[name] = ip
        netplan = (
            "network:\n  version: 2\n  ethernets:\n    k8s0:\n"
            f"      match:\n        macaddress: \"{mac}\"\n"
            f"      addresses: [{ip}/{SUBNET.prefixlen}]\n      mtu: 1500\n"
        )
        run(
            node,
            f"printf %s {shlex.quote(netplan)} | sudo tee /etc/netplan/60-k8s-net.yaml >/dev/null"
            " && sudo chmod 600 /etc/netplan/60-k8s-net.yaml && sudo netplan apply",
        )
        log(f"{name}: {ip} on the {NETWORK} bridge")
    return ips


# IPv4-only hosts the deployment needs from inside: GitHub (the tile builds),
# the Census Bureau (TIGER blocks), a city portal (Socrata). Reached by name.
EGRESS_PROBES = (
    "https://api.github.com",
    "https://www2.census.gov",
    "https://data.cityofchicago.org",
)
PROXY_PORT = 8888


def _reaches(node, url: str, flags: str = "") -> bool:
    out = run(
        node,
        f"curl {flags} -s -o /dev/null -w '%{{http_code}}' --max-time 20 {url} || true",
        check=False,
    )
    return out.strip().startswith(("2", "3", "4"))


def check_egress(slice_, profile) -> bool:
    """How the pods will reach the internet: True through a proxy (NAT64), False directly.

    Direct when every VM has IPv4. Otherwise every VM must reach the IPv4-only
    hosts by name -- FABRIC's NAT64, which image pulls depend on as well --
    and the pods go through cp1's proxy. Neither: stop before installing.
    """
    names = list(profile)
    if all(_reaches(slice_.get_node(n), EGRESS_PROBES[0], "-4") for n in names):
        log("IPv4 egress ok on every node: pods reach the internet directly")
        return False
    for name in names:
        node = slice_.get_node(name)
        failed = [url for url in EGRESS_PROBES if not _reaches(node, url)]
        if failed:
            mgmt = ipaddress.ip_address(node.get_management_ip())
            sys.exit(
                f"{name} (management address {mgmt}, IPv{mgmt.version}) has no IPv4 and "
                f"cannot reach {', '.join(failed)} through FABRIC's NAT64 either. Check that "
                "the VM's DNS settings are FABRIC's defaults (NAT64 needs its DNS64), or "
                "delete the slice (`python create_slice.py delete`) and try another site."
            )
    log("no IPv4 here, but every node reaches IPv4-only hosts through FABRIC's NAT64: "
        "pods will go through a proxy on the control-plane node")
    return True


# The management NIC's MTU is 9000, so the VM advertises an MSS of ~8940 and
# servers send full 1500-byte IPv4 segments. NAT64 grows each one by 20 bytes
# to 1520 bytes of IPv6, which doesn't fit the path, and the Packet Too Big
# never reaches servers that ignore or filter it. Small replies get through and
# big ones vanish: Austin's Imperva-fronted GIS server answers plain HTTP but
# never finishes a TLS handshake (its certificate flight is lost). An MSS of
# 1400 keeps every segment under 1500 bytes after translation.
NAT64_MSS = 1400
MSS_UNIT = "nat64-mss-clamp.service"


def clamp_nat64_mss(node) -> None:
    """Clamp the MSS of outgoing IPv6 SYNs on `node`, now and at every boot."""
    rule = f"-t mangle -%s OUTPUT -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss {NAT64_MSS}"
    unit = (
        "[Unit]\nDescription=Clamp TCP MSS for FABRIC NAT64\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/bin/sh -c '/usr/sbin/ip6tables {rule % 'C'} 2>/dev/null || "
        f"/usr/sbin/ip6tables {rule % 'A'}'\n"
        f"ExecStop=-/usr/sbin/ip6tables {rule % 'D'}\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    run(
        node,
        f"printf %s {shlex.quote(unit)} | sudo tee /etc/systemd/system/{MSS_UNIT} >/dev/null && "
        f"sudo systemctl daemon-reload && sudo systemctl enable --quiet --now {MSS_UNIT}",
    )


def setup_egress_proxy(slice_, profile, ips: dict[str, str]) -> str:
    """tinyproxy on cp1, on the private bridge, for the pods; returns its URL.

    It resolves names itself with the VM's resolver (FABRIC's DNS64) and so
    connects to IPv4-only hosts over IPv6 and NAT64 -- which the pods, on
    an IPv4-only network whose DNS cannot reach FABRIC's IPv6 resolvers,
    could not. Only the bridge and the pod network may use it.
    """
    names = list(profile)
    server = slice_.get_node(names[0])
    ip = ips[names[0]]
    conf = "/etc/tinyproxy/tinyproxy.conf"
    run(
        server,
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tinyproxy >/dev/null && "
        # Drop what is being set, then set it: one value per key.
        f"sudo sed -i -E '/^(Port|Listen|Allow|MaxClients|LogLevel|ConnectPort|Timeout)[[:space:]]/d' "
        f"{conf} && "
        f"printf '%s\\n' 'Port {PROXY_PORT}' 'Listen {ip}' 'Timeout 900' 'MaxClients 200' "
        "'LogLevel Warning' 'Allow 127.0.0.1' 'Allow 10.20.0.0/24' 'Allow 10.42.0.0/16' "
        f"'ConnectPort 443' 'ConnectPort 80' | sudo tee -a {conf} >/dev/null && "
        "sudo systemctl enable --quiet tinyproxy && sudo systemctl restart tinyproxy",
    )
    clamp_nat64_mss(server)
    url = f"http://{ip}:{PROXY_PORT}"
    # From another VM, over the bridge, the way a pod's traffic arrives.
    probe = slice_.get_node(names[-1])
    if not _reaches(probe, EGRESS_PROBES[0], f"-x {url}"):
        sys.exit(f"the egress proxy on {names[0]} ({url}) does not answer from {names[-1]}")
    log(f"egress proxy up on {names[0]}: {url}")
    return url


def egress_proxy_of(server, ip: str) -> str:
    """The proxy URL if cp1 runs one (an IPv6-only site), else empty."""
    active = run(server, "systemctl is-active tinyproxy 2>/dev/null || true", check=False)
    return f"http://{ip}:{PROXY_PORT}" if active.strip() == "active" else ""


# --------------------------------------------------------------------------- install


def install_k3s(slice_, profile, ips: dict[str, str]) -> None:
    names = list(profile)
    server = slice_.get_node(names[0])
    server_ip = ips[names[0]]

    capped = profile_name(profile) in CAPPED
    for name in names:
        node = slice_.get_node(name)
        run(
            node,
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl jq git >/dev/null",
        )
        if capped:
            trim_disk(node)

    token_file = "/var/lib/rancher/k3s/server/token"
    existing = run(server, f"sudo cat {token_file} 2>/dev/null || true", check=False).strip()
    token = existing or secrets.token_hex(32)

    def iface_of(node) -> str:
        return run(node, f"ip -o -4 addr show | awk '$4 ~ /^{ips[node.get_name()]}\\// {{print $2}}'").strip()

    log(f"installing k3s {K3S_VERSION} server on {names[0]}")
    iface = iface_of(server)
    run(
        server,
        f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION={shlex.quote(K3S_VERSION)} "
        f"K3S_TOKEN={token} sh -s - server "
        f"--node-ip {server_ip} --advertise-address {server_ip} --tls-san {server_ip} "
        f"--flannel-iface {iface} --write-kubeconfig-mode 0644 "
        "--node-label safety.io/tier=control"
        + (f" {CAPPED_KUBELET_ARGS}" if capped else ""),
    )
    run(server, f"until sudo k3s kubectl get node {names[0]} >/dev/null 2>&1; do sleep 3; done")

    for name in names[1:]:
        node = slice_.get_node(name)
        role = profile[name][3]
        extra = f"--node-label safety.io/tier={role}"
        if role == "data":
            extra += " --node-taint dedicated=data:NoSchedule"
        elif role == "db":
            extra += " --node-taint dedicated=db:NoSchedule"
            if profile_name(profile) == "sharded":
                # db1 -> shard 1: the fabric-sharded overlay pins each database here.
                shard = "".join(ch for ch in name if ch.isdigit())
                extra += f" --node-label safety.io/db-shard={shard}"
        elif role == "route":
            extra += " --node-taint dedicated=route:NoSchedule"
        if capped:
            extra += f" {CAPPED_KUBELET_ARGS}"
        log(f"joining {name} as an agent ({role})")
        run(
            node,
            f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION={shlex.quote(K3S_VERSION)} "
            f"K3S_URL=https://{server_ip}:6443 K3S_TOKEN={token} sh -s - agent "
            f"--node-ip {ips[name]} --flannel-iface {iface_of(node)} {extra}",
        )

    log("waiting for every node to be Ready")
    run(
        server,
        f"export KUBECONFIG={KUBECONFIG}; "
        f"for i in $(seq 1 60); do "
        f"  n=$(kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready'); "
        f"  [ \"$n\" -ge {len(names)} ] && exit 0; sleep 5; done; "
        "kubectl get nodes; exit 1",
    )
    print(run(server, f"KUBECONFIG={KUBECONFIG} kubectl get nodes -L safety.io/tier -o wide"))


def profile_name(profile) -> str:
    return next(name for name, nodes in PROFILES.items() if nodes is profile)


def trim_disk(node) -> None:
    """Free what a 10 GB disk can spare before the images arrive, and say how much is left.

    snapd and its snaps (a few hundred MB on Ubuntu cloud images) are never
    used on these VMs; apt's cache and the journal are capped.
    """
    run(
        node,
        "sudo DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq snapd >/dev/null 2>&1 || true; "
        "sudo apt-get clean; "
        "sudo mkdir -p /etc/systemd/journald.conf.d && "
        "printf '[Journal]\\nSystemMaxUse=100M\\n' | "
        "sudo tee /etc/systemd/journald.conf.d/size.conf >/dev/null && "
        "sudo systemctl restart systemd-journald && sudo journalctl --vacuum-size=100M -q",
    )
    free_gb = int(run(node, "df --output=avail -B1G / | tail -1").strip() or 0)
    log(f"{node.get_name()}: {free_gb} GB free on / after trimming")
    if free_gb < 5:
        log(f"warning: {node.get_name()} has under 5 GB free; a database or routing pod "
            "there may run out of disk (docs/KUBERNETES.md, 'City-sharded databases')")


def checkout(server, branch: str) -> None:
    run(
        server,
        f"if [ -d {REMOTE_REPO}/.git ]; then "
        f"  git -C {REMOTE_REPO} fetch --quiet origin {branch} && "
        f"  git -C {REMOTE_REPO} checkout --quiet -B {branch} FETCH_HEAD; "
        f"else git clone --quiet --branch {branch} {REPO_URL} {REMOTE_REPO}; fi",
    )


def resolve_image(server, image: str) -> str:
    """The default image pinned to the commit just checked out, once CI has published it.

    The branch tag moves, but a node that pulled it once keeps that copy
    (imagePullPolicy IfNotPresent), so deploying `:horizontal-scaling` again
    would quietly run the old code. The image workflow also tags every build
    with its commit sha; that tag never moves, and deploy.sh's smoke test then
    checks the running commit too. Until the workflow has published it (a
    minute or two after a push) this falls back to the branch tag and says so.
    """
    if image != DEFAULT_IMAGE:
        return image
    sha = run(server, f"git -C {REMOTE_REPO} rev-parse HEAD").strip()
    name = DEFAULT_IMAGE.rsplit(":", 1)[0]
    repo = name.removeprefix("ghcr.io/")
    accept = ("application/vnd.oci.image.index.v1+json,"
              "application/vnd.docker.distribution.manifest.list.v2+json,"
              "application/vnd.oci.image.manifest.v1+json,"
              "application/vnd.docker.distribution.manifest.v2+json")
    found = run(
        server,
        f"tok=$(curl -fsS --max-time 20 'https://ghcr.io/token?scope=repository:{repo}:pull' "
        "| jq -r .token) && "
        f"curl -fsS --max-time 20 -o /dev/null -H \"Authorization: Bearer $tok\" "
        f"-H 'Accept: {accept}' https://ghcr.io/v2/{repo}/manifests/{sha} && echo published || true",
        check=False,
    )
    if "published" in found:
        return f"{name}:{sha}"
    log(f"warning: no image for commit {sha[:12]} yet (the image workflow publishes it a "
        "minute or two after a push); deploying the branch tag, which nodes that already "
        "have it will not re-pull. Rerun `deploy` once the workflow has finished.")
    return image


def deploy(slice_, args) -> None:
    profile = PROFILES[args.profile]
    server = slice_.get_node(next(iter(profile)))
    checkout(server, args.branch)
    args.image = resolve_image(server, args.image)
    proxy = egress_proxy_of(server, node_ip(0))
    log(f"deploying {args.image} with overlay {OVERLAY[args.profile]} (this takes a while: "
        "the database initialises and each routing pod downloads ~1.2 GB of tiles)")
    # quiet=False streams deploy.sh's output as it runs.
    run(
        server,
        f"cd {REMOTE_REPO} && KUBECONFIG={KUBECONFIG} "
        + (f"EGRESS_PROXY={shlex.quote(proxy)} " if proxy else "")
        + ("RERUN_BOOTSTRAP=1 " if args.rerun_bootstrap else "")
        + f"deploy/k8s/deploy.sh {OVERLAY[args.profile]} {shlex.quote(args.image)}",
        quiet=False,
    )
    log("deployed. The first data load runs on in the background; follow it with "
        "`python create_slice.py status` or the kubectl command below.")
    print_tunnel(slice_, profile)


# --------------------------------------------------------------------------- misc


def profile_of(slice_) -> str:
    """Which profile built this slice: each has its own set of node names."""
    names = {node.get_name() for node in slice_.get_nodes()}
    for name, profile in PROFILES.items():
        if names == set(profile):
            return name
    sys.exit(f"slice nodes {sorted(names)} match no profile; pass --profile")


def print_tunnel(slice_, profile) -> None:
    server = slice_.get_node(next(iter(profile)))
    ssh = server.get_ssh_command()
    print(
        "\nOpen the site through an SSH tunnel to the control-plane node (Traefik "
        "listens on port 80 of every node):\n\n"
        f"  {ssh} -L 8080:127.0.0.1:80\n\n"
        "then browse http://localhost:8080 . From your own machine, use the same command "
        "with the bastion and slice keys from your FABRIC JupyterHub's fabric_config/ "
        "(deploy/fabric/README.md, 'Tunnel from your laptop').\n\n"
        "kubectl on the cluster:\n"
        f"  {ssh}\n  kubectl -n safety get pods -o wide\n"
        "  kubectl -n safety logs -f job/safety-bootstrap\n"
    )


def status(slice_, profile) -> None:
    server = slice_.get_node(next(iter(profile)))
    print(run(
        server,
        f"export KUBECONFIG={KUBECONFIG}; kubectl get nodes -L safety.io/tier; echo; "
        "kubectl -n safety get pods -o wide; echo; kubectl -n safety get hpa; echo; "
        "kubectl -n safety get clusters.postgresql.cnpg.io 2>/dev/null; echo; "
        "kubectl -n safety logs job/safety-bootstrap --tail=15 2>/dev/null || true",
        check=False,
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "create", "install", "deploy", "status",
                                           "tunnel", "renew", "delete"])
    parser.add_argument("--slice", default="safety-k8s", help="slice name (default safety-k8s)")
    parser.add_argument("--site", help="FABRIC site for `up`/`create`, e.g. MAX or TACC")
    parser.add_argument(
        "--profile", choices=sorted(PROFILES),
        help="slice size for `up`/`create` (default standard); other actions read it off "
             "the slice's nodes",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--days", type=int, default=14, help="lease extension for `renew`")
    parser.add_argument(
        "--rerun-bootstrap", action="store_true",
        help="with `deploy`: replace the first-load Job (completed or still running) with a "
             "fresh one on this image, e.g. to retry cities that failed",
    )
    args = parser.parse_args(argv)

    fablib = fablib_manager()

    if args.action in ("up", "create"):
        if not args.site:
            parser.error("--site is required to create a slice (see README: choosing a site)")
        args.profile = args.profile or "standard"
        create(fablib, args)
        if args.action == "create":
            return 0

    slice_ = fablib.get_slice(name=args.slice)
    if args.profile is None:
        args.profile = profile_of(slice_)
    profile = PROFILES[args.profile]

    if args.action in ("up", "install"):
        use_proxy = check_egress(slice_, profile)
        ips = configure_network(slice_, profile)
        install_k3s(slice_, profile, ips)
        if use_proxy:
            setup_egress_proxy(slice_, profile, ips)
        deploy(slice_, args)
    elif args.action == "deploy":
        deploy(slice_, args)
    elif args.action == "status":
        status(slice_, profile)
    elif args.action == "tunnel":
        print_tunnel(slice_, profile)
    elif args.action == "renew":
        end = datetime.now(timezone.utc) + timedelta(days=args.days)
        slice_.renew(end.strftime("%Y-%m-%d %H:%M:%S %z"))
        log(f"slice {args.slice} renewed until {end:%Y-%m-%d %H:%M} UTC")
    elif args.action == "delete":
        slice_.delete()
        log(f"slice {args.slice} deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
