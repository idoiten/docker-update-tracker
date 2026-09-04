# Docker Update Tracker

A native Home Assistant replacement for
[What's Up Docker](https://getwud.github.io/wud/) — no separate
container, no MQTT bridge. One `update.` entity per Docker container,
across as many hosts as you like, plus per-host summary entities.

## How it works

1. Reads containers from a
   [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)
   instance on each Docker host (read-only — `CONTAINERS=1 IMAGES=1 POST=0`;
   `EVENTS` is granted by that proxy by default, no extra config needed —
   see [Instant detection](#instant-detection)).
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

## Entities

Per container:
- `update.<name>` — installed vs. latest digest (shortened, e.g. `372d991e5888`)

Per host (one config entry = one host):
- `binary_sensor.<host>_uppdatering_tillganglig` ("Uppdatering
  tillgänglig") — on if any container on that host has an update
  available
- `sensor.<host>_antal_tillgangliga_uppdateringar` ("Antal
  tillgängliga uppdateringar") — count of containers with an update
  available

(Exact `entity_id`s depend on Home Assistant's own global Entity ID
format setting — Settings → Devices & Services → the gear icon — the
Swedish `friendly_name`s above are always exactly as shown regardless.)

## Instant detection

The integration opens a long-lived connection to the proxy's `/events`
endpoint and triggers a refresh within moments of a container actually
restarting (e.g. after `docker compose pull && up -d`) — no more waiting
for the next scheduled scan.

**This works out of the box** — `EVENTS` is one of
[docker-socket-proxy's default-granted API sections](https://github.com/Tecnativa/docker-socket-proxy#access-granted-by-default),
alongside `PING`/`VERSION`, so a standard `CONTAINERS=1 IMAGES=1 POST=0`
proxy already allows it. Nothing to add to your `docker-compose.yaml`.

If a proxy has been explicitly locked down further (`EVENTS=0`), the
integration falls back gracefully to polling only: it logs one warning
per host at startup and doesn't retry, since that needs a config change
rather than a retry to fix.

## Global settings

Docker Hub / GHCR credentials and the scan interval are configured via
**"Configure" on any single host entry** — Settings → Devices & Services
→ Docker Update Tracker → any host → Configure. Saving there writes the
same settings to *every* configured host and reloads them all: a
registry login isn't a per-host concept, so there's no reason to
duplicate it per entry.

- **Scan interval**: 1–168 hours, default 12 (matches a common WUD cron
  cadence). Hourly polling of a couple dozen containers can add up to
  hundreds of registry requests a day. Largely a fallback now that
  [instant detection](#instant-detection) normally catches updates the
  moment they happen.
- **Docker Hub / GHCR username + token** (both optional, independently):
  anonymous registry requests have a low rate limit and can start
  failing with `429 Too Many Requests` under regular use (this is what
  happened during this integration's own testing). Authenticated
  requests get a much higher limit. A Docker Hub
  [access token](https://hub.docker.com/settings/security) or a GHCR
  [personal access token](https://github.com/settings/tokens) with
  `read:packages` both work as the "token" field — use a token, not
  your account password.

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

### 4. (Optional) Set global options

"Configure" on any host entry — see [Global settings](#global-settings)
above.

## Container display names/icons (optional)

Add Docker labels to any container to control how its entity shows up,
independent of the container's own name:

```yaml
labels:
  - dut.friendly_name=Home Assistant
  - dut.icon=mdi:home-assistant
```

Falls back to the raw container name and `mdi:docker` if not set.

## Disclaimer

Read-only against both the Docker API (via the proxy) and the image
registries. No warranty.
