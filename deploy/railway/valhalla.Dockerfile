# The `valhalla` service on Railway: street routing for directions, with the
# tiles baked into the image at build time.
#
# Kubernetes fetches the tiles (~1.2 GB) into each pod's emptyDir from an init
# container (deploy/k8s/base/valhalla.yaml). Railway has no init containers,
# and a volume would tie the service to one replica for no benefit, so the same
# fetch script runs in a build stage instead and the server image starts
# serving immediately. A new month's tiles = a rebuild: deploy/railway/up.sh
# resolves the newest release and passes its tag in VALHALLA_TILES_TAG, which
# also breaks the build cache exactly when the tiles change.
#
# Build context: the repo root.

FROM python:3.12-slim AS tiles
ARG VALHALLA_TILES_TAG=latest
RUN pip install --no-cache-dir "httpx>=0.27"
COPY scripts/fetch_valhalla_tiles.py /fetch_valhalla_tiles.py
RUN python /fetch_valhalla_tiles.py /data --tag "$VALHALLA_TILES_TAG" \
    && chmod -R a+rX /data

# Pinned by digest, as in docker-compose.yml: the tiles are built by this same
# digest (deploy/valhalla/build.sh). Change them together.
FROM ghcr.io/valhalla/valhalla:3.9.1@sha256:4fd114eb1d26b8fc2f43bcb3df234c1b63a0bf13c277405e5943d5361dc575de
COPY --from=tiles /data /data
# nobody:nogroup, as in docker-compose.yml; it only reads /data and keeps its
# ZeroMQ IPC sockets in /tmp.
USER 65534:65534
EXPOSE 8002
# The config travels with the tiles; "2" is the worker thread count.
CMD ["valhalla_service", "/data/valhalla.json", "2"]
