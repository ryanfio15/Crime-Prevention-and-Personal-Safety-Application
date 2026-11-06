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

### With VM.NoLimitCPU and VM.NoLimitDisk, but not VM.NoLimitRAM

FABRIC grants the three limits separately. If your project has unlimited cores and disk but still
stops at 10 GB of RAM per VM (the project's tags are listed on its page in the FABRIC portal), use:

```bash
python create_slice.py up --profile ram-capped --site MAX
```

| VM | Cores / RAM / disk | Runs |
|---|---|---|
| `cp1` | 4 / 8 GB / 40 GB | k3s server, Traefik (and the egress proxy at an IPv6-only site) |
| `db1`, `db2` | 8 / 10 GB / 100 GB | database primary + standby, nothing else |
| `etl1` | 4 / 10 GB / 80 GB | data loads and updates, bronze, nightly backups |
| `app1`, `app2` | 8 / 10 GB / 60 GB | API pods (3–10) and routing (2–4) |

That is 40 cores, 58 GB RAM and 440 GB disk. It is the standard layout with RAM as the only
constraint:
- one database of all six cities with a year of data;
- a standby on the other database node and automatic failover;
- nightly backups;
- routing on both app nodes.

The one structural change is that the data loads get their own VM. On 10 GB nodes, a 7 GB database
and a census load of up to 6 GB (California's blocks) can't safely share one.

### Without the VM.NoLimit tag

The profiles above need VMs larger than FABRIC allows by default. A project without the
**VM.NoLimit** tag is capped at 2 cores, 10 GB RAM and 10 GB disk per VM. A slice request beyond
that is refused with `Your project is lacking VM.NoLimitRAM or VM.NoLimit tag`, and nothing is
created.

1. **For the full site,** ask your project's owner (for a student account, usually your PI or
   instructor) to request the `VM.NoLimit` tag for the project in the FABRIC portal
   (<https://portal.fabric-testbed.net>, the project's page). FABRIC staff grant it. Once the project
   has it, `up --site …` works as described below.
2. **Everything, within the caps:**
   ```bash
   python create_slice.py up --profile sharded --site MAX
   ```
   All six cities with a year of data, plus routing, on eight VMs of 2 cores each:

   | VM | Cores / RAM / disk | Runs |
   |---|---|---|
   | `cp1` | 2 / 8 GB / 10 GB | k3s server, Traefik |
   | `db1` | 2 / 10 GB / 10 GB | database 1: Los Angeles, Washington DC |
   | `db2` | 2 / 10 GB / 10 GB | database 2: Austin, Philadelphia |
   | `db3` | 2 / 10 GB / 10 GB | database 3: Chicago, Seattle |
   | `etl1` | 2 / 10 GB / 10 GB | the data loads and updates |
   | `app1`, `app2` | 2 / 8 GB / 10 GB | one API pod each |
   | `route1` | 2 / 8 GB / 10 GB | routing (Valhalla) |

   No 10 GB disk holds a year of all six cities, so each database VM holds two, and the API sends
   every request to the right one (docs/KUBERNETES.md, "City-sharded databases"). The first load
   works through the three databases in turn and takes about as long as the standard slice's.

   The differences from the standard slice:
   - **No standby databases.** If a database VM fails, its two cities go dark while the other four
     keep working.
   - **No nightly backup.** `etl1` has no room to keep one. The data can be reloaded from the
     portals at any time.

   The script prints each VM's free disk after trimming the OS, and warns below 5 GB.
3. **A quick pipeline test within the caps:**
   ```bash
   python create_slice.py up --profile minimal --site MAX
   ```
   This builds four VMs at 2 cores / 8 GB / 10 GB each: `cp1`, `data1`, and `app1` and `app2` with
   one API pod each. It runs the whole pipeline: k3s, the database operator, migrations, the
   bootstrap, the CronJobs, the ingress, and two API replicas. A 10 GB disk holds little more than
   the OS and the container images, so this profile cuts two things:
   - the bootstrap loads **Philadelphia over one month**;
   - there are **no routing pods**, so drive, walk and bike directions show as unavailable. The map
     and transit still work.

   It uses the `deploy/k8s/overlays/fabric-minimal` overlay. When the tag arrives, `delete` the
   capped slice and build the standard one.

The other commands (`status`, `tunnel`, `deploy`, `renew`, `delete`) work out the profile from the
slice's nodes, so `--profile` is only needed with `up`.

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

Any site works. The pods need to reach IPv4-only hosts (GitHub, census.gov, most city portals), and
the script handles both kinds of site:

- **Sites whose VMs have IPv4:** pods reach the internet directly.
- **IPv6-only sites (MAX, for one):** the VMs still reach IPv4-only hosts by name through FABRIC's
  NAT64, but the pods behind them cannot. So the script installs a small forward proxy (tinyproxy)
  on `cp1`. It listens only on the slice's private network and carries the pods' web traffic out
  through the VM's NAT64. Database and in-cluster traffic never goes through it.

The script works out which kind of site it is right after the VMs boot. It stops before installing
anything only if neither works, for example when a VM's DNS settings have been changed from FABRIC's
defaults (NAT64 relies on FABRIC's DNS). FABRIC's NAT64 gateway is shared and bandwidth-limited: the
first load is slower through it, but the volumes here are small.

If you already have a slice that stopped at the old IPv4 check, don't recreate it. Continue on the
same VMs:

```bash
git pull
python create_slice.py renew --days 7
python create_slice.py install
```

## Go

```bash
python create_slice.py up --site MAX
```

What happens (about 30–60 minutes until the site serves; the data load continues after that):
1. The slice is created and the VMs boot (5–15 min).
2. Internet access is checked (direct IPv4, or NAT64 through a proxy on `cp1`), and each VM gets
   its 10.20.0.x address.
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

Push to `horizontal-scaling`, wait for the `image` workflow to finish (a minute or two), then:

```bash
git pull
python create_slice.py deploy
```

`deploy` pins the image to the commit it just checked out
(`ghcr.io/ryanfio15/crime-prevention-and-personal-safety-application:<sha>`). Deploying the
moving branch tag again wouldn't work: a node that already pulled it keeps its old copy. If the
workflow hasn't published the commit yet, `deploy` says so and falls back to the branch tag; rerun
it once the workflow is done.

`--rerun-bootstrap` also replaces the first-load Job, whether it completed or is still running,
with a fresh one on the new image. A Job keeps the image and settings it was created with, so this
is how a fix reaches cities that failed to load. Cities that already loaded are skipped.

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
