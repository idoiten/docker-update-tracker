"""API clients for Docker Update Tracker.

Two responsibilities, kept separate:

DockerProxyClient
    Talks to a tecnativa/docker-socket-proxy instance to list containers
    and read each one's current image RepoDigests/RepoTags.

RegistryClient
    Given an image reference (e.g. "ghcr.io/home-assistant/home-assistant:stable"
    or "eclipse-mosquitto:latest"), figures out the correct registry host,
    follows the standard Docker Registry v2 WWW-Authenticate challenge to
    get an anonymous bearer token, and returns the current digest for that
    tag. Deliberately generic (reads the challenge from each registry's own
    401 response) rather than hardcoded to Docker Hub/GHCR specifically, so
    other registries (Quay.io, lscr.io, ...) work without code changes.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator

import aiohttp

from .const import MANIFEST_ACCEPT_HEADER

_LOGGER = logging.getLogger(__name__)

# Parses: Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:x/y:pull"
_AUTH_HEADER_RE = re.compile(r'(\w+)="([^"]*)"')

# Registry v2 tokens don't always include expires_in - fall back to this
# (Docker Hub's own default is 300s; being conservative costs one extra
# token request occasionally, which is far cheaper than a stale-token 401).
DEFAULT_TOKEN_TTL_SECONDS = 60
# Refresh slightly before actual expiry to avoid a request landing right
# as a token expires mid-flight.
TOKEN_EXPIRY_MARGIN_SECONDS = 10


class DockerProxyError(Exception):
    """Raised on any docker-socket-proxy communication failure."""


class DockerProxyPermissionError(DockerProxyError):
    """Raised specifically on a 403 - the proxy's env vars don't allow
    this call (e.g. EVENTS not enabled). Retrying won't help without a
    config change, unlike a transient network error."""


class RegistryError(Exception):
    """Raised on any registry lookup failure."""


class DockerProxyClient:
    """Thin client for a tecnativa/docker-socket-proxy instance."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def list_containers(self) -> list[dict]:
        """Return the raw /containers/json list (running containers only, by default)."""
        url = f"{self._base_url}/containers/json"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DockerProxyError(f"Failed to list containers via {url}: {err}") from err

    async def get_image(self, image_id: str) -> dict:
        """Return the raw /images/<id>/json details for one image."""
        url = f"{self._base_url}/images/{image_id}/json"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DockerProxyError(f"Failed to get image {image_id} via {url}: {err}") from err

    async def stream_events(self) -> AsyncIterator[dict]:
        """Stream Docker container lifecycle events as they happen.

        A long-lived connection - the caller is expected to iterate this
        indefinitely and handle disconnects (this only yields events for
        as long as the underlying HTTP stream stays open; on any error it
        raises rather than silently ending, so the caller can distinguish
        "stream ended cleanly" - which shouldn't normally happen - from
        a real failure).

        Requires the proxy's EVENTS section to be allowed - true by
        default on tecnativa/docker-socket-proxy (see its "granted by
        default" list) unless explicitly revoked with EVENTS=0. Raises
        DockerProxyPermissionError (not the generic DockerProxyError) on
        a 403, so callers can tell "needs reconfiguration" apart from
        "transient network blip, just retry".
        """
        filters = json.dumps({"type": ["container"], "event": ["start", "die"]})
        url = f"{self._base_url}/events"
        try:
            async with self._session.get(
                url,
                params={"filters": filters},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
            ) as resp:
                if resp.status == 403:
                    raise DockerProxyPermissionError(
                        f"{url} returned 403 - has this proxy's EVENTS section "
                        "been explicitly revoked (EVENTS=0)?"
                    )
                resp.raise_for_status()
                async for line in resp.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except DockerProxyPermissionError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DockerProxyError(f"Event stream via {url} failed: {err}") from err


def parse_image_ref(image_ref: str) -> tuple[str | None, str, str]:
    """Parse a Docker image reference into (registry_host_or_None, repo, tag).

    registry_host is None for Docker Hub (the default when no explicit host
    is present). A bare single-segment repo (e.g. "eclipse-mosquitto") gets
    the "library/" prefix Docker Hub requires for its official images.
    """
    if "/" in image_ref:
        last_slash = image_ref.rfind("/")
        prefix = image_ref[:last_slash]
        rest = image_ref[last_slash + 1 :]
        if ":" in rest:
            rest, tag = rest.rsplit(":", 1)
        else:
            tag = "latest"
        path_no_tag = f"{prefix}/{rest}"
    else:
        if ":" in image_ref:
            path_no_tag, tag = image_ref.rsplit(":", 1)
        else:
            path_no_tag, tag = image_ref, "latest"

    first_segment = path_no_tag.split("/", 1)[0]
    if "." in first_segment or ":" in first_segment or first_segment == "localhost":
        registry_host = first_segment
        repo = path_no_tag.split("/", 1)[1]
    else:
        registry_host = None
        repo = path_no_tag
        if "/" not in repo:
            repo = f"library/{repo}"

    return registry_host, repo, tag


class RegistryClient:
    """Looks up the current manifest digest for an image:tag from its registry.

    credentials, if given, maps a registry host (e.g. "registry-1.docker.io",
    "ghcr.io") to a (username, password_or_token) tuple, sent as HTTP Basic
    Auth on the token request for that host. Authenticated requests get a
    much higher rate limit than anonymous ones (this is what triggered
    429 Too Many Requests during heavy manual testing - see CHANGELOG).
    Hosts with no entry fall back to the existing anonymous flow.

    IMPORTANT: registry bearer tokens are short-lived (Docker Hub's default
    is 300 seconds) - the cache stores an expiry timestamp per token and
    transparently refetches once it's past that, rather than caching
    forever. Caching forever (the original bug) worked on the very first
    lookup after credentials were saved, then silently started returning
    401 Unauthorized on every subsequent scan once the token actually
    expired, since an expired-but-present token skipped the "no token yet"
    retry path entirely.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._session = session
        # cache_key -> (token, expires_at_monotonic)
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._credentials = credentials or {}

    async def get_latest_digest(self, image_ref: str) -> str:
        """Return the current 'Docker-Content-Digest' for image_ref's tag."""
        registry_host, repo, tag = parse_image_ref(image_ref)
        host = registry_host or "registry-1.docker.io"
        manifest_url = f"https://{host}/v2/{repo}/manifests/{tag}"

        token = await self._get_token(host, repo)
        headers = {"Accept": MANIFEST_ACCEPT_HEADER}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        digest = await self._fetch_manifest_digest(manifest_url, headers)
        if digest is not None:
            return digest

        # 401 despite having a token: either it was stale (shouldn't
        # happen now that expiry is tracked, but be defensive - e.g. the
        # registry could revoke early) or we never had one. Either way,
        # force a fresh token once and retry exactly once.
        token = await self._get_token(host, repo, force_refresh=True)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        digest = await self._fetch_manifest_digest(manifest_url, headers, raise_on_401=True)
        if digest is None:
            raise RegistryError(f"No Docker-Content-Digest in response for {image_ref}")
        return digest

    async def _manifest_request(
        self, method: str, url: str, headers: dict
    ) -> tuple[int, str | None, str]:
        """One HEAD or GET to a manifest URL.

        Returns (status, digest_or_None, www_authenticate_header_or_empty).
        """
        try:
            request_fn = self._session.head if method == "head" else self._session.get
            async with request_fn(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                return (
                    resp.status,
                    resp.headers.get("Docker-Content-Digest"),
                    resp.headers.get("WWW-Authenticate", ""),
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RegistryError(f"Failed to query manifest via {url} ({method}): {err}") from err

    async def _fetch_manifest_digest(
        self, manifest_url: str, headers: dict, raise_on_401: bool = False
    ) -> str | None:
        """HEAD the manifest (GET fallback if unsupported).

        Returns the digest, or None on a 401 (caller decides whether to
        retry) unless raise_on_401 is set.

        HEAD is used deliberately: Docker Hub's own docs state a GET on
        a manifest "emulates a real pull and counts" against the pull
        rate limit, while a HEAD "won't" - both return identical headers
        (including Docker-Content-Digest), we never read the body either
        way, so this is a pure win. Confirmed working identically on
        GHCR too (manual test, 2026-09-04). Falls back to GET on a 405,
        so any registry that doesn't support HEAD on this endpoint still
        works exactly as before - no regression risk.
        """
        status, digest, _ = await self._manifest_request("head", manifest_url, headers)
        method_used = "HEAD"
        if status == 405:
            status, digest, _ = await self._manifest_request("get", manifest_url, headers)
            method_used = "GET (405 fallback)"

        _LOGGER.debug(
            "Manifest check via %s -> status=%s digest=%s url=%s",
            method_used,
            status,
            digest,
            manifest_url,
        )

        if status == 401:
            if raise_on_401:
                raise RegistryError(f"{manifest_url} returned 401 Unauthorized")
            return None
        if status >= 400:
            raise RegistryError(f"{manifest_url} returned HTTP {status}")
        if not digest:
            raise RegistryError(f"No Docker-Content-Digest in response from {manifest_url}")
        return digest

    async def _get_token(
        self, host: str, repo: str, force_refresh: bool = False
    ) -> str | None:
        """Get a bearer token, from cache if still valid, else fetch fresh."""
        cache_key = f"{host}:{repo}"
        if not force_refresh:
            cached = self._token_cache.get(cache_key)
            if cached:
                token, expires_at = cached
                if time.monotonic() < expires_at:
                    return token

        probe_url = f"https://{host}/v2/{repo}/manifests/latest"
        probe_headers = {"Accept": MANIFEST_ACCEPT_HEADER}
        try:
            status, _, challenge = await self._manifest_request(
                "head", probe_url, probe_headers
            )
            if status == 405:
                status, _, challenge = await self._manifest_request(
                    "get", probe_url, probe_headers
                )
        except RegistryError:
            return None

        if status != 401:
            return None  # registry didn't ask for auth at all
        token, ttl = await self._token_from_challenge(challenge, host, repo)

        if token:
            expires_at = time.monotonic() + max(ttl - TOKEN_EXPIRY_MARGIN_SECONDS, 5)
            self._token_cache[cache_key] = (token, expires_at)
        return token

    async def _token_from_challenge(
        self, challenge: str, host: str, repo: str
    ) -> tuple[str | None, int]:
        """Follow a WWW-Authenticate: Bearer ... challenge string and fetch a token.

        Returns (token_or_None, ttl_seconds).
        """
        if not challenge.lower().startswith("bearer"):
            return None, DEFAULT_TOKEN_TTL_SECONDS

        params = dict(_AUTH_HEADER_RE.findall(challenge))
        realm = params.get("realm")
        if not realm:
            return None, DEFAULT_TOKEN_TTL_SECONDS

        query = {k: v for k, v in params.items() if k != "realm"}
        query.setdefault("scope", f"repository:{repo}:pull")

        creds = self._credentials.get(host)
        auth = aiohttp.BasicAuth(*creds) if creds else None

        try:
            async with self._session.get(
                realm, params=query, auth=auth, timeout=aiohttp.ClientTimeout(total=15)
            ) as token_resp:
                token_resp.raise_for_status()
                data = await token_resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RegistryError(f"Failed to fetch auth token from {realm}: {err}") from err

        token = data.get("token") or data.get("access_token")
        ttl = data.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)
        return token, ttl
