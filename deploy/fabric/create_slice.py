"""Build a k3s cluster on a FABRIC slice and deploy the site onto it.

Run from a terminal in the FABRIC JupyterHub (https://jupyter.fabric-testbed.net),
where FABlib is installed and configured with your project, bastion and slice
keys already. See deploy/fabric/README.md for the walkthrough.

    python create_slice.py up --site MAX            # create + install + deploy
    python create_slice.py status                   # nodes, pods, bootstrap progress
    python create_slice.py tunnel                   # print the SSH tunnel command
    python create_slice.py deploy --image <ref>     # roll out another image
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

All five VMs sit at one site on one L2 bridge (10.20.0.0/24). Kubernetes
traffic -- the API server, flannel's VXLAN, the database's replication -- uses
that dataplane network, never the management network, which FABRIC reserves
for SSH.

One requirement on the site: its management network must give the VMs IPv4
egress. The ETL pulls from city portals and census.gov over IPv4, from pods
on an IPv4-only cluster network, and FABRIC's NAT64 (which serves IPv6-only
sites) helps processes on the VM, not pods behind it. `up` checks this right
after the VMs boot and stops, saying so, before installing anything if it is
missing; recreate the slice at another site.
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
}
OVERLAY = {"standard": "fabric", "small": "fabric-small", "minimal": "fabric-minimal"}


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
            "granted the VM.NoLimit tag. Either\n"
            "  * ask the project owner (your PI or instructor) to request VM.NoLimit for the "
            "project in the FABRIC portal, then rerun this command; or\n"
            "  * run now within the caps: python create_slice.py up --profile minimal "
            f"--site {args.site}\n"
            "    (the full pipeline, but Philadelphia over one month and no routing; "
            "README, 'Without the VM.NoLimit tag')."
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


def check_ipv4_egress(slice_, profile) -> None:
    """Stop early at a site whose VMs cannot reach the IPv4 internet."""
    for name in profile:
        node = slice_.get_node(name)
        mgmt = ipaddress.ip_address(node.get_management_ip())
        out = run(
            node,
            "curl -4 -s -o /dev/null -w '%{http_code}' --max-time 15 https://api.github.com || true",
            check=False,
        )
        if not out.strip().startswith(("2", "3", "4")):
            sys.exit(
                f"{name} (management address {mgmt}, IPv{mgmt.version}) has no IPv4 internet "
                "access. The ETL's pods need it to reach the city portals. Delete this slice "
                "(`python create_slice.py delete`) and create it at another site with --site; "
                "sites with IPv4 management networks work."
            )
    log("IPv4 egress ok on every node")


# --------------------------------------------------------------------------- install


def install_k3s(slice_, profile, ips: dict[str, str]) -> None:
    names = list(profile)
    server = slice_.get_node(names[0])
    server_ip = ips[names[0]]

    for name in names:
        node = slice_.get_node(name)
        run(
            node,
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl jq git >/dev/null",
        )

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
        "--node-label safety.io/tier=control",
    )
    run(server, f"until sudo k3s kubectl get node {names[0]} >/dev/null 2>&1; do sleep 3; done")

    for name in names[1:]:
        node = slice_.get_node(name)
        role = profile[name][3]
        extra = f"--node-label safety.io/tier={role}"
        if role == "data":
            extra += " --node-taint dedicated=data:NoSchedule"
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


def checkout(server, branch: str) -> None:
    run(
        server,
        f"if [ -d {REMOTE_REPO}/.git ]; then "
        f"  git -C {REMOTE_REPO} fetch --quiet origin {branch} && "
        f"  git -C {REMOTE_REPO} checkout --quiet -B {branch} FETCH_HEAD; "
        f"else git clone --quiet --branch {branch} {REPO_URL} {REMOTE_REPO}; fi",
    )


def deploy(slice_, args) -> None:
    profile = PROFILES[args.profile]
    server = slice_.get_node(next(iter(profile)))
    checkout(server, args.branch)
    log(f"deploying {args.image} with overlay {OVERLAY[args.profile]} (this takes a while: "
        "the database initialises and each routing pod downloads ~1.2 GB of tiles)")
    # quiet=False streams deploy.sh's output as it runs.
    run(
        server,
        f"cd {REMOTE_REPO} && KUBECONFIG={KUBECONFIG} "
        f"deploy/k8s/deploy.sh {OVERLAY[args.profile]} {shlex.quote(args.image)}",
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
        "kubectl -n safety get cluster safety-db 2>/dev/null; echo; "
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
        check_ipv4_egress(slice_, profile)
        ips = configure_network(slice_, profile)
        install_k3s(slice_, profile, ips)
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
