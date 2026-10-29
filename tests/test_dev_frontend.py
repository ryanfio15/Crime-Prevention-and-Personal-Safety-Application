"""The /dev/ front end (web-dev/) is frozen.

It is the main site as it was at the ui-dev tag, kept for comparison while web/
moves on, so it must not change when anyone edits a front end. Every file's
hash is pinned here; CI runs this, and deploy/promote.sh promotes nothing CI
has not passed.

An accidental edit is undone with `scripts/pin-frontend.sh dev`, which rebuilds
this exact copy. Pinning something else on purpose means updating these hashes
in the same commit.
"""

from __future__ import annotations

import hashlib

from safety.config import EXTRA_WEB_DIRS

DEV_DIR = EXTRA_WEB_DIRS["dev"]

PINNED = {
    "app.js": "86bbb345b60046c10241d8f9f5344efecd4d78ab124525e9dea692ed8dbf79f3",
    "directions.js": "b3ea804ab18b509730e27307286e096b5bfb01870fb17b648fd369cac302ae83",
    "hours.js": "f5dd1070880783ad9d54fdf8e64414822186bcc02b0369ea0a32f73b049bbaf6",
    "html.js": "f8fe2118f2150a90bafaad4b0f214d97715976ee23f064cf30e429a722805853",
    "index.html": "ca9019fbbeb836a8b6f35a9967148c41b0f1b6b64ce1108f792347780b7dca37",
    "styles.css": "a6b6745a6a4eb458353ae44a598d7ab0f963d1a6b3c3c224092fe5ec624e23a6",
    "sw.js": "97c1f0df98d17e7177da4d9d4186e56f5df6afe85af7133e6e44300e40241ae8",
    "sw-register.js": "e96aacfeb2f0e8e8a79cd5fe8e5c2d884cf329b9b04d487d1f2cd1fdff19c2c2",
    "windows.js": "befe045c7b00d2e229922bf6d8b49ba9600fccebf43f36378dd6c26d59ce6c43",
}

RESTORE = "web-dev/ is frozen; restore it with `scripts/pin-frontend.sh dev`"


def test_dev_frontend_has_exactly_the_pinned_files():
    present = {p.relative_to(DEV_DIR).as_posix() for p in DEV_DIR.rglob("*") if p.is_file()}
    assert present == set(PINNED), RESTORE


def test_dev_frontend_files_are_unchanged():
    changed = [
        name
        for name, digest in PINNED.items()
        if hashlib.sha256((DEV_DIR / name).read_bytes()).hexdigest() != digest
    ]
    assert not changed, f"{RESTORE} (changed: {', '.join(changed)})"
