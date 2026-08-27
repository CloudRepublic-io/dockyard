#!/usr/bin/env python3
"""
Runs on a remote server, alongside its docker-socket-proxy, and periodically
pushes that server's docker-compose.yml content to a central Dockyard instance.

This exists for hosts whose compose directory Dockyard can't reach directly
(no shared NFS/SMB mount) - Dockyard still gets live container facts (ports,
status, labels) straight from the Docker API either way; this agent is only
what fills in the compose-file-specific extras (declared ports/volumes,
depends_on) and the "write labels back to compose file" feature.

Only needs read access to the compose directory - no Docker socket, no
write access to anything. Scans hidden (dot-prefixed) directories too, since
those are common in homelab setups (.appdata, .stacks, etc.) - Python's glob
skips them by default otherwise. Matches ANY .yml/.yaml file with a top-level
services: key, not just exactly-named docker-compose.yml - many real setups
use names like docker-compose.prod.yml or media-stack.yml.

Env vars:
  DOCKYARD_URL          e.g. http://central-dockyard-host:8088 (required)
  DOCKYARD_INGEST_TOKEN must match the central instance's DOCKYARD_INGEST_TOKEN (required)
  DOCKYARD_HOST_NAME    must match this host's "name" in the central DOCKYARD_HOSTS config (required)
  COMPOSE_DIR           directory to scan, recursively (default: /compose)
  PUSH_INTERVAL         seconds between pushes (default: 300); set to 0 to push once and exit

NOTE on PUSH_INTERVAL=0: if you deploy this with `restart: unless-stopped` (as
in the example docker-compose.example.yml), a one-shot run will just exit and
immediately get restarted by Docker's restart policy, looking like it "never
stops" even though the script itself exited correctly. PUSH_INTERVAL=0 is
meant for triggering the agent externally (a cron job or systemd timer running
`docker compose run --rm dockyard-compose-agent`, or `restart: "no"`) - not
for a long-running service, which should just use the default interval instead.
"""

import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DOCKYARD_URL = os.environ.get("DOCKYARD_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("DOCKYARD_INGEST_TOKEN", "")
HOST_NAME = os.environ.get("DOCKYARD_HOST_NAME", "")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "/compose")
PUSH_INTERVAL = int(os.environ.get("PUSH_INTERVAL", "300"))

PATTERNS = ["**/*.yml", "**/*.yaml"]

# Cheap, dependency-free check for "does this look like a compose file" -
# a top-level (unindented) `services:` key. Avoids needing a YAML parser just
# to filter candidates; Dockyard's own server-side parser makes the real
# determination when it receives the file.
_SERVICES_KEY_RE = re.compile(r"^services:\s*$", re.MULTILINE)


def looks_like_compose(content: str) -> bool:
    return bool(_SERVICES_KEY_RE.search(content))


def collect_files():
    if not os.path.isdir(COMPOSE_DIR):
        print(f"[dockyard-agent] WARNING: {COMPOSE_DIR!r} does not exist inside this container.",
              file=sys.stderr)
        print("[dockyard-agent]   This is almost always a volume mount mismatch: COMPOSE_DIR must be",
              file=sys.stderr)
        print("[dockyard-agent]   the path INSIDE the container (the right-hand side of your volume", file=sys.stderr)
        print("[dockyard-agent]   mount, e.g. the '/compose' in '/home/you/docker:/compose:ro'), not the", file=sys.stderr)
        print("[dockyard-agent]   path on the host. Run diagnose_compose_scan.py (bundled in this image)", file=sys.stderr)
        print(f"[dockyard-agent]   inside THIS container to check: docker exec <this-container> python3 /diagnose_compose_scan.py {COMPOSE_DIR}", file=sys.stderr)
        return []

    paths = set()
    for pattern in PATTERNS:
        paths.update(glob.glob(os.path.join(COMPOSE_DIR, pattern), recursive=True, include_hidden=True))
    # macOS AppleDouble resource-fork files (._foo.yml) are never valid text.
    paths = {p for p in paths if not os.path.basename(p).startswith("._")}

    files = []
    skipped_not_compose = 0
    for path in sorted(paths):
        try:
            with open(path, "r") as f:
                content = f.read()
        except Exception as e:
            print(f"[dockyard-agent] skipping {path}: {e}", file=sys.stderr)
            continue
        if not looks_like_compose(content):
            skipped_not_compose += 1
            continue
        files.append({"path": path, "content": content})

    if skipped_not_compose:
        print(f"[dockyard-agent] ignored {skipped_not_compose} .yml/.yaml file(s) with no top-level 'services:' key",
              file=sys.stderr)

    if not paths:
        print(f"[dockyard-agent] WARNING: {COMPOSE_DIR!r} exists but no .yml/.yaml files were found anywhere",
              file=sys.stderr)
        print("[dockyard-agent]   inside it, at any depth. Either it's genuinely empty from this container's", file=sys.stderr)
        print("[dockyard-agent]   point of view (wrong volume mount - check what's actually mounted there, e.g.", file=sys.stderr)
        print(f"[dockyard-agent]   'docker exec <this-container> ls -la {COMPOSE_DIR}'), or the mount is present", file=sys.stderr)
        print("[dockyard-agent]   but empty. Run diagnose_compose_scan.py (bundled in this image) inside THIS", file=sys.stderr)
        print(f"[dockyard-agent]   container for a full report: docker exec <this-container> python3 /diagnose_compose_scan.py {COMPOSE_DIR}", file=sys.stderr)
    elif not files:
        print(f"[dockyard-agent] WARNING: found {len(paths)} .yml/.yaml file(s) under {COMPOSE_DIR!r}, but none", file=sys.stderr)
        print("[dockyard-agent]   had a top-level 'services:' key, so none look like compose files. If one of", file=sys.stderr)
        print("[dockyard-agent]   them should be, check it's valid YAML and 'services:' isn't indented under", file=sys.stderr)
        print("[dockyard-agent]   anything else.", file=sys.stderr)

    return files


def push_once() -> bool:
    if not DOCKYARD_URL or not HOST_NAME or not INGEST_TOKEN:
        print("[dockyard-agent] DOCKYARD_URL, DOCKYARD_HOST_NAME, and DOCKYARD_INGEST_TOKEN are all required",
              file=sys.stderr)
        return False

    files = collect_files()
    body = json.dumps({"files": files}).encode("utf-8")
    url = f"{DOCKYARD_URL}/api/hosts/{HOST_NAME}/compose"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {INGEST_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[dockyard-agent] pushed {len(files)} file(s) from {COMPOSE_DIR}: {resp.read().decode()}")
        return True
    except urllib.error.HTTPError as e:
        print(f"[dockyard-agent] push failed: HTTP {e.code} {e.read().decode()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[dockyard-agent] push failed: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    if PUSH_INTERVAL <= 0:
        sys.exit(0 if push_once() else 1)

    while True:
        push_once()
        time.sleep(PUSH_INTERVAL)
