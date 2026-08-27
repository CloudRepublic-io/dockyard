import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import Container
from label_specs import extract_publisher_docs
from traefik_labels import extract_traefik_routers


def collect_container_records(db: Session) -> list:
    """Canonical, structured view of everything Dockyard knows about every
    container: auto-pulled facts, publisher/Traefik labels, and your notes.
    This is the single source of truth behind both the Markdown export and
    the JSON API - both are just different renderings of this same data."""
    containers = db.query(Container).order_by(Container.name).all()
    records = []

    for c in containers:
        labels = json.loads(c.labels_json or "{}")
        note = c.note

        records.append({
            "name": c.display_name or c.name,
            "host": c.host or "local",
            "status": c.status,
            "image": c.image,
            "stack": {
                "project": c.compose_project,
                "service": c.compose_service,
            } if c.compose_project else None,
            "runtime": {
                "ports": json.loads(c.ports_json or "[]"),
                "volumes": json.loads(c.volumes_json or "[]"),
                "networks": json.loads(c.networks_json or "[]"),
                "network_ips": json.loads(c.network_ips_json or "{}"),
                "network_mode": c.network_mode,
                "env_keys": json.loads(c.env_keys_json or "[]"),
            },
            "publisher_docs": extract_publisher_docs(labels),
            "traefik_routers": extract_traefik_routers(labels),
            "notes": {
                "summary": note.summary or "",
                "description": note.description or "",
                "usage_notes": note.usage_notes or "",
                "debug_notes": note.debug_notes or "",
                "backup_notes": note.backup_notes or "",
                "links": note.links or "",
                "tags": [t.strip() for t in (note.tags or "").split(",") if t.strip()],
                "updated_at": note.updated_at.isoformat() if note.updated_at else None,
            } if note else None,
            "first_seen": c.first_seen.isoformat() if c.first_seen else None,
            "last_synced": c.last_synced.isoformat() if c.last_synced else None,
        })

    return records


def _bullet_list(items):
    if not items:
        return "_None._\n"
    return "\n".join(f"- `{i}`" for i in items) + "\n"


def generate_markdown_export(db: Session) -> str:
    records = collect_container_records(db)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Dockyard export",
        "",
        f"Generated {generated_at} — {len(records)} container(s).",
        "",
        "---",
        "",
    ]

    for r in records:
        note = r["notes"]

        lines.append(f"## {r['name']}")
        lines.append("")
        lines.append(f"- **Image:** `{r['image']}`")
        lines.append(f"- **Status:** {r['status']}")
        if r["stack"]:
            lines.append(f"- **Stack:** {r['stack']['project']} (service: {r['stack']['service']})")
        lines.append("")

        if note and note["summary"]:
            lines.append(f"> {note['summary']}")
            lines.append("")

        lines.append("### Runtime facts")
        lines.append("")
        lines.append("**Ports**")
        lines.append("")
        lines.append(_bullet_list(r["runtime"]["ports"]))
        lines.append("**Volumes**")
        lines.append("")
        lines.append(_bullet_list(r["runtime"]["volumes"]))
        lines.append("**Networks**")
        lines.append("")
        net_ips = r["runtime"]["network_ips"]
        net_lines = [
            f"{n} ({net_ips[n]})" if n in net_ips else n
            for n in r["runtime"]["networks"]
        ]
        if not net_lines and r["runtime"]["network_mode"] == "host":
            lines.append("_Host networking - shares the host's network stack directly._\n")
        else:
            lines.append(_bullet_list(net_lines))
        lines.append("**Env vars (names only)**")
        lines.append("")
        lines.append(_bullet_list(r["runtime"]["env_keys"]))

        if r["traefik_routers"]:
            lines.append("### Reverse proxy (Traefik)")
            lines.append("")
            for tr in r["traefik_routers"]:
                bits = [f"**{tr['name']}**"]
                if tr["rule"]:
                    bits.append(f"rule: `{tr['rule']}`")
                if tr["entrypoints"]:
                    bits.append(f"entrypoints: `{tr['entrypoints']}`")
                if tr["port"]:
                    bits.append(f"port: `{tr['port']}`")
                if tr["tls"]:
                    bits.append("tls: yes" + (f" ({tr['cert_resolver']})" if tr["cert_resolver"] else ""))
                lines.append("- " + " — ".join(bits))
            lines.append("")

        if r["publisher_docs"]:
            lines.append("### Publisher-provided docs")
            lines.append("")
            for k, v in r["publisher_docs"].items():
                lines.append(f"- **{k}:** {v}")
            lines.append("")

        if note and (note["description"] or note["usage_notes"] or note["debug_notes"] or note["backup_notes"] or note["links"]):
            lines.append("### Notes")
            lines.append("")
            if note["description"]:
                lines.append("**What is this**")
                lines.append("")
                lines.append(note["description"])
                lines.append("")
            if note["usage_notes"]:
                lines.append("**How to run / use it**")
                lines.append("")
                lines.append(note["usage_notes"])
                lines.append("")
            if note["debug_notes"]:
                lines.append("**Debugging & logs**")
                lines.append("")
                lines.append(note["debug_notes"])
                lines.append("")
            if note["backup_notes"]:
                lines.append("**Backup & restore**")
                lines.append("")
                lines.append(note["backup_notes"])
                lines.append("")
            if note["links"]:
                lines.append("**Links**")
                lines.append("")
                lines.append(note["links"])
                lines.append("")
            if note["tags"]:
                lines.append(f"**Tags:** {', '.join(note['tags'])}")
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)
