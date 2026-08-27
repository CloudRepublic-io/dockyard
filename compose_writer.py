"""
Writes a small set of labels back into the actual docker-compose.yml service
definition, so a summary/tags/link back to the full docs travels with the
compose file itself - not just inside Dockyard's database.

Two label namespaces are written together:
- `dockyard.summary` / `dockyard.tags` / `dockyard.docs` - Dockyard's own keys,
  including `tags`, which has no OCI equivalent.
- `org.opencontainers.image.description` / `org.opencontainers.image.documentation`
  - the standard OCI annotations (see the Image Format Specification) that any
  OCI-aware tool already knows how to read, not just Dockyard.

This is deliberately conservative:
- Off by default. Requires DOCKYARD_ALLOW_COMPOSE_WRITE=true AND the compose
  directory to actually be mounted read-write (the recommended docker-compose.yml
  mounts it :ro on purpose).
- Only writes short, scalar fields (summary, tags, a link back to the container's
  Dockyard page) - never the long-form markdown notes, which don't belong in a
  label value.
- Uses ruamel.yaml in round-trip mode, which preserves the file's existing
  formatting, comments, and key order instead of rewriting the whole file from
  a plain dict (which is what PyYAML would do).
- Re-running this overwrites the specific keys listed above each time (so your
  latest summary/tags always win), but touches nothing else in the file.
"""

import os
import shutil

from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 4096  # avoid line-wrapping long label values
yaml.indent(mapping=2, sequence=4, offset=2)  # common docker-compose style

DOCKYARD_PREFIX = "dockyard."
OCI_DESCRIPTION_KEY = "org.opencontainers.image.description"
OCI_DOCUMENTATION_KEY = "org.opencontainers.image.documentation"


def compose_write_enabled() -> bool:
    return os.environ.get("DOCKYARD_ALLOW_COMPOSE_WRITE", "false").strip().lower() in ("1", "true")


def _build_labels(summary: str, tags: str, docs_url: str, include_dockyard: bool, include_oci: bool) -> dict:
    labels = {}
    if include_dockyard:
        if summary:
            labels[f"{DOCKYARD_PREFIX}summary"] = summary
        if tags:
            labels[f"{DOCKYARD_PREFIX}tags"] = tags  # no OCI equivalent for free-form tags
        if docs_url:
            labels[f"{DOCKYARD_PREFIX}docs"] = docs_url
    if include_oci:
        if summary:
            labels[OCI_DESCRIPTION_KEY] = summary
        if docs_url:
            labels[OCI_DOCUMENTATION_KEY] = docs_url
    return labels


def write_labels_to_compose(compose_file: str, service_name: str, summary: str, tags: str, docs_url: str,
                             include_dockyard: bool = True, include_oci: bool = True):
    """Raises on any failure - callers should catch and surface the message."""
    if not compose_write_enabled():
        raise PermissionError(
            "Writing to compose files is disabled. Set DOCKYARD_ALLOW_COMPOSE_WRITE=true "
            "and mount the compose directory read-write to enable this."
        )

    if not include_dockyard and not include_oci:
        raise ValueError("Select at least one label set to write (Dockyard and/or OCI).")

    if not os.path.isfile(compose_file):
        raise FileNotFoundError(f"Compose file not found on disk: {compose_file}")

    with open(compose_file, "r") as f:
        data = yaml.load(f)

    services = (data or {}).get("services") or {}
    if service_name not in services:
        raise KeyError(f"Service '{service_name}' not found in {compose_file}")

    service = services[service_name]
    new_labels = _build_labels(summary, tags, docs_url, include_dockyard, include_oci)
    if not new_labels:
        raise ValueError("Nothing to write - add a summary or tags first.")

    existing = service.get("labels")

    if existing is None:
        service["labels"] = new_labels
    elif isinstance(existing, dict):
        existing.update(new_labels)
    elif isinstance(existing, list):
        # list form: "key=value" strings. Drop any prior dockyard.* entries,
        # then append the fresh ones, leaving everything else untouched.
        kept = [
            item for item in existing
            if not (isinstance(item, str) and item.split("=", 1)[0].strip() in new_labels)
        ]
        for k, v in new_labels.items():
            kept.append(f"{k}={v}")
        service["labels"] = kept
    else:
        raise TypeError(f"Unrecognized 'labels' format for service '{service_name}' in {compose_file}")

    # Back up the original once per session-worth of edits, then write atomically.
    backup_path = compose_file + ".dockyard-bak"
    if not os.path.exists(backup_path):
        shutil.copy2(compose_file, backup_path)

    tmp_path = compose_file + ".dockyard-tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(data, f)
    os.replace(tmp_path, compose_file)
