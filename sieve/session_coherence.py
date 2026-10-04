"""Temporal session coherence — human-timing delays, warm-up navigation plans,
and persistent cookie identity.

Scout 12 (temporal session coherence): Cloudflare / DataDome / PerimeterX all
score *longitudinal* session history, not just a single request. A cold session
that jumps straight to a high-value page is a classic automation signature. This
module therefore *generates* the behavioral plan and timing — it does not itself
perform any navigation (the parent wires the real fetchers):

  * :func:`human_delay` — sample a human-like inter-request pause, with an
    occasional long "reading" pause (~10% of draws land in a 3-8s window).
  * :func:`warmup_sequence` — build the traversal plan home -> category -> target,
    with pauses that grow toward the target so the session looks warmed up.
  * :class:`SessionProfile` — persistent identity (cookie jar, proxy, activity
    timestamp, JSON serialization). Scout 19: identity *stability* beats
    rotation — long-lived bots that keep one identity score better.
  * :class:`SessionStore` — thread-safe in-memory registry of profiles.

Pure standard library (random, time, dataclasses, threading). No I/O, no network.
"""

from __future__ import annotations

import random
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

__all__ = [
    "human_delay",
    "warmup_sequence",
    "SessionProfile",
    "SessionStore",
    "set_rng",
    "current_rng",
]

# 10% chance of a long "reading" pause rather than a plain micro-pause.
_LONG_PAUSE_PROBABILITY = 0.10
_LONG_PAUSE_MIN_S = 3.0
_LONG_PAUSE_MAX_S = 8.0

# Injectable RNG strategy (issue #237): timing choices here are behavioral
# camouflage, NOT security material, so a plain seeded ``random.Random`` is
# a valid injection point for deterministic tests. Unpredictability-sensitive
# identifiers elsewhere keep ``secrets``/``SystemRandom`` by contract.
_RNG: random.Random | None = None
_RNG_LOCK = threading.Lock()
_SCOPED_RNG: ContextVar[random.Random | None] = ContextVar("sieve_behavior_rng", default=None)


def set_rng(rng: random.Random | None) -> None:
    """Set the module RNG (``None`` restores the ambient global instance)."""
    global _RNG
    with _RNG_LOCK:
        _RNG = rng


def current_rng() -> random.Random:
    """Task/client strategy, then legacy helper override, then ambient random."""
    return _SCOPED_RNG.get() or (_RNG if _RNG is not None else random)


@contextmanager
def rng_scope(rng: random.Random | None = None):
    """Nested tasks share one owner's behavior RNG; roots get independent RNGs."""
    token = _SCOPED_RNG.set(rng if rng is not None else _SCOPED_RNG.get() or random.Random())
    try:
        yield current_rng()
    finally:
        _SCOPED_RNG.reset(token)


def human_delay(*, min_ms: int = 400, max_ms: int = 2500, rng: random.Random | None = None) -> float:
    """Sample a human-like delay in seconds between two navigations.

    Draws uniformly within ``[min_ms, max_ms]`` milliseconds, but with a ~10%
    chance of a far longer "reading" pause (3-8s) that sits well outside the
    mechanical range — real humans spend time absorbing a page, bots rarely do.

    Args:
        min_ms: lower bound of the ordinary pause, in milliseconds.
        max_ms: upper bound of the ordinary pause, in milliseconds.
        rng: optional injectable RNG (issue #237) for deterministic tests;
            falls back to the module-wide strategy set via :func:`set_rng`,
            else the ambient ``random`` module.

    Returns:
        Delay in seconds (always > 0). The caller sleeps for this long; this
        module only *computes* the value so the plan can be serialized ahead of
        execution without blocking.
    """
    if max_ms < min_ms:
        # Tolerate a swapped or collapsed range rather than raising mid-plan.
        min_ms, max_ms = max_ms, min_ms
    if min_ms < 0:
        min_ms = 0
    source = rng if rng is not None else current_rng()
    if source.random() < _LONG_PAUSE_PROBABILITY:
        return source.uniform(_LONG_PAUSE_MIN_S, _LONG_PAUSE_MAX_S)
    return source.uniform(min_ms, max_ms) / 1000.0


def _homepage_of(url: str) -> str:
    """Return the scheme+netloc of ``url`` (i.e. the site root)."""
    parts = urlsplit(url)
    scheme = parts.scheme or "https"
    return f"{scheme}://{parts.netloc}"


def _category_path(url: str) -> str:
    """Build a plausible mid-level "category" path derived from the target.

    Prefer real path structure over invented URLs: if the target already has a
    non-trivial path, walk up one directory level (keep every segment, drop the
    last) so the intermediate page is a genuine parent of the target. Fall back
    to a generic ``/browse`` path otherwise.
    """
    parts = urlsplit(url)
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    path_segs = [seg for seg in parts.path.split("/") if seg]
    if len(path_segs) >= 2:
        parent = "/" + "/".join(path_segs[:-1]) + "/"
        return f"{scheme}://{netloc}{parent}"
    return f"{scheme}://{netloc}/browse"


def warmup_sequence(target_url: str, *, steps: int = 3,
                    rng: random.Random | None = None) -> list[dict]:
    """Build a warm-up navigation plan ending at ``target_url``.

    Produces ``steps`` navigation steps of kinds ``home``, then ``category``,
    then ``target``, each carrying a ``pause_ms`` to sleep *before* the next
    request. Pauses grow toward the target so the approach looks organic rather
    than machine-fast. The final step's URL is always exactly ``target_url``.

    A bare ``steps=2`` collapses straight home -> target (minimum plan);
    ``steps>3`` walks further up intermediate ancestor paths before descending.

    Args:
        target_url: the high-value page reachable at the end of the plan.
        steps: number of steps, at least 2 (home + target).
        rng: optional injectable RNG (issue #237) for deterministic tests.

    Returns:
        A list of dicts shaped ``{"url", "pause_ms", "kind"}``.
    """
    target_url = target_url.strip()
    parsed_target = urlsplit(target_url)
    if parsed_target.scheme not in {"http", "https"} or not parsed_target.netloc:
        raise ValueError("target_url must be an absolute HTTP(S) URL")
    if isinstance(steps, bool) or not isinstance(steps, int) or not 2 <= steps <= 10:
        raise ValueError("steps must be an integer between 2 and 10")
    home = _homepage_of(target_url)
    parsed = urlsplit(target_url)
    path_segs = [seg for seg in parsed.path.split("/") if seg]
    intermediates = [
        f"{parsed.scheme}://{parsed.netloc}/" + "/".join(path_segs[:depth]) + "/"
        for depth in range(1, len(path_segs))
    ]

    n_steps = max(2, int(steps))
    # Number of intermediate (category) hops we can lay out before the target.
    max_intermediates = n_steps - 2

    source = rng if rng is not None else current_rng()
    plan: list[dict] = []
    elapsed_index = 0.0
    offsets = sorted(source.random() for _ in range(n_steps))
    # Painlessly increasing pauses: distribute n_steps pauses in ascending order
    # across a roughly (0.6s .. 4s) band, then stretch it modestly with noise.
    for i, frac in enumerate(offsets):
        base_s = 0.6 + (3.4 * (i / max(1, n_steps - 1)))
        pause_s = base_s * source.uniform(0.9, 1.15)
        plan.append({"pause_ms": int(pause_s * 1000), "_frac": frac})
        elapsed_index = i

    seq: list[dict] = []
    step_iter = iter(plan)
    # Home first.
    home_meta = next(step_iter)
    seq.append({"url": home, "pause_ms": home_meta["pause_ms"], "kind": "home"})
    # Intermediate category hops (0..max_intermediates of them).
    selected_intermediates = intermediates[-max_intermediates:] if max_intermediates else []
    for intermediate in selected_intermediates:
        meta = next(step_iter)
        seq.append({"url": intermediate, "pause_ms": meta["pause_ms"], "kind": "category"})
    # Final target hop.
    target_meta = next(step_iter)
    seq.append(
        {"url": target_url, "pause_ms": target_meta["pause_ms"], "kind": "target"}
    )

    # Drop the bookkeeping field before returning.
    for step in seq:
        step.pop("_frac", None)
    return seq


@dataclass
class SessionProfile:
    """Persistent identity for a browsing session (Scout 19: stability wins).

    Holds the cookie jar (the continuity token anti-bot systems inspect most),
    an optional egress proxy, and a last-active timestamp. Instances are
    json-safe via :meth:`to_dict` / :meth:`from_dict`, so the parent can persist
    a profile across runs and keep one identity for the lifetime of a "browser".
    """

    name: str = "default"
    cookie_jar: list[dict] = field(default_factory=list)
    _proxy: Optional[str] = field(default=None, repr=False)
    # time.monotonic (#235): idle tracking is an elapsed-time calculation, so
    # it must not jump with wall-clock changes. Profiles persisted across runs
    # re-baseline on load (from_dict), so the monotonic origin stays coherent.
    _last_active: float = field(default_factory=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cookie_jar, list):
            self.cookie_jar = []
        self.cookie_jar = [
            self._normalize_cookie(c)
            for c in self.cookie_jar
            if isinstance(c, dict) and {"name", "value", "domain"} <= set(c)
        ]
        if self._last_active is None:
            self._last_active = time.monotonic()

    @staticmethod
    def _normalize_cookie(c: dict) -> dict:
        """Coerce a persisted/added cookie into the canonical attribute shape.

        Missing optional attributes get RFC 6265 defaults: path ``/``, not
        secure, not host-only (bare-domain cookies keep the historical
        subdomain-broadening so persisted jars behave as before), no expiry.
        """
        out = dict(c)
        out["name"] = str(out.get("name", ""))
        out["value"] = str(out.get("value", ""))
        out["domain"] = str(out.get("domain", "")).lower().lstrip(".").rstrip(".")
        out["path"] = str(out.get("path") or "/")
        out["secure"] = bool(out.get("secure", False))
        out["host_only"] = bool(out.get("host_only", False))
        expires = out.get("expires")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            out.pop("expires", None)
        return out

    # -- cookie management -------------------------------------------------
    def add_cookie(self, name: str, value: str, domain: str, *, path: str = "/",
                   secure: bool = False, expires: float | None = None,
                   host_only: bool = False, same_site: str | None = None) -> None:
        """Record a cookie ``name=value`` scoped to ``domain`` (#99).

        Optional browser attributes are preserved so matching can follow
        RFC 6265: ``path`` (default ``/``), ``secure`` (HTTPS-only),
        ``expires`` (epoch seconds), and ``host_only`` (exact-host matching;
        when ``False`` the cookie also matches subdomains, the historical
        behavior). A cookie with the same (name, domain, path, host_only)
        replaces the previous one in place, keeping its creation position
        (RFC 6265 §5.3).
        """
        cookie = self._normalize_cookie({
            "name": name, "value": value, "domain": domain,
            "path": path, "secure": secure, "expires": expires,
            "host_only": host_only,
            "sameSite": same_site,
        })
        key = (cookie["name"], cookie["domain"], cookie["path"], cookie["host_only"])
        for i, existing in enumerate(self.cookie_jar):
            existing_key = (existing["name"], existing["domain"],
                            existing["path"], existing["host_only"])
            if existing_key == key:
                self.cookie_jar[i] = cookie  # replace, keep creation order
                return
        self.cookie_jar.append(cookie)

    def cookies(self) -> list[dict]:
        """Return a shallow copy of the cookie jar."""
        return list(self.cookie_jar)

    @staticmethod
    def _path_matches(request_path: str, cookie_path: str) -> bool:
        """RFC 6265 §5.1.4 path-match."""
        if not request_path.startswith("/"):
            request_path = "/" + request_path
        if request_path == cookie_path:
            return True
        return (request_path.startswith(cookie_path)
                and (cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/")))

    def cookies_for_url(self, url: str, *, same_site: bool = False,
                        top_level_navigation: bool = False, method: str = "GET") -> list[dict]:
        """Cookies to send to ``url`` under RFC 6265 §5.4 (#99).

        Filters the jar by domain-match (§5.1.3: exact for host-only
        cookies, host or subdomain for domain cookies), path-match
        (§5.1.4), the Secure flag, and expiry; orders the survivors by
        longer cookie-path first, stable on creation order for equal
        paths. Explicit browser SameSite attributes require a same-site
        context or, for Lax, a safe top-level navigation. With no context,
        Strict/Lax cookies are withheld; legacy jars without SameSite retain
        RFC 6265 selection. Returns copies so callers cannot mutate the jar.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return []
        host = (parsed.hostname or "").lower().rstrip(".")
        request_path = parsed.path or "/"
        is_secure = (parsed.scheme or "").lower() == "https"
        now = time.time()
        matched: list[dict] = []
        for c in self.cookie_jar:
            policy = str(c.get("sameSite") or c.get("same_site") or "").lower()
            if policy == "none" and not c["secure"]:
                continue
            if policy and policy != "none" and not same_site:
                if policy != "lax" or not top_level_navigation or method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
                    continue
            if c["secure"] and not is_secure:
                continue
            expires = c.get("expires")
            if expires is not None and now >= expires:
                continue
            cdomain = c["domain"]
            if not cdomain:
                continue
            if c["host_only"]:
                if host != cdomain:
                    continue
            elif host != cdomain and not host.endswith("." + cdomain):
                # Lookalike suffixes (notexample.com vs example.com) never match.
                continue
            if not self._path_matches(request_path, c["path"]):
                continue
            matched.append(dict(c))
        matched.sort(key=lambda c: -len(c["path"]))
        return matched

    def cookie_header_for_url(self, url: str, *, same_site: bool = False,
                              top_level_navigation: bool = False, method: str = "GET") -> str:
        """Build a ``Cookie`` header value for a full target URL (#99).

        Applies the RFC 6265 §5.4 selection and ordering from
        :meth:`cookies_for_url`. Returns ``""`` when no cookie applies.
        """
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies_for_url(
            url, same_site=same_site, top_level_navigation=top_level_navigation, method=method,
        ))

    def cookie_header(self, url: str) -> str:
        """Build a header for a full HTTP(S) URL; domain-only replay is unsafe."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("cookie_header requires a full HTTP(S) URL; use cookie_header_for_url")
        return self.cookie_header_for_url(url)

    # -- proxy --------------------------------------------------------------
    def set_proxy(self, proxy: Optional[str]) -> None:
        """Set (or clear with ``None``) the egress proxy for this session."""
        self._proxy = proxy

    def proxy(self) -> Optional[str]:
        """Return the current proxy (``None`` if unset)."""
        return self._proxy

    # -- activity ------------------------------------------------------------
    def touch(self) -> None:
        """Record that this session was just used."""
        self._last_active = time.monotonic()

    def last_active(self) -> float:
        """Return the timestamp (epoch-fractional) of the last :meth:`touch`."""
        return self._last_active

    # -- serialization -------------------------------------------------------
    def to_dict(self, *, include_secrets: bool = False) -> dict:
        """Serialize the profile to a JSON-safe dict."""
        data = {"name": self.name, "last_active": self._last_active}
        if include_secrets:
            data["cookie_jar"] = list(self.cookie_jar)
            data["proxy"] = self._proxy
        else:
            data["cookie_count"] = len(self.cookie_jar)
            data["has_proxy"] = self._proxy is not None
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "SessionProfile":
        """Rebuild a profile from a dict produced by :meth:`to_dict`."""
        return cls(
            name=str(d.get("name", "default")),
            cookie_jar=list(d.get("cookie_jar", []) or []),
            _proxy=d.get("proxy"),
            # Re-baseline on load (#235): a persisted wall-clock stamp is not
            # on the monotonic axis, so the loaded profile starts fresh.
            _last_active=time.monotonic(),
        )


class SessionStore:
    """Thread-safe in-memory registry of :class:`SessionProfile` by name.

    Profiles are created lazily on first :meth:`get` and reused thereafter, so
    concurrent fetchers that ask for the same ``name`` share one identity — the
    thing Scout 19 demands. All mutation points are guarded by a :class:`Lock`.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, SessionProfile] = {}
        self._lock = threading.RLock()

    def get(self, name: str) -> SessionProfile:
        """Return the profile for ``name``, creating it if absent."""
        with self._lock:
            profile = self._profiles.get(name)
            if profile is None:
                profile = SessionProfile(name=name)
                self._profiles[name] = profile
            return profile

    def list(self) -> list[str]:
        """Return the names of all registered profiles (unsorted)."""
        with self._lock:
            return list(self._profiles.keys())

    def clear(self) -> None:
        """Drop every profile from the store."""
        with self._lock:
            self._profiles.clear()
