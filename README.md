# Docker Update Tracker

A native Home Assistant replacement for
[What's Up Docker](https://getwud.github.io/wud/) — no separate
container, no MQTT bridge. One `update.` entity per Docker container,
across as many hosts as you like.

## How it works

1. Reads containers from a
   [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)
   instance on each Docker host (read-only — `CONTAINERS=1 IMAGES=1 POST=0`).
2. For each container, reads its current image's digest.
3. Looks up the latest available digest for that same `image:tag` from
   its registry (Docker Hub, GHCR, or anything else — see below).
4. Compares the two. A mismatch means an update is available.

Registry lookups are **generic**, not hardcoded per-registry: each
lookup follows the target registry's own `WWW-Authenticate` challenge
(the standard Docker Registry v2 auth flow), so Docker Hub, GHCR, and
other registries (Quay.io, lscr.io, ...) all work without special-casing.
Verified against 17 real images spanning both registries, including two
awkward edge cases: a repo whose name is literally `latest`
(`openspeedtest/latest`), and a Docker Hub "official/library" image with
no namespace (`eclipse-mosquitto`, resolved to `library/eclipse-mosquitto`).

**This integration is read-only / informational.** It does not pull
images or restart containers — there's no `INSTALL` button. It tells you
what's outdated; updating is still up to you.

## Setup

### 1. Run a docker-socket-proxy on each host you want tracked

```yaml
services:
  docker-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: docker-proxy
    environment:
      CONTAINERS: 1
      IMAGES: 1
      POST: 0          # read-only - never allow writes
    ports:
      - "127.0.0.1:2375:2375"   # bind to localhost only for a local host;
                                 # bind to the host's LAN IP (never 0.0.0.0)
                                 # for a remote host, and firewall it to
                                 # only your Home Assistant host's IP
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    restart: unless-stopped
```

Verify it's read-only before relying on it:
```bash
curl http://<host>:2375/containers/json          # should return data
curl -X POST http://<host>:2375/containers/x/stop # should return 403
```

### 2. Install this integration via HACS

1. HACS → the three dots (top right) → **Custom repositories**
2. Add this repo's URL, category **Integration**
3. Install **Docker Update Tracker**
4. Restart Home Assistant

### 3. Add each host

Settings → Devices & Services → Add Integration → **Docker Update Tracker**.
Give it a name (e.g. "hapc" or "NAS") and the proxy's URL
(`http://127.0.0.1:2375`, `http://10.10.10.10:2375`, ...). Repeat once
per host.

## Disclaimer

Read-only against both the Docker API (via the proxy) and the image
registries. No warranty.
