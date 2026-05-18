"""v1.6.9: filesystem-anchored ownership tokens for webTool-launched browsers.

Background
----------
Prior to v1.6.9, the ``remote_cdp`` backend (introduced in v1.6.8) accepted
any loopback ``ws://`` URL via ``chromium.connect_over_cdp``. Loopback alone
does **not** prove the browser was launched by webTool -- a user's personal
Chrome started with ``--remote-debugging-port=9222`` lives on loopback too.
Attaching to it would expose real cookies, history, and logged-in accounts,
violating the design rule "webTool only controls browsers it launched."

This module implements a filesystem-anchored ownership proof:

* When ``BrowserManager`` launches an isolated browser, it writes a
  cryptographically random hex token into ``<profile_dir>/.webtool-ownership``.
* When a ``remote_cdp`` caller wants to attach to that browser, they read
  the token off the same file (the launcher's profile_dir) and pass it
  via ``BrowserConfig.remote_cdp_ownership_token``.
* ``BrowserManager.start()`` verifies the token before calling
  ``connect_over_cdp`` -- a non-webTool browser cannot present this proof
  because its profile dir has no such file.

The token is 32 random bytes hex-encoded (64 chars), generated via
``secrets.token_hex(32)``. Verification uses ``secrets.compare_digest``
for constant-time comparison.

Threat model
------------
This defends against the **accidental-attach** scenario where a user has
a personal Chrome on loopback and a misconfigured webTool tries to attach
to it. It does **not** defend against a local attacker who can read the
ownership file -- such an attacker already controls the user's filesystem
and has many easier attacks available. The token is a coordination
mechanism, not a secret.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path


class OwnershipToken:
    """Read/write/verify filesystem-anchored ownership tokens.

    All methods are classmethods so this is a stateless helper rather
    than an instance. The token file always lives at
    ``<profile_dir>/.webtool-ownership``; the filename is fixed so a
    third-party reader (e.g. a sibling process) can find it without
    knowing webTool internals.

    Token format: 64-character lowercase hex string (32 random bytes).
    """

    FILENAME = ".webtool-ownership"
    TOKEN_BYTES = 32  # -> 64 hex chars

    @classmethod
    def issue(cls, profile_dir: Path | str) -> str:
        """Generate a fresh token and write it under ``profile_dir``.

        Creates ``profile_dir`` (and its parents) if missing. On POSIX,
        the token file is chmodded to 0o600 (best-effort -- on Windows
        the chmod call is silently ignored).

        Returns the new token string so the caller can capture it
        in-memory (e.g. ``BrowserManager._issued_token``) without a
        re-read round-trip.
        """
        pdir = Path(profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(cls.TOKEN_BYTES)
        path = pdir / cls.FILENAME
        path.write_text(token, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return token

    @classmethod
    def read(cls, profile_dir: Path | str) -> str | None:
        """Return the token stored under ``profile_dir`` or None.

        Returns None when the file is missing OR the file exists but is
        empty (after stripping whitespace). Used by ``remote_cdp``
        callers to discover the token a webTool launcher wrote into its
        own profile dir.
        """
        path = Path(profile_dir) / cls.FILENAME
        try:
            data = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return data or None

    @classmethod
    def verify(cls, profile_dir: Path | str, candidate: str) -> bool:
        """Constant-time compare ``candidate`` against the token at
        ``profile_dir``. Returns False when no token file exists, the
        candidate is empty, or the strings disagree.
        """
        if not candidate:
            return False
        actual = cls.read(profile_dir)
        if actual is None:
            return False
        return secrets.compare_digest(actual, candidate)
