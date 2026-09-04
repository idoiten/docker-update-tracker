"""Constants for Docker Update Tracker."""
from __future__ import annotations

DOMAIN = "docker_update_tracker"
PLATFORMS = ["update", "binary_sensor", "sensor"]

CONF_NAME = "name"
CONF_PROXY_URL = "proxy_url"
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
CONF_DOCKERHUB_USERNAME = "dockerhub_username"
CONF_DOCKERHUB_TOKEN = "dockerhub_token"
CONF_GHCR_USERNAME = "ghcr_username"
CONF_GHCR_TOKEN = "ghcr_token"

DEFAULT_SCAN_INTERVAL_MINUTES = 720  # 12 hours
MIN_SCAN_INTERVAL_MINUTES = 5
MAX_SCAN_INTERVAL_MINUTES = 1440  # 24 hours

GHCR_REGISTRY = "ghcr.io"

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
