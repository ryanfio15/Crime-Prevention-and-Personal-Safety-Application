# Running the site on FABRIC

The simplest path is one Python script, run from the FABRIC JupyterHub. It creates the slice,
installs k3s, joins the nodes and runs `deploy/k8s/deploy.sh fabric`. The cluster is described in
[`docs/KUBERNETES.md`](../../docs/KUBERNETES.md).

## What you get

Five Ubuntu VMs at one FABRIC site on a private L2 network (10.20.0.0/24):

| VM | Cores / RAM / disk | Role |
|---|---|---|
| `cp1` | 4 / 8 GB / 60 GB | k3s server, Traefik |
| `data1`, `data2` | 8 / 32 GB / 150 GB | database primary + standby, ETL |
| `app1`, `app2` | 8 / 16 GB / 60 GB | API pods, routing |

That is 36 cores, 104 GB RAM and 520 GB disk. If your project's allocation is smaller,
`--profile small` builds three VMs (cp1, data1, app1: 20 cores, 56 GB) with one database instance.

## Before you start (once)

1. **Make the container image public.** The first push of the `horizontal-scaling` branch runs
   `.github/workflows/image.yml`, which publishes
   `ghcr.io/ryanfio15/crime-prevention-and-personal-safety-application`. On GitHub go to your
   profile, then **Packages**, then that package, **Package settings**, **Change visibility**, and
   choose **Public**. The slice pulls it anonymously.
2. **Log in to the FABRIC JupyterHub**, <https://jupyter.fabric-testbed.net>. If you have never
   used it, run the `Configure Environment` notebook under `jupyter-examples` once: it sets your
   project, bastion key and slice key.
3. Open a **Terminal** in JupyterHub and get the script:
   ```bash
   git clone --branch horizontal-scaling https://github.com/ryanfio15/Crime-Prevention-and-Personal-Safety-Application.git
   cd Crime-Prevention-and-Personal-Safety-Application/deploy/fabric
   ```

## Choosing a site

The pods need **IPv4** internet access to reach the city portals and census.gov. Some FABRIC sites
give VMs only an IPv6 management network. FABRIC's NAT64 lets the VM itself reach IPv4 hosts, but
not the pods inside the cluster.

So pick a site whose management network is IPv4. FABRIC staff have named **MAX** and **TACC**;
check current availability with `fablib.list_sites()` in a notebook. The script tests IPv4 egress
right after the VMs boot. If it fails, the script stops before installing anything; delete the slice
and try another site.

## Go

```bash
python create_slice.py up --site MAX
```

What happens (about 30–60 minutes until the site serves; the data load continues after that):
1. The slice is created and the VMs boot (5–15 min).
2. IPv4 egress is checked, and each VM gets its 10.20.0.x address.
3. k3s v1.36 is installed: a server on cp1 and agents on the other nodes, labelled and tainted by
   role.
4. On cp1, the repo is cloned and `deploy/k8s/deploy.sh fabric` runs:
   1. the database operator and the PostgreSQL cluster start;
   2. migrations run;
   3. the API and routing pods roll out (each routing pod downloads about 1.2 GB);
   4. the **bootstrap** Job starts.
5. The bootstrap enables all six cities and loads **12 months** of incidents plus census data. This
   takes one to a few hours, mostly spent waiting on the city portals. Each city appears on the map
   as soon as its own load finishes.

The script ends by printing the tunnel command.

## Open the site (SSH tunnel)

```bash
python create_slice.py tunnel
```

prints something like

```bash
ssh -i <slice key> -J <you>@bastion.fabric-testbed.net ubuntu@<cp1 address> -L 8080:127.0.0.1:80
```

Run it in a JupyterHub terminal and keep it open. To browse from **your own machine**, run the same
command there instead:
1. Download your bastion key and the slice key from JupyterHub's `fabric_config/` folder.
2. Point the command at the downloaded keys (`-i` for the slice key, and
   `-o ProxyCommand="ssh -i <bastion key> -W %h:%p <you>@bastion.fabric-testbed.net"` in place of
   `-J`).

Then open <http://localhost:8080>.

## Watching it

```bash
python create_slice.py status        # nodes, pods, autoscalers, database, bootstrap log tail
```

or SSH to cp1 (the tunnel command without `-L`) and use kubectl directly:

```bash
kubectl -n safety get pods -o wide
kubectl -n safety logs -f job/safety-bootstrap
kubectl -n safety exec deploy/safety-api -- python -m safety.etl.run status
```

The bootstrap is done when its log ends `bootstrap complete` and
`kubectl -n safety get job safety-bootstrap` shows `1/1`. From then on the CronJobs keep the data
current: new incidents every 6 hours, the time-of-day layers weekly, and backups nightly.

## Load test

On cp1:

```bash
cd ~/safety && deploy/k8s/loadtest/run.sh 3000
```

In a second shell, `kubectl -n safety get hpa -w` shows the API scaling out.

## New code

Push to `horizontal-scaling`, wait for the `image` workflow to finish, then:

```bash
python create_slice.py deploy --image ghcr.io/ryanfio15/crime-prevention-and-personal-safety-application:<full commit sha>
```

## Keeping and removing the slice

FABRIC slices expire, by default a day after creation. Extend the lease with:

```bash
python create_slice.py renew --days 14
```

The maximum depends on your project. When you are done:

```bash
python create_slice.py delete
```

This deletes the VMs and everything on them: the database, bronze and backups. To keep a copy,
first copy the newest dump from the `db-backups` volume off the cluster (docs/KUBERNETES.md,
Runbook).
