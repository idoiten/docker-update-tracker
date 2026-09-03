"""Constants for Docker Update Tracker."""
from __future__ import annotations

DOMAIN = "docker_update_tracker"
PLATFORMS = ["update"]

CONF_NAME = "name"
CONF_PROXY_URL = "proxy_url"

DEFAULT_SCAN_INTERVAL_HOURS = 12

# Registry hosts that are NOT Docker Hub. Anything whose image reference's
# first path segment contains a "." or ":" is treated as an explicit
# registry host (e.g. "ghcr.io/..."); anything else defaults to Docker Hub.
DOCKER_HUB_REGISTRY = "registry-1.docker.io"
DOCKER_HUB_AUTH = "https://auth.docker.io/token"
DOCKER_HUB_SERVICE = "registry.docker.io"

MANIFEST_ACCEPT_HEADER = ",".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)
