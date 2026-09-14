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
RUN useradd --system --create-home --home-dir /home/safety safety \
    && chown -R safety:safety /app
USER safety

EXPOSE 8000

# $PORT is injected by the platform; 8000 is the fallback for a bare
# `docker run`. --host :: binds dual-stack, which matters because Railway's
# private network is IPv6-only. --proxy-headers plus a permissive
# --forwarded-allow-ips is what makes X-Forwarded-For trustworthy for
# safety/api/ratelimit.py: the container is only reachable through the
# platform's proxy, never directly.
CMD ["sh", "-c", "uvicorn safety.api.main:app --host :: --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
