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

import logging
import re

import aiohttp

from .const import MANIFEST_ACCEPT_HEADER

_LOGGER = logging.getLogger(__name__)

# Parses: Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:x/y:pull"
_AUTH_HEADER_RE = re.compile(r'(\w+)="([^"]*)"')


class DockerProxyError(Exception):
    """Raised on any docker-socket-proxy communication failure."""


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
    """Looks up the current manifest digest for an image:tag from its registry."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token_cache: dict[str, str] = {}

    async def get_latest_digest(self, image_ref: str) -> str:
        """Return the current 'Docker-Content-Digest' for image_ref's tag."""
        registry_host, repo, tag = parse_image_ref(image_ref)
        host = registry_host or "registry-1.docker.io"
        manifest_url = f"https://{host}/v2/{repo}/manifests/{tag}"

        token = await self._get_token(host, repo)
        headers = {"Accept": MANIFEST_ACCEPT_HEADER}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with self._session.get(
                manifest_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 401 and not token:
                    # Some registries require the challenge round-trip even
                    # for a first request; retry once after reading it.
                    token = await self._token_from_challenge(resp, host, repo)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        async with self._session.get(
                            manifest_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                        ) as resp2:
                            resp2.raise_for_status()
                            digest = resp2.headers.get("Docker-Content-Digest")
                else:
                    resp.raise_for_status()
                    digest = resp.headers.get("Docker-Content-Digest")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RegistryError(f"Failed to query manifest for {image_ref}: {err}") from err

        if not digest:
            raise RegistryError(f"No Docker-Content-Digest in response for {image_ref}")
        return digest

    async def _get_token(self, host: str, repo: str) -> str | None:
        """Get (and cache) an anonymous bearer token via a probe request."""
        cache_key = f"{host}:{repo}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        probe_url = f"https://{host}/v2/{repo}/manifests/latest"
        try:
            async with self._session.get(
                probe_url,
                headers={"Accept": MANIFEST_ACCEPT_HEADER},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 401:
                    return None  # registry didn't ask for auth at all
                token = await self._token_from_challenge(resp, host, repo)
        except (aiohttp.ClientError, TimeoutError):
            return None

        if token:
            self._token_cache[cache_key] = token
        return token

    async def _token_from_challenge(
        self, resp: aiohttp.ClientResponse, host: str, repo: str
    ) -> str | None:
        """Follow a WWW-Authenticate: Bearer ... challenge and fetch a token."""
        challenge = resp.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer"):
            return None

        params = dict(_AUTH_HEADER_RE.findall(challenge))
        realm = params.get("realm")
        if not realm:
            return None

        query = {k: v for k, v in params.items() if k != "realm"}
        query.setdefault("scope", f"repository:{repo}:pull")

        try:
            async with self._session.get(
                realm, params=query, timeout=aiohttp.ClientTimeout(total=15)
            ) as token_resp:
                token_resp.raise_for_status()
                data = await token_resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RegistryError(f"Failed to fetch auth token from {realm}: {err}") from err

        return data.get("token") or data.get("access_token")
