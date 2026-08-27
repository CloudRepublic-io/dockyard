# Dockyard

A small self-hosted app for documenting the Docker containers on your server.

It merges three layers into one page per container:

1. **Live facts**, pulled from the Docker socket (`docker inspect`): status, image,
   ports, volumes, networks, env var *names* (never values).
2. **Compose-file facts**, parsed straight from your `docker-compose.yml` files on
   disk: declared ports/volumes, `depends_on`, image.
3. **Your notes** — a form for the stuff no label or inspect call can tell you: how
   to run it, how to debug it, how to back it up, links to the real docs.

Layers 1 and 2 refresh automatically (on startup, every 5 minutes, and on demand via
the **Sync** button). Layer 3 only changes when you edit it, so your notes never get
clobbered by a re-sync.

## Quick start

1. Copy this whole `dockyard/` folder onto your server.
2. Edit `docker-compose.yml` and change the `/srv/docker:/compose:ro` volume line to
   point at wherever your own `docker-compose.yml` files actually live (it's scanned
   recursively, so pointing it at a parent folder containing several project folders
   works fine).
3. From inside the `dockyard/` folder, run `docker compose up -d --build`.
4. Visit `http://<your-server-ip>:8088`.

That's it — Dockyard will connect through `docker-socket-proxy`, pull in every
running/stopped container it can see, scan your compose files, and show you a grid
of containers to click into.

## Why the docker-socket-proxy sidecar?

Mounting `/var/run/docker.sock` straight into a container — even "read-only" as a
*file* — does **not** limit what that container can do with the Docker API; the
socket still grants full control (starting/stopping containers, reading all
secrets in env vars, etc.), because "read-only" only applies to the bind mount, not
to the requests sent over it. The included `docker-socket-proxy` service sits in
front of the real socket and only allows the specific read-only endpoints Dockyard
actually needs (`CONTAINERS`, `IMAGES`, `NETWORKS`, `VOLUMES`), with all `POST`
(write) calls blocked. If you'd rather skip the proxy for simplicity, you can mount
the socket directly into `dockyard` instead — just know that trade-off exists.

## Day to day use

- **List view** (`/`): every container as a card — status light, image, your
  one-line summary, tags. Search by name/image/tag, filter by status.
- **Detail view** (`/container/<name>`): the full manifest — runtime facts, matching
  compose-file facts, and your written notes (usage, debugging, backup/restore,
  links), rendered from Markdown.
- **Edit** (`/container/<name>/edit`): the form where you write the notes layer.
  Nothing here is ever overwritten by a sync.
- **Sync button**: re-scans right now instead of waiting for the 5-minute timer.
  Change the interval with the `DOCKYARD_SYNC_INTERVAL` env var (seconds).

## Data & backups

Everything Dockyard knows — auto-pulled facts *and* your notes — lives in one
SQLite file at `./data/dockyard.db` (mounted from the host via the compose file).
Back that single file up and you have your whole documentation set; it's plain
SQLite, so `sqlite3 data/dockyard.db .dump` works if you ever want to export it.

**Stacks & Traefik** — Dockyard already reads a container's full label set, so it
surfaces two more things from that for free:

- **Stacks**: containers started via Compose carry `com.docker.compose.project`,
  which Dockyard shows as a clickable chip on every card and in a filter dropdown
  in the header, so you can view "everything in the `media` stack" at a glance.
- **Traefik**: if a container has `traefik.http.routers.*` labels, Dockyard parses
  them into a plain-English "Reverse proxy" section on the detail page — the
  routing rule, entrypoints, backend port, TLS/cert resolver, and middlewares —
  so you don't have to go check Traefik's own dashboard to answer "how do I reach
  this?". It's read straight off the labels, so it only appears when present; no
  configuration needed. See `traefik_labels.py` for the exact label patterns
  recognized.

**JSON API** (`/api/containers`, `/api/containers/<name>`) — the exact same data
as the Markdown export, as JSON, for scripts/dashboards/other tools to consume.
Both endpoints (and the export) are backed by the same `collect_container_records()`
function in `export.py`, so they can never drift out of sync with each other.

**Writing labels back to your compose file** — by default, Dockyard's notes live
only in its own SQLite database. If you'd rather a short version of them travel
with the compose file itself (so it's not locked inside Dockyard), each container's
detail page has a "Write summary & tags to this compose file" button (behind a
confirmation prompt, since it edits a file on disk) that adds two label
namespaces to that service:

- `dockyard.summary` / `dockyard.tags` / `dockyard.docs` - Dockyard's own keys.
- `org.opencontainers.image.description` / `org.opencontainers.image.documentation`
  - the standard [OCI annotations](https://github.com/opencontainers/image-spec/blob/master/annotations.md),
  so any other OCI-aware tool can read the same summary/docs link, not just Dockyard.

This uses round-trip-safe YAML editing (`ruamel.yaml`) that preserves the rest of
the file's formatting and comments - only those specific label keys change.
Re-running it overwrites those same keys in place rather than duplicating them.
This is **off by default** and requires two things:

1. `DOCKYARD_ALLOW_COMPOSE_WRITE=true` (env var, commented out by default).
2. The compose directory actually mounted read-write (the default
   `docker-compose.yml` mounts it `:ro` on purpose - you'd need to change that
   line too).

A one-time `.dockyard-bak` copy of the file is kept alongside it before the first
write. Only short scalar fields go back into labels — the long-form notes
(usage/debug/backup/links) stay in Dockyard's database, since multi-line markdown
doesn't belong in a label value.

## Multi-host: centralized dashboard for several servers

Dockyard can watch more than one server's Docker daemon at once, so you get one
dashboard for containers spread across your homelab instead of one Dockyard
instance per box.

**How it works:** set the `DOCKYARD_HOSTS` env var to a JSON array, one entry per
server:

```json
[
  {"name": "media-server", "docker_url": "tcp://192.168.1.50:2375", "compose_dir": "/compose/media-server"},
  {"name": "backup-server", "docker_url": "tcp://192.168.1.60:2375"}
]
```

- **`name`** — how the host shows up in Dockyard's UI, filters, and exports.
- **`docker_url`** — anything docker-py's client accepts: `tcp://host:2375`,
  `ssh://user@host`, or `unix:///path`. See the security note below before
  picking one.
- **`compose_dir`** — optional. If it's missing, or the path isn't actually
  reachable from inside the Dockyard container, Dockyard just skips
  compose-file parsing and the "write labels back to compose file" button for
  that host's containers. Everything else - status, ports, volumes, networks,
  stack name, Traefik labels, OCI labels - still comes from the live Docker API
  regardless, since none of that needs filesystem access.

Leave `DOCKYARD_HOSTS` unset entirely and Dockyard behaves exactly like a
single-host install always has - there's no migration needed and no visible
change; it's still watching one implicit "local" host through the same
`docker-socket-proxy` your `docker-compose.yml` already sets up.

**Once you do set `DOCKYARD_HOSTS`, local containers don't disappear** - you
don't need to add an entry for `"local"` yourself. If your list only names
remote servers (like the example above), Dockyard automatically includes local
alongside them, using `DOCKYARD_COMPOSE_DIR` for its compose directory just
like before. If you *do* want to customize local's own config (e.g. give it a
different `compose_dir`), add an entry named exactly `"local"` and your
definition is used as-is instead of the automatic one.

**Reaching remote hosts' Docker API safely:** the Docker API is root-equivalent
access to that machine, so however you connect, keep it off the public internet.
The recommended pattern is the same `docker-socket-proxy` sidecar the local setup
already uses, deployed on *each* remote server (read-only, `POST: "0"`), reachable
only over your LAN or a private VPN/mesh network (e.g. Tailscale/WireGuard) - never
port-forwarded to the internet. If your servers aren't on a shared network yet,
that's the first thing to set up before pointing `docker_url` at them.

**Collision handling:** two servers can both run a container literally named
`plex` without conflict - Dockyard stores non-local containers under a
`host::name` key internally, while still showing and linking to them by their
plain name in the UI. The `/api/containers/<name>` endpoint returns a list
instead of a single object if a name is ambiguous across hosts; pass
`?host=<name>` to disambiguate.

**In the UI:** once more than one host is detected, a host filter dropdown
appears in the header (AJAX-driven like the stack filter), and a small host chip
shows up on each card and detail page. With only one host configured, none of
this appears - zero visual change from a single-host install.

**Stale-sync warning:** if a host's Docker API connection breaks (wrong
`docker-socket-proxy` binding, firewall, host down, etc.), that host's
containers just silently stop getting their live facts refreshed - `status`,
`ports`, `labels`, and `last_synced` all freeze at whatever they were the last
time a sync actually succeeded, and the only visible sign was previously
a warning in the server logs. A red "Not syncing" badge now appears next to
that host's name on the dashboard (and on the detail page for its
containers) once its most recent successful sync is older than three sync
intervals (15 minutes by default) - hover it to see exactly when it last
synced. Note this is specific to the live Docker API path; a host reached
only through the compose-push agent keeps updating its compose data
independently of this, since that's a separate mechanism that doesn't touch
`Container` rows at all (see below).

### Compose-push agent (no shared filesystem needed)

`compose_dir` above requires Dockyard to actually be able to read that host's
compose files - fine if you mount an NFS/SMB share in, but not every setup has
that. For hosts where that isn't practical, `agent/` is a complete, standalone
deployment: copy that whole folder to the remote server however you like
(`scp -r`, `rsync`, cloning the repo) - it needs nothing else from the main
project - then:

```bash
cd agent
cp .env.example .env   # fill in DOCKYARD_URL, DOCKYARD_INGEST_TOKEN, DOCKYARD_HOST_NAME, HOST_COMPOSE_DIR
docker compose up -d
```

See **`agent/README.md`** for the full walkthrough, troubleshooting, and the
security notes on the bundled `docker-socket-proxy` service (included for
hosts that also want live container inspection, not just pushed compose
data). In brief:

- `DOCKYARD_URL`/`DOCKYARD_INGEST_TOKEN` must match the central instance
  (`DOCKYARD_INGEST_TOKEN` is set the same way there, and use `https://` -
  the token is sent as a bearer token on every push).
- `DOCKYARD_HOST_NAME` must match that host's `"name"` in the central
  instance's `DOCKYARD_HOSTS` config.
- The agent re-pushes on an interval (`PUSH_INTERVAL`, default 5 minutes),
  replacing its host's previous compose data each time, so removed services
  disappear from Dockyard too, not just added ones.

Pushed data is parsed identically to a locally-scanned directory and shows up
the same way for reading - declared ports/volumes, matching to the right
container, all of it. Dockyard can't tell the difference between the two
sources for anything read-only.

**Write-back is the one exception.** The "write labels back to compose file"
button needs Dockyard to have direct filesystem access to that file, and the
agent only ever sends compose *content* over HTTP - it never gives Dockyard an
actual writable path. So for a host reached only through the agent, the detail
page explains this and doesn't offer the button; write-back only works when
Dockyard can see that host's compose directory directly (the local host, or a
remote host you've shared in via NFS/SMB instead of the agent).

Both the agent and Dockyard's own local `compose_dir` scan look inside hidden
(dot-prefixed) directories too - e.g. `.appdata/`, `.stacks/` - since those are
common in homelab setups and Python's directory scanning skips them by default
otherwise. Matching is also based on content, not filename - any `.yml`/`.yaml`
file with a top-level `services:` key counts, so `docker-compose.prod.yml` or
`media-stack.yml` work just as well as the exact name `docker-compose.yml`.

If you set `PUSH_INTERVAL=0` to push once and exit (for triggering the agent
externally via cron/systemd, rather than running it as a persistent service),
make sure its `restart` policy is `"no"`, not `unless-stopped` - otherwise
Docker just restarts the container the instant it exits, which looks like the
agent "never stops" even though the script itself exited correctly each time.
`agent/README.md` has a note on this.

**If compose files still aren't showing up**: the agent itself now self-diagnoses
and prints a specific warning in its logs (`docker logs <agent-container>`) the
moment it finds nothing - it distinguishes "this directory doesn't exist",
"it exists but is empty", and "files exist but none look like compose files",
and tells you what to check for each.

The single most common cause is a **path mismatch between the host and the
container**: `COMPOSE_DIR` must be set to the path *inside the container* - the
right-hand side of your volume mount (e.g. the `/compose` in
`/home/you/docker:/compose:ro`) - never the path as it exists on the host. If
you run a check against the host's filesystem directly (outside Docker), of
course the files are there; that doesn't tell you whether the agent's own
container can see them.

For a full report, `diagnose_compose_scan.py` is bundled into both Dockyard's
own image and the agent's image, so you can run it **inside the exact
container that's failing** and see precisely what that container's filesystem
looks like:

```bash
# for the agent:
docker exec <agent-container> python3 /diagnose_compose_scan.py $COMPOSE_DIR

# for Dockyard's own local compose_dir scan:
docker compose exec dockyard python3 /app/scripts/diagnose_compose_scan.py <compose_dir>
```

It walks the directory exactly the way the real scan does, prints everything it
can see, and points out the most common causes: a missing or empty mount, a
compose file with no top-level `services:` key, or (once files are confirmed
found) a `DOCKYARD_HOST_NAME` that doesn't match the host's name in
`DOCKYARD_HOSTS`.

## Integrating with Portainer / Podman

Dockyard's documentation isn't locked inside its own UI - the same
write-labels-to-compose-file feature used for [self-documentation](#features)
also makes your notes visible directly inside other tools that already show
Docker labels, like Portainer and Podman. No new code or plugins needed, just
this workflow:

1. **Document the container in Dockyard** - open its detail page → **Edit
   notes** → fill in at least a **Summary** and any **Tags** (these are the two
   fields that get written back to labels). Save.
2. **Enable write-back** (one-time setup, if you haven't already) - set
   `DOCKYARD_ALLOW_COMPOSE_WRITE=true` on the Dockyard container, make sure the
   compose directory is mounted **read-write** (not `:ro`, check the volume
   line in Dockyard's own `docker-compose.yml`), and restart Dockyard for the
   env var to take effect.
3. **Write the labels** - back on that container's detail page, scroll to the
   "Compose definition" section → check **Dockyard labels** and/or **OCI
   annotations** (both is fine, they serve different tools) → click **Write to
   this compose file** → confirm the prompt.
4. **Check the compose file** - it now has new label lines under that service:
   `dockyard.summary=...`, `dockyard.tags=...`, `dockyard.docs=...`,
   `org.opencontainers.image.description=...`, and
   `org.opencontainers.image.documentation=...`. A one-time `.dockyard-bak`
   copy of the original file is kept alongside it.
5. **Recreate the container so the labels actually apply** - labels are set at
   creation time, not live-patchable on a running container, so a restart
   alone won't pick them up. Run `docker compose up -d` for that stack.
6. **View it in Portainer or Podman** - in **Portainer**: Containers → click
   the container → the details page's Labels section. In **Podman**:
   `podman inspect <container>` (look under `Config.Labels`), or Podman
   Desktop's container details view.

The `dockyard.docs` / `org.opencontainers.image.documentation` label is a live
link straight back to that container's full Dockyard page - worth clicking
through from either tool when you need the deeper usage/debug/backup notes
that don't fit in a label.

**On Podman specifically:** since Podman exposes a Docker-API-compatible
socket (`podman system service`), a `DOCKYARD_HOSTS` entry can likely point
its `docker_url` directly at that socket, letting Dockyard monitor a Podman
host the same way it does a Docker one - this hasn't been tested against a
live Podman instance, so if the connection doesn't behave as expected,
`scripts/diagnose_compose_scan.py` and the sync logs are the first places to
check.

## Archive: documented containers that disappear

If a container that has notes attached to it ever stops showing up on its host
(moved to another server, renamed, decommissioned), Dockyard doesn't just quietly
delete it and lose your documentation. Instead:

- It's marked **archived** (with a timestamp of when it vanished) and dropped out
  of the main dashboard, so your day-to-day view only ever shows what's actually
  running.
- Its notes, tags, and last-known facts (image, ports, stack, etc.) stay exactly
  as they were, viewable at **Archive** in the header, or its same
  `/container/<name>` detail page (now showing a banner explaining it's archived).
- If a container with the same name reappears on that host later, it automatically
  un-archives itself and picks back up normally - no manual step needed.
- An archived entry can be deleted permanently from its detail page if you're done
  with it; this is refused for anything still live, so it can't be used to
  accidentally wipe a running container's documentation.

Undocumented containers that disappear are still just removed outright, same as
before - there's nothing to preserve there, so nothing is archived unnecessarily.

## Features

**Launch button** — a green external-link icon on both the card grid and the
detail page opens that container's actual web UI in a new tab, when Dockyard
can work out where it lives, and only for containers that are actually
running (a stopped container has nothing listening, however it's configured).
A working Traefik route (`traefik.http.routers.*` rule) always takes
priority, since it's the most reliable source - Dockyard parses the
`Host(...)` (and `PathPrefix(...)`, if present) straight out of the rule and
uses `https://` automatically if TLS is configured. If there's no Traefik
route, it builds a candidate from each published TCP port (UDP ports, like a
DNS server's own 53/udp, are never a browsable web UI, so those are skipped):
an explicit non-wildcard bind IP is used directly; a wildcard bind
(`0.0.0.0`/`::`, the common case) uses that host's known address for remote
hosts (parsed from `docker_url`), or the browser's own current hostname for
the local host. A candidate is also added for the container's own IP on any
custom (non-default-bridge) network paired with its declared `EXPOSE` ports -
this matters for macvlan/ipvlan setups, where a container gets a real address
of its own directly on the LAN: Docker's `-p` publishing there is often
recorded without actually creating a working host-side forward at all, so
without this, the button would point at a port that looks configured but
never responds. If there's more than one viable candidate and no Traefik
route to disambiguate which one is the real web UI, the button becomes a
small dropdown to pick from instead of guessing. Host-network containers
(`network_mode: host`) are a related special case - they have no formal
port-publish record of their own at all, so as a last resort the button falls
back to the declared `EXPOSE` ports resolved against the host's own address,
since a host-network container's ports genuinely are the host's own ports.
Containers with nothing usable at all simply don't get a button.

**Header menu** — a hamburger icon in the header opens a dropdown with Sync,
Export, Archive, Guide, and a light/dark theme toggle, in that order. Your
theme choice is remembered (via `localStorage`) and reapplied instantly on the
next visit, before the page even paints, so there's no flash of the wrong
theme. Dark is the default.

**Accurate compose-file matching** — if a compose directory has more than one
file defining a service with the same name (e.g. a real `docker-compose.yml`
alongside a `docker-compose.example.yml` some setups keep around for
reference), Dockyard doesn't guess: it reads the exact file Docker itself
recorded creating the container from (the
`com.docker.compose.project.config_files` label docker compose sets), matched
by filename so it isn't thrown off by Dockyard's mount path differing from the
original host's path. Only falls back to a plain service-name guess if that
label isn't present at all.

**Container IP addresses** — the detail page's Networks column shows each
network's actual IP address for that container (`bridge — 172.17.0.2`,
`media_network — 10.0.5.3`, etc.), not just the network name, so containers that
are only reachable over an internal Docker network - never published to a host
port - still have a real, useful address on their page. If a container uses host
networking instead (`network_mode: host`), that's called out explicitly rather
than showing a confusing blank, since there's no separate container IP in that
mode. Also included in the JSON API (`runtime.network_ips` / `runtime.network_mode`)
and the Markdown export.

For a remote host in a multi-host setup, this is that container's IP on *its own*
server's Docker network - accurate, but typically only reachable from that same
server, since plain Docker bridge networks are host-local by default. It won't
generally be reachable from wherever Dockyard runs, or from other machines on your
LAN, unless that host uses macvlan/ipvlan networking or Docker Swarm overlay
networks. Dockyard just reports what `docker inspect` says; it can't tell you
whether the address is actually routable from where you're sitting.

**In-app setup guide** (`/guide`, or the Guide button in the header) — this whole
README, rendered inside Dockyard itself with a table-of-contents sidebar linking
to each section, so once one instance is running, everything needed to set up
multi-host, the compose-push agent, write-back, auth, etc. is available without
leaving the app or finding the file on disk.

**Markdown export** (`/export`, or the Export button in the header) — downloads a
single `dockyard-export.md` with every container's runtime facts, compose facts,
publisher-provided docs, and your notes. Useful as an offline copy or something to
drop in a wiki/git repo.

**Live updates via `docker events`** — in addition to the 5-minute poll, Dockyard
runs a background listener on the Docker event stream (start/stop/create/destroy/
rename). When a container's state changes, Dockyard re-syncs immediately instead of
waiting for the next timer tick, so new containers show up right away. It
auto-reconnects if the socket connection drops.

**"NEW" and "NEEDS DOCS" badges** — a container gets a `NEW` badge for its first 24
hours in Dockyard (tracked via `first_seen`, which is set once and never touched by
later syncs), and a `NEEDS DOCS` badge for as long as it has no summary or usage
notes. Both show on the list view and the detail page, so freshly-started
containers with no documentation are easy to spot.

**Publisher-provided docs** — if an image already sets `org.opencontainers.image.*`
annotations or `org.label-schema.*` labels (title, description, documentation URL,
source repo, run/test commands, etc.), Dockyard reads them straight off the
container's labels and shows them in their own section on the detail page and in
the Markdown export, right alongside your own notes. Dockyard also recognizes its
own `dockyard.summary` / `.tags` / `.docs` keys the same way - so the labels the
write-back feature adds to a compose file get read straight back in as soon as
that container's recreated, closing the loop. See `label_specs.py` for the exact
keys recognized.

Dockyard uses this on itself: `docker-compose.yml`'s `dockyard` service ships with
a few of these labels already set (title, description, a documentation link, a
usage hint, and a tag), so Dockyard shows up documented in its own dashboard the
moment it starts - no manual entry needed. Update the documentation URL in there
to match your actual host/port once deployed.

**Optional HTTP Basic Auth** — set both `DOCKYARD_AUTH_USER` and `DOCKYARD_AUTH_PASS`
(env vars, commented out by default in `docker-compose.yml`) to require a login on
every page. Leave either unset and auth is skipped entirely — useful if you're
already putting Dockyard behind a reverse proxy that handles auth itself.

## Further ideas

Beyond what's built:

- Swap SQLite for a shared Postgres if multiple people need to edit notes
  concurrently (last-write-wins is fine for one person, less fine for a team).
- Add a "stale docs" indicator — flag notes that haven't been touched since the
  image's last update.
- Push the Markdown export to a git repo on a schedule for a version-controlled
  documentation history.

## Project layout

```
dockyard/
├── main.py              FastAPI routes
├── models.py             SQLAlchemy models (Container, ComposeService, Note)
├── database.py           SQLite engine/session setup
├── sync.py                Docker socket + compose-file scanning (multi-host aware)
├── hosts.py               Multi-host configuration (DOCKYARD_HOSTS)
├── events.py              Background docker events listener (live updates, per host)
├── export.py              Markdown export builder
├── label_specs.py         OCI annotation / Label Schema key recognition
├── traefik_labels.py      Traefik router label parsing
├── compose_writer.py      Optional write-back of labels into docker-compose.yml
├── auth.py                Optional HTTP Basic Auth guard
├── agent/                 Standalone compose-push agent deployment (see Multi-host)
│   ├── push_compose.py
│   ├── diagnose_compose_scan.py
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── README.md          Self-contained instructions for this folder alone
├── templates/            Jinja2 templates
├── static/style.css      Styling
├── requirements.txt
├── Dockerfile
└── docker-compose.yml    Deploys Dockyard itself
```
