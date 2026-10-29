"""The dev-only front ends in safety.config.EXTRA_WEB_DIRS.

Mounted only when EXTRA_FRONTENDS is on (safety/api/main.py), so a missing
directory would only surface as a startup crash on dev. Each is a complete copy
of web/ with its own service-worker cache, so the three never sweep each
other's caches on the shared origin.
"""

from __future__ import annotations

import re

import pytest

from safety.config import EXTRA_WEB_DIRS, WEB_DIR


def _cache_settings(directory):
    sw = (directory / "sw.js").read_text()
    name = re.search(r'const CACHE_NAME = "([^"]+)"', sw).group(1)
    prefix = re.search(r'const CACHE_PREFIX = "([^"]+)"', sw).group(1)
    return name, prefix


@pytest.mark.parametrize("name", sorted(EXTRA_WEB_DIRS))
def test_extra_frontend_is_a_full_copy(name):
    directory = EXTRA_WEB_DIRS[name]
    for asset in ("index.html", "app.js", "styles.css", "sw.js", "sw-register.js"):
        assert (directory / asset).is_file(), (name, asset)


def test_no_frontend_sweeps_another_ones_cache():
    caches = {"main": _cache_settings(WEB_DIR)} | {
        name: _cache_settings(d) for name, d in EXTRA_WEB_DIRS.items()
    }
    for owner, (_, prefix) in caches.items():
        for other, (cache_name, _) in caches.items():
            if other != owner:
                assert not cache_name.startswith(prefix), (owner, other)
