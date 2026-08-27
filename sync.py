import os
import json
import glob
import logging

import yaml
from sqlalchemy.exc import OperationalError

from database import SessionLocal
from models import Container, ComposeService, now_utc
from hosts import load_hosts_config, docker_client_for, container_pk

logger = logging.getLogger("dockyard.sync")


def _commit_or_explain(db, host_name: str):
    """Commits, but detects a read-only database specifically and logs a
    clear, targeted diagnostic instead of just letting a confusing generic
    traceback surface - this is a volume/permissions problem, not something
    retrying will fix on its own, but it also shouldn't crash the caller."""
    try:
        db.commit()
    except OperationalError as e:
        if "readonly database" in str(e).lower():
            logger.error(
                "[%s] Can't save sync results: the database is read-only (%s). This is a "
                "volume/permissions problem, not a bug - check that the data volume isn't "
                "mounted read-only (:ro), that its ownership matches the user this container "
                "runs as, and that the underlying host disk isn't full or remounted read-only. "
                "Dockyard will keep retrying on its own once this is fixed - no restart needed.",
                host_name, e,
            )
        else:
            raise


def _upsert_compose_service(db, host_name: str, path: str, service_name: str, spec: dict, seen_ids: set) -> bool:
    """Upserts one service definition parsed from a compose file - shared by
    both local directory scanning and content pushed by a remote-host agent."""
    if not isinstance(spec, dict):
        return False
    svc_id = f"{host_name}::{path}::{service_name}"
    seen_ids.add(svc_id)

    ports = [str(p) for p in (spec.get("ports") or [])]
    volumes = [str(v) for v in (spec.get("volumes") or [])]
    depends_on = spec.get("depends_on") or []
    if isinstance(depends_on, dict):
        depends_on = list(depends_on.keys())
    networks = spec.get("networks") or []
    if isinstance(networks, dict):
        networks = list(networks.keys())

    env = spec.get("environment") or []
    env_keys = []
    if isinstance(env, dict):
        env_keys = list(env.keys())
    elif isinstance(env, list):
        for item in env:
            env_keys.append(str(item).split("=", 1)[0])

    row = db.get(ComposeService, svc_id)
    if row is None:
        row = ComposeService(id=svc_id)
        db.add(row)

    row.host = host_name
    row.compose_file = path
    row.service_name = service_name
    row.image = spec.get("image")
    row.ports_json = json.dumps(ports)
    row.volumes_json = json.dumps(volumes)
    row.env_keys_json = json.dumps(sorted(env_keys))
    row.depends_on_json = json.dumps(depends_on)
    row.networks_json = json.dumps(networks)
    return True


def _parse_compose_content(db, host_name: str, path: str, content: str, seen_ids: set) -> int:
    try:
        data = yaml.safe_load(content) or {}
    except Exception as e:
        # Expected and harmless: a broad scan of a real filesystem will always
        # turn up .yml/.yaml files that aren't valid YAML at all (vendor test
        # fixtures, YAML 1.0 documents PyYAML can't read, etc.). Full parser
        # detail goes to debug only; sync_compose_host logs one calm summary
        # line instead of a scary traceback per file.
        logger.debug("[%s] Not a valid YAML file, skipped: %s (%s)", host_name, path, e)
        return None

    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        logger.debug("[%s] Not a docker-compose file (no top-level services:), skipped: %s", host_name, path)
        return None  # not a compose file (e.g. a k8s manifest, CI config, or other unrelated YAML)

    count = 0
    for service_name, spec in data["services"].items():
        if _upsert_compose_service(db, host_name, path, service_name, spec, seen_ids):
            count += 1
    return count


def sync_docker_host(host_cfg: dict) -> int:
    """Connect to one configured host's Docker API and upsert its live
    container facts, scoped to that host so multiple hosts never clash."""
    host_name = host_cfg["name"]
    try:
        client = docker_client_for(host_cfg)
    except Exception as e:
        logger.warning("[%s] Could not connect to Docker: %s", host_name, e)
        return 0

    db = SessionLocal()
    count = 0
    try:
        seen_names = set()
        for c in client.containers.list(all=True):
            bare_name = c.name
            try:
                attrs = c.attrs
                pk_name = container_pk(host_name, bare_name)
                seen_names.add(pk_name)

                config = attrs.get("Config", {}) or {}
                net_settings = attrs.get("NetworkSettings", {}) or {}

                # Ports: "8080:80/tcp" style strings, host-facing only
                ports = []
                for cport, bindings in (net_settings.get("Ports") or {}).items():
                    if bindings:
                        for b in bindings:
                            host_ip = b.get("HostIp") or "0.0.0.0"
                            host_port = b.get("HostPort")
                            ports.append(f"{host_ip}:{host_port} -> {cport}")
                    else:
                        ports.append(f"(unpublished) {cport}")

                # Volumes/mounts
                volumes = []
                for m in attrs.get("Mounts", []) or []:
                    src = m.get("Source", "?")
                    dst = m.get("Destination", "?")
                    mode = "ro" if not m.get("RW", True) else "rw"
                    volumes.append(f"{src} -> {dst} ({mode})")

                # Image-declared EXPOSEd ports (e.g. "80/tcp") - distinct from
                # the actually-published ports above. Only useful as a launch
                # fallback for host-network containers, which have no formal
                # port-publish record at all even though these ports are
                # genuinely reachable directly on the host.
                exposed_ports = sorted((config.get("ExposedPorts") or {}).keys())

                # Env var NAMES only - never store values, they may hold secrets
                env_keys = []
                for e in config.get("Env", []) or []:
                    key = e.split("=", 1)[0]
                    env_keys.append(key)

                networks = list((net_settings.get("Networks") or {}).keys())
                network_ips = {
                    net_name: net_info.get("IPAddress")
                    for net_name, net_info in (net_settings.get("Networks") or {}).items()
                    if net_info.get("IPAddress")
                }
                network_mode = (attrs.get("HostConfig", {}) or {}).get("NetworkMode")
                labels = config.get("Labels") or {}

                row = db.get(Container, pk_name)
                if row is None:
                    row = Container(name=pk_name)
                    db.add(row)

                row.archived_at = None  # it's live again, in case it was previously archived
                row.display_name = bare_name
                row.host = host_name
                row.status = attrs.get("State", {}).get("Status", "unknown")
                row.image = config.get("Image", "")
                row.created = attrs.get("Created", "")
                row.ports_json = json.dumps(sorted(ports))
                row.exposed_ports_json = json.dumps(exposed_ports)
                row.volumes_json = json.dumps(sorted(volumes))
                row.env_keys_json = json.dumps(sorted(env_keys))
                row.networks_json = json.dumps(sorted(networks))
                row.network_ips_json = json.dumps(network_ips)
                row.network_mode = network_mode
                row.labels_json = json.dumps(labels)
                row.compose_project = labels.get("com.docker.compose.project")
                row.compose_service = labels.get("com.docker.compose.service")
                count += 1
            except Exception:
                logger.exception("[%s] Failed to process container '%s' - skipping it this pass", host_name, bare_name)
                continue

        # A container no longer seen on this host either gets archived (if it
        # had notes worth keeping) or deleted outright (if it was never
        # documented, so there's nothing to lose). Once a row is archived, it
        # is never touched by this loop again on any later sync - it's
        # permanent until explicitly removed via the "Delete permanently"
        # button. Re-evaluating already-archived rows here was a real bug:
        # a row could get deleted on a later pass if its note briefly didn't
        # satisfy this check, destroying already-preserved documentation.
        for row in db.query(Container).filter(Container.host == host_name).all():
            if row.name in seen_names or row.archived_at is not None:
                continue
            try:
                if row.note is not None and (row.note.summary or row.note.description or row.note.usage_notes
                                              or row.note.debug_notes or row.note.backup_notes
                                              or row.note.links or row.note.tags):
                    row.archived_at = now_utc()
                    row.status = "archived"
                    logger.info("[%s] Archived '%s' (no longer seen, had notes)", host_name, row.name)
                else:
                    logger.info("[%s] Removed '%s' (no longer seen, never documented)", host_name, row.name)
                    db.delete(row)
            except Exception:
                logger.exception("[%s] Failed to archive/remove '%s' - leaving it as-is this pass", host_name, row.name)
                continue

        _commit_or_explain(db, host_name)
    finally:
        db.close()

    return count


def sync_compose_host(host_cfg: dict) -> int:
    """Parse docker-compose.yml/yaml files on disk for one host's design-time
    service facts. Silently does nothing if that host has no compose_dir
    configured, or the directory isn't reachable from Dockyard - live
    docker inspect data still covers most of what matters either way. For
    hosts that push their compose content instead (see ingest_compose_files),
    this is simply skipped. Scans hidden (dot-prefixed) directories too, since
    Python's glob skips them by default and they're common in homelab setups
    (.appdata, .stacks, etc.). Matches ANY .yml/.yaml file with a top-level
    services: key, not just exactly-named docker-compose.yml - many real
    setups use names like docker-compose.prod.yml or media-stack.yml."""
    host_name = host_cfg["name"]
    compose_dir = host_cfg.get("compose_dir")
    if not compose_dir or not os.path.isdir(compose_dir):
        return 0

    patterns = ["**/*.yml", "**/*.yaml"]
    files = set()
    for p in patterns:
        files.update(glob.glob(os.path.join(compose_dir, p), recursive=True, include_hidden=True))
    # macOS AppleDouble resource-fork files (._foo.yml) are never valid text -
    # skip them outright rather than wasting a read attempt and a log line.
    files = {f for f in files if not os.path.basename(f).startswith("._")}

    db = SessionLocal()
    count = 0
    skipped = 0
    try:
        seen_ids = set()
        for path in sorted(files):
            try:
                with open(path, "r") as f:
                    content = f.read()
            except Exception as e:
                logger.debug("[%s] Skipped %s (couldn't read as text: %s)", host_name, path, e)
                skipped += 1
                continue
            try:
                n = _parse_compose_content(db, host_name, path, content, seen_ids)
            except Exception:
                logger.exception("[%s] Failed to parse %s - skipping it this pass", host_name, path)
                skipped += 1
                continue
            if n is None:
                skipped += 1
            else:
                count += n

        if skipped:
            logger.info("[%s] Compose scan: %d service(s) found, %d file(s) skipped "
                        "(not valid docker-compose files - see debug logs for detail)",
                        host_name, count, skipped)

        # Stale-format ids (e.g. from before host-prefixing) and services no
        # longer present both get cleaned up here, scoped to this host.
        for row in db.query(ComposeService).filter(ComposeService.host == host_name).all():
            if row.id not in seen_ids:
                db.delete(row)

        _commit_or_explain(db, host_name)
    finally:
        db.close()

    return count


def ingest_compose_files(host_name: str, files: list) -> int:
    """Accepts pushed compose file content from a remote-host agent (see
    agent/push_compose.py) instead of reading it off local disk. Used for
    hosts that aren't reachable via a shared filesystem mount. `files` is a
    list of {"path": ..., "content": ...} dicts - path is just a label (it
    doesn't need to exist on Dockyard's own filesystem)."""
    db = SessionLocal()
    count = 0
    skipped = 0
    try:
        seen_ids = set()
        for f in files:
            path = f.get("path") or "unknown"
            content = f.get("content") or ""
            try:
                n = _parse_compose_content(db, host_name, path, content, seen_ids)
            except Exception:
                logger.exception("[%s] Failed to parse pushed file %s - skipping it this pass", host_name, path)
                skipped += 1
                continue
            if n is None:
                skipped += 1
            else:
                count += n

        if skipped:
            logger.info("[%s] Compose ingest: %d service(s) found, %d pushed file(s) skipped "
                        "(not valid docker-compose files)", host_name, count, skipped)

        for row in db.query(ComposeService).filter(ComposeService.host == host_name).all():
            if row.id not in seen_ids:
                db.delete(row)

        _commit_or_explain(db, host_name)
    finally:
        db.close()

    return count


def sync_docker() -> int:
    total = 0
    for host_cfg in load_hosts_config():
        try:
            total += sync_docker_host(host_cfg)
        except Exception:
            logger.exception("[%s] sync_docker_host failed unexpectedly - skipping this host this pass",
                              host_cfg.get("name", "?"))
    return total


def sync_compose() -> int:
    total = 0
    for host_cfg in load_hosts_config():
        try:
            total += sync_compose_host(host_cfg)
        except Exception:
            logger.exception("[%s] sync_compose_host failed unexpectedly - skipping this host this pass",
                              host_cfg.get("name", "?"))
    return total


def run_full_sync():
    n_docker = sync_docker()
    n_compose = sync_compose()
    logger.info("Synced %d containers, %d compose services across %d host(s)",
                n_docker, n_compose, len(load_hosts_config()))
    return n_docker, n_compose
