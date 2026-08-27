import os
import json
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, case
from sqlalchemy.orm import Session
from pydantic import BaseModel
import markdown as md

from database import init_db, get_db
from models import Container, ComposeService, Note, now_utc
from sync import run_full_sync, ingest_compose_files
from label_specs import extract_publisher_docs
from traefik_labels import extract_traefik_routers, traefik_enabled, build_traefik_url
from hosts import load_hosts_config, host_launch_address
from export import generate_markdown_export, collect_container_records
from compose_writer import write_labels_to_compose, compose_write_enabled
from events import start_event_listener, stop_event_listener
from auth import require_auth, require_ingest_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dockyard")

SYNC_INTERVAL_SECONDS = int(os.environ.get("DOCKYARD_SYNC_INTERVAL", "300"))
NEW_WINDOW = timedelta(hours=24)


async def _background_sync_loop():
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(run_full_sync)
        except Exception:
            logger.exception("Background sync failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        run_full_sync()
    except Exception:
        logger.exception("Initial sync failed")

    task = asyncio.create_task(_background_sync_loop())
    start_event_listener()  # live updates: new/stopped containers show up immediately
    yield
    task.cancel()
    stop_event_listener()


app = FastAPI(title="Dockyard", lifespan=lifespan, dependencies=[Depends(require_auth)])
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def is_new(container: Container) -> bool:
    if not container.first_seen:
        return False
    ts = container.first_seen
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) < NEW_WINDOW


def needs_docs(container: Container) -> bool:
    return container.note is None or not (container.note.summary or container.note.description
                                           or container.note.usage_notes)


def render_md(text: str) -> str:
    return md.markdown(text or "", extensions=["fenced_code", "tables"])


templates.env.filters["markdown"] = render_md


def render_guide_markdown(text: str):
    """Renders the guide's markdown with heading anchors and returns
    (content_html, toc_html) - toc_html links to h2 sections only ("main
    headings"), since h1 is just the title and h3s are sub-details."""
    converter = md.Markdown(
        extensions=["fenced_code", "tables", "toc"],
        extension_configs={"toc": {"toc_depth": "2"}},
    )
    html = converter.convert(text or "")
    return html, converter.toc


def find_matching_compose_service(db: Session, container: Container):
    """Best-effort match, scoped to the same host. Prefers the EXACT compose
    file Docker itself recorded the container as having been created from
    (the com.docker.compose.project.config_files label), matched by filename
    rather than full path since Dockyard's view of that path (via its own
    mount) often has a different prefix than the path Docker recorded on the
    original host. This disambiguates cases where more than one file in a
    scanned directory happens to define a service with the same name (e.g. a
    real docker-compose.yml alongside a docker-compose.yml.example). Falls
    back to a plain service-name match if that label isn't present or
    doesn't match anything found on disk."""
    labels = json.loads(container.labels_json or "{}")
    config_files = labels.get("com.docker.compose.project.config_files", "")
    candidate_basenames = {
        os.path.basename(p.strip()) for p in config_files.split(",") if p.strip()
    }

    if candidate_basenames and container.compose_service:
        rows = (
            db.query(ComposeService)
            .filter(ComposeService.service_name == container.compose_service,
                    ComposeService.host == container.host)
            .all()
        )
        for row in rows:
            if os.path.basename(row.compose_file) in candidate_basenames:
                return row

    if container.compose_service:
        row = (
            db.query(ComposeService)
            .filter(ComposeService.service_name == container.compose_service,
                    ComposeService.host == container.host)
            .first()
        )
        if row:
            return row
    return (
        db.query(ComposeService)
        .filter(ComposeService.service_name == (container.display_name or container.name),
                ComposeService.host == container.host)
        .first()
    )


HOST_COLOR_PALETTE = ["#7dd3fc", "#34d399", "#c4b5fd", "#fbbf24", "#f472b6", "#38bdf8"]


def build_host_colors(db: Session) -> dict:
    """Assigns each host a consistent color from a small palette, cycling if
    there are more hosts than colors. Used for the group-header dots on the
    dashboard and the host chip on the detail page."""
    all_hosts = sorted({
        row[0] for row in db.query(Container.host).distinct()
        if row[0]
    })
    return {h: HOST_COLOR_PALETTE[i % len(HOST_COLOR_PALETTE)] for i, h in enumerate(all_hosts)}


# A host is flagged stale if its most recently synced container is older
# than this - generous enough to absorb normal timing jitter around the
# periodic sync interval, but still catch a genuinely broken connection
# (e.g. a docker-socket-proxy that's become unreachable) well before it's
# been silently stale for hours.
STALE_THRESHOLD_SECONDS = max(SYNC_INTERVAL_SECONDS * 3, 900)


def build_host_sync_status(db: Session) -> dict:
    """Maps each host name to {'last_synced': datetime|None, 'stale': bool}.
    A broken Docker API connection to a host (wrong docker-socket-proxy
    binding, firewall, host down, etc.) means that host's containers just
    silently stop getting their live facts refreshed - this surfaces that in
    the UI instead of it only being visible by noticing warnings in the
    server logs."""
    rows = (
        db.query(Container.host, func.max(Container.last_synced))
        .filter(Container.archived_at.is_(None))
        .group_by(Container.host)
        .all()
    )
    # SQLite round-trips DateTime columns as naive datetimes even though they
    # were originally written via now_utc() (tz-aware) - strip tzinfo here so
    # the subtraction below doesn't raise on the mismatch.
    now = now_utc().replace(tzinfo=None)
    status = {}
    for host, last_synced in rows:
        if not host:
            continue
        stale = last_synced is None or (now - last_synced).total_seconds() > STALE_THRESHOLD_SECONDS
        status[host] = {"last_synced": last_synced, "stale": stale}
    return status


def build_host_addresses() -> dict:
    """Maps each configured host's name to a reachable IP/hostname derived
    from its docker_url, or None if there isn't one (the local host, or any
    host without a resolvable address) - callers fall back to the browser's
    own current hostname in that case."""
    return {h["name"]: host_launch_address(h) for h in load_hosts_config()}


_PUBLISHED_PORT_RE = re.compile(r"^([^:]+):(\d+) -> \d+/(\w+)")


def compute_launch_target(container: Container, host_addresses: dict):
    """Returns {'url': str} for a single fully-resolved absolute URL,
    {'port': str} for a single published port that needs the browser's own
    current hostname filled in client-side, {'options': [...]} when there's
    more than one option and no Traefik route to disambiguate which one is
    the actual web UI, or None if there's nothing to launch. A working
    Traefik route always takes priority over everything else. Only offered
    for containers that are actually running - a stopped container has
    nothing listening on any of its ports or routes, however they're
    configured."""
    if container.status != "running":
        return None

    labels = json.loads(container.labels_json or "{}")
    for router in extract_traefik_routers(labels):
        url = build_traefik_url(router)
        if url:
            return {"url": url}

    candidates = []
    seen_host_ports = set()
    for p in json.loads(container.ports_json or "[]"):
        m = _PUBLISHED_PORT_RE.match(p)
        if not m or m.group(3).lower() != "tcp":
            continue
        host_ip, host_port = m.group(1), m.group(2)
        if host_port in seen_host_ports:
            continue
        seen_host_ports.add(host_port)
        if host_ip not in ("0.0.0.0", "::"):
            candidates.append({"label": f"Port {host_port}", "url": f"http://{host_ip}:{host_port}"})
            continue
        addr = host_addresses.get(container.host)
        if addr:
            candidates.append({"label": f"Port {host_port}", "url": f"http://{addr}:{host_port}"})
        else:
            candidates.append({"label": f"Port {host_port}", "port": host_port})

    # Direct container IP on any custom (non-default-bridge) network - the
    # correct way to reach containers on macvlan/ipvlan networks, where the
    # container gets a real address of its own on the LAN. This matters
    # because `docker run -p` on such a network is often recorded by Docker
    # without actually creating a working host-side forward at all, so a
    # "published port" candidate above can exist and still not work - this
    # is offered alongside it, not instead of it, so the user can pick
    # whichever one actually responds.
    exposed = sorted({
        ep.split("/", 1)[0] for ep in json.loads(container.exposed_ports_json or "[]")
        if ep.split("/", 1)[0].isdigit() and ep.split("/", 1)[-1].lower() == "tcp"
    })
    network_ips = json.loads(container.network_ips_json or "{}")
    seen_ip_ports = set()
    for net_name, ip in network_ips.items():
        if net_name == "bridge" or not ip:
            continue
        for port_num in exposed:
            key = f"{ip}:{port_num}"
            if key in seen_ip_ports:
                continue
            seen_ip_ports.add(key)
            candidates.append({"label": key, "url": f"http://{ip}:{port_num}"})

    if not candidates and container.network_mode == "host":
        # Host-network containers have no formal port-publish record at all
        # (docker inspect's port-mapping table is always empty for them) -
        # but since they share the host's network stack directly, whatever
        # the image declares via EXPOSE genuinely is reachable at the host's
        # own address, resolved the same way a wildcard-bound (0.0.0.0)
        # published port already is above.
        addr = host_addresses.get(container.host)
        for port_num in exposed:
            if addr:
                candidates.append({"label": f"Port {port_num}", "url": f"http://{addr}:{port_num}"})
            else:
                candidates.append({"label": f"Port {port_num}", "port": port_num})

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return {"options": candidates}


def filter_containers(db: Session, q: str, statuses: list, stacks: list, host: str, flags: list):
    query = db.query(Container).filter(Container.archived_at.is_(None))
    if statuses:
        query = query.filter(Container.status.in_(statuses))
    if stacks:
        query = query.filter(Container.compose_project.in_(stacks))
    if host:
        query = query.filter(Container.host == host)
    containers = query.order_by(
        case((Container.host == "local", 0), else_=1),
        Container.host,
        func.lower(Container.display_name),
    ).all()

    if q:
        needle = q.lower()
        containers = [
            c for c in containers
            if needle in (c.display_name or c.name).lower()
            or needle in (c.image or "").lower()
            or needle in (c.host or "").lower()
            or (c.note and needle in (c.note.tags or "").lower())
        ]

    if flags:
        want_new = "new" in flags
        want_needs_docs = "needs_docs" in flags

        def matches_flags(c):
            checks = []
            if want_new:
                checks.append(is_new(c))
            if want_needs_docs:
                checks.append(needs_docs(c))
            return any(checks)

        containers = [c for c in containers if matches_flags(c)]

    return containers


@app.get("/")
def index(request: Request, q: str = "", status: list = Query(default=[]), stack: list = Query(default=[]),
          host: str = "", flag: list = Query(default=[]), db: Session = Depends(get_db)):
    containers = filter_containers(db, q, status, stack, host, flag)

    all_stacks = sorted({
        row[0] for row in db.query(Container.compose_project).distinct()
        if row[0]
    })
    all_hosts = sorted({
        row[0] for row in db.query(Container.host).distinct()
        if row[0]
    })
    multi_host = len(all_hosts) > 1
    host_colors = build_host_colors(db)
    host_addresses = build_host_addresses()
    host_sync_status = build_host_sync_status(db)

    flags = {c.name: {"is_new": is_new(c), "needs_docs": needs_docs(c)} for c in containers}
    launch_targets = {c.name: compute_launch_target(c, host_addresses) for c in containers}

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request, "containers": containers, "q": q, "status": status,
            "stack": stack, "all_stacks": all_stacks, "host": host, "all_hosts": all_hosts,
            "flag": flag, "multi_host": multi_host, "flags": flags, "host_colors": host_colors,
            "launch_targets": launch_targets, "host_sync_status": host_sync_status,
        },
    )


@app.get("/partials/containers")
def containers_partial(request: Request, q: str = "", status: list = Query(default=[]), stack: list = Query(default=[]),
                        host: str = "", flag: list = Query(default=[]), db: Session = Depends(get_db)):
    """Returns just the container grid's inner HTML, for the live-filtering
    AJAX search/pills/stacks/flags/host-select on the index page."""
    containers = filter_containers(db, q, status, stack, host, flag)
    flags = {c.name: {"is_new": is_new(c), "needs_docs": needs_docs(c)} for c in containers}
    all_hosts_count = db.query(Container.host).distinct().count()
    host_colors = build_host_colors(db)
    host_addresses = build_host_addresses()
    host_sync_status = build_host_sync_status(db)
    launch_targets = {c.name: compute_launch_target(c, host_addresses) for c in containers}
    return templates.TemplateResponse(
        "_container_grid.html",
        {"request": request, "containers": containers, "flags": flags, "multi_host": all_hosts_count > 1,
         "host_colors": host_colors, "launch_targets": launch_targets, "host_sync_status": host_sync_status},
    )


@app.post("/sync")
def trigger_sync():
    try:
        run_full_sync()
    except Exception:
        logger.exception("Sync failed unexpectedly - see the traceback above for details")
    return RedirectResponse(url="/", status_code=303)


README_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")


@app.get("/guide")
def guide(request: Request):
    """Renders Dockyard's own README.md as an in-app page, so once one
    instance is running, everything needed to set up multi-host, the
    compose-push agent, write-back, auth, etc. is available without leaving
    the app. (Named /guide, not /docs, since FastAPI already uses /docs for
    its built-in Swagger UI.)"""
    try:
        with open(README_PATH, "r") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = "# Documentation unavailable\n\nREADME.md wasn't found alongside the app."
    content_html, toc_html = render_guide_markdown(raw)
    return templates.TemplateResponse(
        "docs.html", {"request": request, "content": content_html, "toc": toc_html}
    )


@app.get("/archive")
def archive(request: Request, db: Session = Depends(get_db)):
    """Containers that were documented but no longer exist on their host -
    moved, renamed, or removed. Their notes are preserved here rather than
    deleted, since the information might still be useful."""
    containers = (
        db.query(Container)
        .filter(Container.archived_at.isnot(None))
        .order_by(Container.archived_at.desc())
        .all()
    )
    multi_host = db.query(Container.host).distinct().count() > 1
    host_colors = build_host_colors(db)
    return templates.TemplateResponse(
        "archive.html", {"request": request, "containers": containers, "multi_host": multi_host,
                          "host_colors": host_colors}
    )


@app.post("/container/{name}/delete")
def delete_container(name: str, db: Session = Depends(get_db)):
    """Permanently removes an archived container and its notes. Refuses to
    touch anything still live, so this can't be used to accidentally wipe a
    running container's documentation."""
    container = db.get(Container, name)
    if container is not None and container.archived_at is not None:
        db.delete(container)
        db.commit()
    return RedirectResponse(url="/archive", status_code=303)


@app.get("/export")
def export_markdown(db: Session = Depends(get_db)):
    body = generate_markdown_export(db)
    return Response(
        content=body,
        media_type="text/markdown",
        headers={"Content-Disposition": "attachment; filename=dockyard-export.md"},
    )


@app.get("/api/containers")
def api_containers(host: str = "", db: Session = Depends(get_db)):
    """Same underlying data as /export, as JSON - for scripts, dashboards, or
    other tools to consume. Optionally filter to one host."""
    records = collect_container_records(db)
    if host:
        records = [r for r in records if r["host"] == host]
    return records


@app.get("/api/containers/{name}")
def api_container_detail(name: str, host: str = "", db: Session = Depends(get_db)):
    """Looks up by display name. If multiple hosts have a same-named
    container, pass ?host=<name> to disambiguate - otherwise all matches
    are returned as a list."""
    records = collect_container_records(db)
    matches = [r for r in records if r["name"] == name and (not host or r["host"] == host)]
    if not matches:
        return Response(content=json.dumps({"error": f"No container named '{name}'"}),
                         media_type="application/json", status_code=404)
    if len(matches) == 1:
        return matches[0]
    return matches


class ComposeFilePayload(BaseModel):
    path: str
    content: str


class ComposeIngestPayload(BaseModel):
    files: list[ComposeFilePayload]


@app.post("/api/hosts/{host_name}/compose", dependencies=[Depends(require_ingest_token)])
def ingest_compose(host_name: str, payload: ComposeIngestPayload):
    """Accepts pushed compose file content from a remote host's agent (see
    agent/push_compose.py) - for hosts whose compose directory isn't reachable
    from Dockyard's own filesystem. Requires DOCKYARD_INGEST_TOKEN to be set
    and sent as an 'Authorization: Bearer <token>' header."""
    n = ingest_compose_files(host_name, [f.model_dump() for f in payload.files])
    return {"host": host_name, "services_synced": n}


@app.get("/container/{name}")
def detail(name: str, request: Request, db: Session = Depends(get_db),
           wrote: str = "", write_error: str = ""):
    container = db.get(Container, name)
    if container is None:
        return templates.TemplateResponse(
            "missing.html", {"request": request, "name": name}, status_code=404
        )

    compose_svc = find_matching_compose_service(db, container)
    compose_file_reachable = compose_svc is not None and os.path.isfile(compose_svc.compose_file)
    multi_host = db.query(Container.host).distinct().count() > 1
    host_colors = build_host_colors(db)
    launch_target = compute_launch_target(container, build_host_addresses())
    host_stale = build_host_sync_status(db).get(container.host, {}).get("stale", False)

    network_names = json.loads(container.networks_json or "[]")
    network_ips = json.loads(container.network_ips_json or "{}")
    networks_display = [
        {"name": n, "ip": network_ips.get(n)} for n in network_names
    ]

    ctx = {
        "request": request,
        "c": container,
        "multi_host": multi_host,
        "host_colors": host_colors,
        "launch_target": launch_target,
        "host_stale": host_stale,
        "ports": json.loads(container.ports_json or "[]"),
        "volumes": json.loads(container.volumes_json or "[]"),
        "env_keys": json.loads(container.env_keys_json or "[]"),
        "networks": networks_display,
        "network_mode": container.network_mode,
        "labels": json.loads(container.labels_json or "{}"),
        "publisher_docs": extract_publisher_docs(json.loads(container.labels_json or "{}")),
        "traefik_routers": extract_traefik_routers(json.loads(container.labels_json or "{}")),
        "traefik_enabled": traefik_enabled(json.loads(container.labels_json or "{}")),
        "compose_svc": compose_svc,
        "compose_depends_on": json.loads(compose_svc.depends_on_json) if compose_svc else [],
        "compose_ports": json.loads(compose_svc.ports_json) if compose_svc else [],
        "compose_volumes": json.loads(compose_svc.volumes_json) if compose_svc else [],
        "note": container.note,
        "is_new": is_new(container),
        "needs_docs": needs_docs(container),
        "is_archived": container.archived_at is not None,
        "compose_write_enabled": compose_write_enabled(),
        "compose_file_reachable": compose_file_reachable,
        "wrote": wrote,
        "write_error": write_error,
    }
    return templates.TemplateResponse("detail.html", ctx)


@app.post("/container/{name}/write-labels")
def write_labels(name: str, request: Request, db: Session = Depends(get_db),
                  write_dockyard: str = Form(""), write_oci: str = Form("")):
    container = db.get(Container, name)
    if container is None:
        return RedirectResponse(url="/", status_code=303)

    compose_svc = find_matching_compose_service(db, container)
    if compose_svc is None:
        msg = quote("No matching compose file/service found for this container.")
        return RedirectResponse(url=f"/container/{name}?write_error={msg}", status_code=303)

    note = container.note
    docs_url = str(request.base_url).rstrip("/") + f"/container/{name}"

    try:
        write_labels_to_compose(
            compose_svc.compose_file,
            compose_svc.service_name,
            summary=note.summary if note else "",
            tags=note.tags if note else "",
            docs_url=docs_url,
            include_dockyard=bool(write_dockyard),
            include_oci=bool(write_oci),
        )
    except Exception as e:
        return RedirectResponse(url=f"/container/{name}?write_error={quote(str(e))}", status_code=303)

    return RedirectResponse(url=f"/container/{name}?wrote=1", status_code=303)


@app.get("/container/{name}/edit")
def edit_form(name: str, request: Request, db: Session = Depends(get_db),
              from_page: str = Query("detail", alias="from"), list_query: str = ""):
    container = db.get(Container, name)
    if container is None:
        return templates.TemplateResponse(
            "missing.html", {"request": request, "name": name}, status_code=404
        )
    return templates.TemplateResponse("edit.html", {
        "request": request, "c": container, "note": container.note,
        "from_page": from_page, "list_query": list_query,
    })


@app.post("/container/{name}/edit")
def save_note(
    name: str,
    db: Session = Depends(get_db),
    summary: str = Form(""),
    description: str = Form(""),
    usage_notes: str = Form(""),
    debug_notes: str = Form(""),
    backup_notes: str = Form(""),
    links: str = Form(""),
    tags: str = Form(""),
    from_page: str = Form("detail"),
    list_query: str = Form(""),
):
    container = db.get(Container, name)
    if container is None:
        return RedirectResponse(url="/", status_code=303)

    note = container.note
    if note is None:
        note = Note(container_name=name)
        db.add(note)

    note.summary = summary
    note.description = description
    note.usage_notes = usage_notes
    note.debug_notes = debug_notes
    note.backup_notes = backup_notes
    note.links = links
    note.tags = tags

    db.commit()

    if from_page == "list":
        target = "/" + (f"?{list_query}" if list_query else "")
    else:
        target = f"/container/{name}"
    return RedirectResponse(url=target, status_code=303)
