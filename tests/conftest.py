"""Shared test fixtures.

Hermeticity (revision-audit finding T6): the suite must pass regardless of
the operator's ambient environment. The README documents setting
SIEVE_YTDLP_PO_TOKEN and cookie/profile vars for real YouTube work; without
this fixture those leak into tests that assert exact ``_runtime_args()``
equality and turn the suite red on the operator's own machine.
"""

import os

import pytest

# Operator-facing vars documented outside the SIEVE_ namespace that still
# influence Sieve behavior (docs/configuration.md, command_router.py).
_EXTERNAL_SIEVE_VARS = (
    "IG_TRANSCRIPTS_DIR",
    "FREELMAPI_API_KEY",
)


@pytest.fixture(autouse=True)
def _clear_sieve_env(monkeypatch):
    """Scrub every ambient SIEVE_* variable so tests see a clean environment.

    Operators legitimately run with SIEVE_* configured (README documents PO
    tokens, cookie files, proxies); none of the tests may depend on that
    ambient state — exact-equality assertions in test_youtube.py break on
    it (revision-audit finding T6).
    """
    for var in [k for k in os.environ if k.startswith("SIEVE_") or k in _EXTERNAL_SIEVE_VARS]:
        monkeypatch.delenv(var, raising=False)
