# Dockyard compose-push agent

Runs on a remote server and periodically pushes that server's docker-compose
file *content* to a central [Dockyard](https://github.com/CloudRepublic-io/dockyard.git) instance running
elsewhere - for hosts where Dockyard can't reach the compose directory
directly (no shared NFS/SMB mount). Live container facts (status, ports,
labels) still come from the Docker API either way, via the optional
`docker-socket-proxy` service in the same compose file; this agent only fills
in the compose-file-specific extras (declared ports/volumes, `depends_on`,
and the "write labels back to compose file" feature).

This folder is entirely self-contained - copy it to the remote server however
you like (`scp -r`, `rsync`, `git clone` if the whole Dockyard repo is
convenient) and it doesn't need anything else from the main project.

## Quick start

```bash
cp .env.example .env
# edit .env - fill in DOCKYARD_URL, DOCKYARD_INGEST_TOKEN, DOCKYARD_HOST_NAME,
# and HOST_COMPOSE_DIR at minimum

docker compose up -d
```

That's it. Check it's working:

```bash
docker compose logs -f dockyard-compose-agent
```

You should see a line like `pushed N file(s) from /compose: {...}` on each
push. If you see 0 files or a warning instead, the agent explains the likely
cause directly in its own log output - most commonly a `COMPOSE_DIR`/volume
mount mismatch (see the warning text for specifics).

## What's in this folder

| File | Purpose |
|---|---|
| `docker-compose.yml` | The actual deployment - agent + optional docker-socket-proxy |
| `.env.example` | Copy to `.env` and fill in your values (never commit the real `.env`) |
| `Dockerfile` | Builds the agent image (stdlib-only Python, no dependencies) |
| `push_compose.py` | The agent script itself |
| `diagnose_compose_scan.py` | Run this inside the container to debug "why aren't my compose files showing up" - see below |

## Diagnosing compose file discovery

If files aren't being found, run the bundled diagnostic tool **inside this
exact container** (not on the host, not in a different container - it needs
to see precisely what this container's filesystem looks like):

```bash
docker compose exec dockyard-compose-agent python3 /diagnose_compose_scan.py /compose
```

It walks the directory the same way the real scan does, prints everything it
can see, and points out the most common causes: a missing/empty volume mount,
a compose file with no top-level `services:` key, or (the single most common
mistake) confusing the **host** path with the **container** path -
`COMPOSE_DIR` must be `/compose` (the container-side path), never the actual
path on the host's own filesystem.

## Notes on the included hardening

- **Runs as a non-root user** (uid 10001) inside the container. If the agent
  can't read your compose directory, that's almost always why - either make
  the directory world-readable (`chmod -R o+rX /path/to/compose`) or add
  `user: "1000:1000"` (matching your own host user) to override the
  Dockerfile's default in `docker-compose.yml`.
- **`read_only: true`** with a `tmpfs` for `/tmp` - the agent never writes to
  its own filesystem at all (confirmed: it only ever opens compose files for
  reading), so there's nothing this should ever break.
- **`cap_drop: ALL`** and **`no-new-privileges`** - the agent needs no Linux
  capabilities and never elevates privileges.
- **Log rotation** (`max-size`/`max-file`) - without this, logs from a
  service that runs indefinitely will grow unbounded over months of uptime.
- **`mem_limit`**, not `deploy.resources.limits` - the latter is silently
  *ignored* under plain `docker compose up` (it only applies in swarm mode,
  or with `--compatibility`), which is a common gotcha that gives a false
  sense of having limits in place. `mem_limit` actually works.

## PUSH_INTERVAL=0 (cron/systemd timer mode instead of a long-running loop)

If you trigger this externally (`docker compose run --rm
dockyard-compose-agent`) instead of leaving it running continuously, set
`PUSH_INTERVAL=0` in `.env` **and** change `restart: unless-stopped` to
`restart: "no"` in `docker-compose.yml`. Otherwise Docker just restarts the
container the instant it exits after one push, which looks like it "never
stops" even though the script itself exited correctly.

## docker-socket-proxy: read this before enabling it

The proxy service is included and active by default, but bound to
`127.0.0.1` - harmless, but also not reachable from your central Dockyard
instance (a different machine) until you deliberately set `PROXY_BIND_IP` in
`.env`. **Read the security comments directly above that service in
`docker-compose.yml` before doing so** - exposing any Docker API, even a
read-only proxy, needs actual thought about exactly what can reach it. The
short version: prefer a private overlay network (Tailscale/WireGuard) over
binding to a plain LAN IP, and never publish it to every interface with a
bare `2375:2375` mapping.

If you only want the compose-push functionality and don't need live
container inspection of this host, delete the `docker-socket-proxy` service
and the `networks:` section entirely - the agent doesn't use them.
