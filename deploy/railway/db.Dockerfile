# The `db` service on Railway: the same PostGIS image as docker-compose.yml and
# the Kubernetes cluster, plus the two things a plain image service cannot be
# given from outside -- server settings, and an init script that creates the
# non-superuser owner role the application expects (`safety_app`, as
# CloudNativePG does in deploy/k8s/base/db-cluster.yaml).
#
# Built by Railway from this repository (deploy/railway/up.sh sets the
# service's Dockerfile path to this file); the build context is the repo root.

FROM postgis/postgis:17-3.5

# A subdirectory of the volume, not its root: a fresh Railway volume is not
# empty (lost+found), and initdb refuses a non-empty data directory.
ENV PGDATA=/var/lib/postgresql/data/pgdata \
    TZ=UTC \
    PGTZ=UTC

# Runs once, on the first start with an empty volume, after the image's own
# 10_postgis.sh has created PostGIS in POSTGRES_DB.
COPY deploy/railway/db-init.sh /docker-entrypoint-initdb.d/20-safety-app.sh

# Sized for a database of about 7 GB (one rolling year of six cities) on a
# service Railway lets grow to several GB of memory and bills by use: the
# Kubernetes `fabric-ram-capped` overlay's settings, a little smaller.
CMD ["postgres", \
     "-c", "shared_buffers=1GB", \
     "-c", "effective_cache_size=3GB", \
     "-c", "work_mem=32MB", \
     "-c", "maintenance_work_mem=512MB", \
     "-c", "max_connections=100", \
     "-c", "max_parallel_workers_per_gather=2", \
     "-c", "max_wal_size=2GB", \
     "-c", "timezone=UTC", \
     "-c", "log_min_duration_statement=5000"]
