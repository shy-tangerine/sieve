"""Consistency Analysis Engine (Scouts 19/20).

WHY this module exists
----------------------
Western anti-bot vendors (scout #19) no longer trust a single signal: they
cross-correlate Canvas <-> WebGL <-> AudioContext <-> fonts <-> timezone <-> CPU
concurrency <-> deviceMemory. A single-signal spoof (fake UA only) is tripped by
statistical inconsistency between otherwise-independent measurements. Scout #20
concluded the fix is *coherent bundles*: every signal must describe the SAME fake
device, so the whole bundle reads like one real machine.

WHAT this module does
---------------------
This module is the *framework* for generating coherent identity bundles, not the
spoof patch itself. It produces a deterministic device descriptor (blueprint +
derived signals); the parent wires it into primp impersonation and Sleeper profile
config, then executes the actual browser-side fingerprinting. Hashing of Canvas /
AudioContext is left to the browser — the module ships coherent *hints* so downstream
impersonation targets agree on the underlying device.

Scout #17 rule 4 (per-task rotation) is handled by ``rotate``: pick a DIFFERENT
blueprint per task so a long crawl doesn't look like one immortal device.

Signals we freeze as a coherent set:
  platform, timezone, languages, screen, hardware_concurrency, device_memory,
  vendor, webgl_renderer. UA prefix is deliberately NOT part of the equality check,
  because real browsers vary the UA across versions while every fingerprint signal
  stays constant.

Pure stdlib only: ``random``, ``json``, ``dataclasses``. No network, no file I/O.
"""

from __future__ import annotations

import random
import secrets
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Coherent base profiles.
#
# Every value inside one blueprint describes the same physical device. These are
# intentionally conservative, high-frequency real-world fingerprints (Scout #20:
# blend in with the dominant population per OS).
# ---------------------------------------------------------------------------
PROFILE_BLUEPRINTS: Dict[str, Dict[str, Any]] = {
    # Apple Silicon-era MacBook Air 13" (M2), Chrome on macOS 14 Sonoma.
    "mac_chrome": {
        "ua_prefix": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "platform": "MacIntel",
        "timezone": "America/Los_Angeles",
        "languages": ["en-US", "en"],
        "screen": "2560x1440",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "vendor": "Google Inc.",
        "renderer": "ANGLE (Apple, Apple M2, OpenGL 4.1 Metal - 84)",
    },
    # Current-gen Windows 11 desktop, Chrome on a 6-core/16GB rig.
    "win_chrome": {
        "ua_prefix": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "platform": "Win32",
        "timezone": "America/New_York",
        "languages": ["en-US", "en"],
        "screen": "1920x1080",
        "hardware_concurrency": 6,
        "device_memory": 16,
        "vendor": "Google Inc.",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    # Mid-range Dell with integrated Intel graphics, Firefox on Linux (Ubuntu 24.04).
    "linux_firefox": {
        "ua_prefix": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0)",
        "platform": "Linux x86_64",
        "timezone": "Europe/Berlin",
        "languages": ["en-GB", "en"],
        "screen": "1366x768",
        "hardware_concurrency": 4,
        "device_memory": 8,
        "vendor": "",
        "renderer": "Mesa DRI Intel(R) HD Graphics 620 (KBL GT2)",
    },
    # Mid-range Android 14 phone, Chrome. deviceMemory is unsupported (~absent)
    # on Android Chrome, signalled here as 8 as a conservative placeholder.
    "android_chrome": {
        "ua_prefix": "Mozilla/5.0 (Linux; Android 14; Pixel 8)",
        "platform": "Linux armv8l",
        "timezone": "Asia/Tokyo",
        "languages": ["ja-JP", "ja", "en"],
        "screen": "1080x2400",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "vendor": "Google Inc.",
        "renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)",
    },
}

# Signals that must agree for two identities to look like the same device.
# UA prefix is intentionally excluded — browsers rotate it across versions.
_COHERENT_SIGNALS: List[str] = [
    "platform",
    "timezone",
    "languages",
    "vendor",
    "renderer",
    "screen",
]

# Extra numeric signals vendors cross-correlate; folded into equality too.
_NUMERIC_SIGNALS: List[str] = ["hardware_concurrency", "device_memory"]


def _copy_blueprint(blueprint: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep-ish copy (nested list defensively) so callers can't mutate
    the shared PROFILE_BLUEPRINTS pool."""
    return {k: list(v) if isinstance(v, list) else v for k, v in blueprint.items()}


def generate_identity(
    blueprint: Optional[str] = None,
    *,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a coherent identity bundle.

    Args:
        blueprint: name of a PROFILE_BLUEPRINTS entry. If None, a random profile
            is chosen.
        seed: optional RNG seed for deterministic output. When given, the choice
            and the ``id`` are both reproducible.

    Returns:
        A dict combining the full blueprint (ua_prefix, platform, timezone,
        languages, screen, hardware_concurrency, device_memory, vendor, renderer)
        plus derived fields: ``profile_name``, ``id`` (random 8-hex), and the
        cross-correlation ``hint`` placeholders.
    """
    names = list(PROFILE_BLUEPRINTS.keys())
    rng = random.Random(seed) if seed is not None else secrets.SystemRandom()

    if blueprint is None:
        blueprint = rng.choice(names)

    if blueprint not in PROFILE_BLUEPRINTS:
        raise KeyError(
            f"unknown blueprint {blueprint!r}; known: {sorted(names)}"
        )

    identity = _copy_blueprint(PROFILE_BLUEPRINTS[blueprint])
    identity["profile_name"] = blueprint
    identity["id"] = (rng.randbytes(4) if seed is not None else secrets.token_bytes(4)).hex()  # 8 hex chars

    # Placeholder hints: actual hashing is browser-side (Canvas / AudioContext pull
    # real device data). We only assert the *dependency* so downstream impersonation
    # keeps every signal pointing at the same GPU/vendor as the blueprint.
    identity["canvas_hash_hint"] = "high-entropy"
    identity["webgl_renderer"] = identity["renderer"]
    identity["audio_context_hint"] = "coherent"
    return identity


def identities_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Cross-correlation equality check (what the anti-bot vendors run).

    Returns True when the coherent fingerprint signals match, meaning the two
    identities describe the same physical device. UA prefix may differ, so it is
    ignored here.
    """
    for key in _COHERENT_SIGNALS + _NUMERIC_SIGNALS:
        if a.get(key) != b.get(key):
            return False
    return True


def fingerprint_summary(identity: Dict[str, Any]) -> str:
    """One-line human summary of an identity's coherent fingerprint."""
    core = " | ".join(
        str(identity.get(k))
        for k in (
            "profile_name",
            "platform",
            "timezone",
            "screen",
            "hardware_concurrency",
            "device_memory",
        )
    )
    renderer = identity.get("renderer", "")
    return f"{core} | {renderer}"


def rotate(identity: Dict[str, Any], *, seed: Optional[int] = None) -> Dict[str, Any]:
    """Pick a DIFFERENT blueprint and regenerate (Scout #17 rule 4).

    Used for per-task profile rotation so a long-running crawl doesn't present as
    one immortal device across many independent fetch jobs.

    Args:
        identity: an existing identity whose profile must be rotated away from.
        seed: optional seed for deterministic rotation.
    """
    current = identity.get("profile_name")
    names = [n for n in PROFILE_BLUEPRINTS if n != current]
    rng = random.Random(seed)
    next_name = rng.choice(names)
    return generate_identity(next_name, seed=seed)
