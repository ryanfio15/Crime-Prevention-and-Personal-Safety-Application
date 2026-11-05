"""Download and verify the Valhalla routing tiles into a directory.

    python scripts/fetch_valhalla_tiles.py /data                 # newest release
    python scripts/fetch_valhalla_tiles.py /data --tag valhalla-tiles-20261029

The Kubernetes Valhalla pods run this as their init container
(deploy/k8s/base/valhalla.yaml), from the application image, into the volume
the server then mounts read-only at /data. It is the in-cluster counterpart of
deploy/valhalla/fetch.sh, which does the same for the home server's
docker-compose container, and it applies the same trust rules:

- a release is a candidate only if its tag starts `valhalla-tiles-`, it is
  neither a draft nor a prerelease, it was published by `github-actions[bot]`
  (the tiles workflow, .github/workflows/valhalla-tiles.yml) and all three
  assets download from https://github.com/;
- `latest` (the default) is the newest such release by published_at. The
  workflow keeps only the two newest, so a pinned tag stops resolving about two
  months after it was built -- pin one only to hold a build back on purpose;
- valhalla_tiles.tar and valhalla.json must both be listed in SHA256SUMS and
  match it, or nothing is left in the directory.

If the directory already holds a verified copy of the chosen release (a
persistent volume, or a restarted init container), the download is skipped.

Exit status 0 when the directory holds verified tiles, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import httpx

REPO = os.environ.get(
    "VALHALLA_TILES_REPO", "ryanfio15/Crime-Prevention-and-Personal-Safety-Application"
)
ASSETS = ("valhalla_tiles.tar", "valhalla.json", "SHA256SUMS")
VERIFIED = ("valhalla_tiles.tar", "valhalla.json")
TAG_FILE = ".release-tag"


def log(message: str) -> None:
    print(f"fetch_valhalla_tiles: {message}", flush=True)


def choose_release(client: httpx.Client, tag: str) -> tuple[str, dict[str, str]]:
    response = client.get(
        f"https://api.github.com/repos/{REPO}/releases",
        params={"per_page": 30},
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    best = None
    for release in response.json():
        name = release.get("tag_name") or ""
        if not name.startswith("valhalla-tiles-"):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        if (release.get("author") or {}).get("login") != "github-actions[bot]":
            continue
        if tag != "latest" and name != tag:
            continue
        urls = {
            a.get("name"): a.get("browser_download_url") or ""
            for a in release.get("assets", [])
        }
        if not all(urls.get(n, "").startswith("https://github.com/") for n in ASSETS):
            continue
        if best is None or (release.get("published_at") or "") > (best[0].get("published_at") or ""):
            best = (release, urls)
    if best is None:
        raise SystemExit(f"no usable valhalla-tiles release matching {tag!r} in {REPO}")
    return best[0]["tag_name"], best[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(directory: Path) -> bool:
    sums_path = directory / "SHA256SUMS"
    if not sums_path.is_file():
        return False
    expected: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            expected[parts[1].lstrip("*")] = parts[0].lower()
    for name in VERIFIED:
        path = directory / name
        if name not in expected:
            log(f"SHA256SUMS does not list {name}")
            return False
        if not path.is_file() or sha256(path) != expected[name]:
            log(f"checksum mismatch for {name}")
            return False
    return True


def download(client: httpx.Client, url: str, target: Path) -> None:
    partial = target.with_name(target.name + ".partial")
    for attempt in range(1, 4):
        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        handle.write(chunk)
            partial.replace(target)
            target.chmod(0o644)
            return
        except httpx.HTTPError as exc:
            log(f"download of {target.name} failed (attempt {attempt}/3): {exc}")
            if attempt == 3:
                raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tag", default=os.environ.get("VALHALLA_TILES_TAG", "latest"))
    args = parser.parse_args(argv)

    directory: Path = args.directory
    directory.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": "safety-valhalla-fetch"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(
        headers=headers, follow_redirects=True, timeout=httpx.Timeout(60, read=300)
    ) as client:
        tag, urls = choose_release(client, args.tag)
        tag_file = directory / TAG_FILE
        if tag_file.is_file() and tag_file.read_text().strip() == tag and verify(directory):
            log(f"{tag} already present and verified")
            return 0
        log(f"downloading {tag}")
        for name in ASSETS:
            download(client, urls[name], directory / name)

    if not verify(directory):
        for name in ASSETS:
            (directory / name).unlink(missing_ok=True)
        log(f"{tag} failed verification; removed")
        return 1
    (directory / TAG_FILE).write_text(tag + "\n")
    log(f"{tag} installed and verified in {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
