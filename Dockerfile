# One image, two services (docs/DEPLOY.md).
#
# The API and the ETL run identical code and differ only in start command, so
# they share an image rather than duplicating a build. The API is the default
# CMD; the ETL service overrides it with
# `python -m safety.etl.run incremental --city phl`.
#
# 3.12 rather than the 3.14 used for local development: every compiled
# dependency (h3, pyproj, psycopg-binary, pydantic-core) ships a cp312 manylinux
# wheel for x86_64 and aarch64, so nothing is built from source and no compiler
# toolchain is needed in the image.

FROM python:3.12-slim

# Unbuffered so log lines reach the platform console as they happen rather than
# when the process exits -- an ETL run that dies mid-pull should still have told
# you how far it got.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements before the source copy: the dependency layer is then cached
# across every deploy that does not change requirements.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# safety/config.py resolves MIGRATIONS_DIR, CROSSWALK_DIR and WEB_DIR relative
# to the repo root, which is /app here -- so db/migrations, reference/crosswalk
# and web/ all have to be present in the image, not just the safety package.
# See .dockerignore for what is deliberately left out.
COPY . .

# Least privilege: nothing in the app writes inside /app. The ETL writes to
# BRONZE_ROOT, which points at a mounted volume outside this tree.
#
# The image deliberately does NOT end on `USER safety`. A mounted volume arrives
# root-owned whatever the image says, so something has to chown it before the
# application touches it -- docker-entrypoint.sh does that and then drops to
# this account for the process itself. Adding `USER safety` here would take away
# the privilege needed to make the volume usable in the first place.
RUN useradd --system --create-home --home-dir /home/safety safety \
    && chown -R safety:safety /app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000

# $PORT is injected by the platform; 8000 is the fallback for a bare
# `docker run`.
#
# --host 0.0.0.0, not `::`. A `::` bind looks like it should be dual-stack, but
# Python sets IPV6_V6ONLY on the listening socket, so the process accepts IPv6
# only and every IPv4 connection is dropped before it reaches the application --
# uvicorn logs a clean startup and no request line, and the client sees an empty
# reply. Railway's edge proxy and Docker's port forwarding both arrive over
# IPv4. The IPv6-only private network is used for *outbound* connections to the
# database, which the listen address does not affect.
#
# --proxy-headers plus a permissive --forwarded-allow-ips is what makes
# X-Forwarded-For trustworthy for safety/api/ratelimit.py: the container is only
# reachable through the platform's proxy, never directly.
#
# `exec` so uvicorn replaces the shell and becomes PID 1. Without it the shell
# stays in front, and a platform stop signal is delivered to the shell rather
# than to uvicorn -- no graceful shutdown, just a kill after the grace period.
CMD ["sh", "-c", "exec uvicorn safety.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
