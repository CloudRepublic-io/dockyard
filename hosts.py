"""
Dockyard can watch more than one server's Docker daemon, turning it into a
centralized dashboard. Configure this with the DOCKYARD_HOSTS env var - a
JSON array like:

    [
      {"name": "media-server", "docker_url": "tcp://192.168.1.50:2375", "compose_dir": "/compose/media-server"},
      {"name": "backup-server", "docker_url": "tcp://192.168.1.60:2375"}
    ]

- "name" is how the host shows up in Dockyard's UI and filters.
- "docker_url" is anything docker-py's DockerClient(base_url=...) accepts:
  tcp://host:2375 (put a docker-socket-proxy in front of each remote daemon -
  see the README's security note), or ssh://user@host, or unix:///path.
- "compose_dir" is optional per host. If it's not set, or the path isn't
  reachable from inside the Dockyard container (e.g. you haven't shared that
  server's compose directory in), Dockyard simply skips compose-file parsing
  and the "write labels back to compose file" button for that host's
  containers - everything else (ports, volumes, status, stack, Traefik/OCI
  labels) still comes from the live Docker API either way.

If DOCKYARD_HOSTS isn't set at all, Dockyard behaves exactly like a
single-host install always has: one implicit "local" host, using the local
Docker socket and DOCKYARD_COMPOSE_DIR.

Listing only your remote servers in DOCKYARD_HOSTS does NOT drop the local
host - it's automatically included alongside whatever you list, unless you
explicitly add your own entry named "local" (e.g. to give it a custom
compose_dir), in which case your definition is used as-is instead.
"""

import os
import json
import logging
import urllib.parse

import docker

logger = logging.getLogger("dockyard.hosts")

LOCAL_HOST_NAME = "local"


def _local_host_default() -> dict:
    return {
        "name": LOCAL_HOST_NAME,
        "docker_url": None,
        "compose_dir": os.environ.get("DOCKYARD_COMPOSE_DIR", "/compose"),
    }


def load_hosts_config() -> list:
    raw = os.environ.get("DOCKYARD_HOSTS", "").strip()
    if not raw:
        return [_local_host_default()]

    try:
        hosts = json.loads(raw)
        if not isinstance(hosts, list) or not hosts:
            raise ValueError("DOCKYARD_HOSTS must be a non-empty JSON array")
        for h in hosts:
            if "name" not in h:
                raise ValueError("Each host needs a 'name'")
        # Auto-include local unless the user already defined their own entry
        # for it - so listing remote hosts never silently drops local
        # containers, but a custom "local" definition is still respected.
        if not any(h.get("name") == LOCAL_HOST_NAME for h in hosts):
            hosts = [_local_host_default()] + hosts
        return hosts
    except Exception as e:
        logger.warning("Could not parse DOCKYARD_HOSTS (%s) - falling back to local only", e)
        return [_local_host_default()]


def docker_client_for(host_cfg: dict):
    """Raises if it can't connect - callers should catch and skip that host."""
    url = host_cfg.get("docker_url")
    if url:
        client = docker.DockerClient(base_url=url, timeout=10)
    else:
        client = docker.from_env()
    client.ping()
    return client


def container_pk(host_name: str, bare_name: str) -> str:
    """The Container.name primary key: unprefixed for the local host (so
    existing single-host databases need no migration), host-prefixed for
    everything else (so same-named containers on different servers never
    collide)."""
    if host_name == LOCAL_HOST_NAME:
        return bare_name
    return f"{host_name}::{bare_name}"


def host_launch_address(host_cfg: dict):
    """Best-effort 'reachable IP/hostname' for this Docker host, derived from
    its configured docker_url (e.g. tcp://192.168.1.50:2375 -> 192.168.1.50).
    Returns None for the local host, or if docker_url isn't set / isn't a
    tcp:// URL - callers should fall back to the browser's own current
    hostname in that case, since Dockyard is typically reachable at the same
    address as whatever it's monitoring locally."""
    url = host_cfg.get("docker_url")
    if not url:
        return None
    try:
        return urllib.parse.urlparse(url).hostname
    except Exception:
        return None
