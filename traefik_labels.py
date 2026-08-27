"""
Traefik configures itself entirely through container labels, so a container's
`docker inspect` labels already contain everything needed to show "how is this
exposed to the world" without touching Traefik itself. This covers the common
docker/docker-compose label provider pattern:

    traefik.enable=true
    traefik.http.routers.<router>.rule=Host(`app.example.com`)
    traefik.http.routers.<router>.entrypoints=websecure
    traefik.http.routers.<router>.tls=true
    traefik.http.routers.<router>.tls.certresolver=letsencrypt
    traefik.http.routers.<router>.middlewares=some-middleware@docker
    traefik.http.routers.<router>.service=<service>
    traefik.http.services.<service>.loadbalancer.server.port=8080

Labels are free-form, so this is a best-effort parse, not a full Traefik config
validator - the goal is "what would I have to go dig up in Traefik's own
dashboard to answer 'how do I reach this?'", surfaced right on the container.
"""

import re

_ROUTER_RULE_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")
_ROUTER_ENTRYPOINTS_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.entrypoints$")
_ROUTER_TLS_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.tls$")
_ROUTER_TLS_RESOLVER_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.tls\.certresolver$")
_ROUTER_SERVICE_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.service$")
_ROUTER_MIDDLEWARES_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.middlewares$")
_SERVICE_PORT_RE = re.compile(r"^traefik\.http\.services\.([^.]+)\.loadbalancer\.server\.port$")

_HOST_RULE_RE = re.compile(r"Host\(`([^`]+)`\)")
_PATH_PREFIX_RE = re.compile(r"PathPrefix\(`([^`]+)`\)")


def build_traefik_url(router: dict):
    """Best-effort reconstruction of the actual browsable URL for a Traefik
    router, from its rule/tls fields. Returns None if the rule doesn't
    contain a Host(...) match (e.g. purely path-based routing with no
    host of its own) - there's no absolute URL to build without one."""
    rule = router.get("rule") or ""
    host_match = _HOST_RULE_RE.search(rule)
    if not host_match:
        return None
    host = host_match.group(1)
    path_match = _PATH_PREFIX_RE.search(rule)
    path = path_match.group(1) if path_match else ""
    scheme = "https" if router.get("tls") else "http"
    return f"{scheme}://{host}{path}"


def traefik_enabled(labels: dict) -> bool:
    return bool(labels) and str(labels.get("traefik.enable", "")).strip().lower() in ("true", "1")


def extract_traefik_routers(labels: dict) -> list:
    """Returns one dict per router: name, rule, entrypoints, tls, cert_resolver,
    middlewares, port. Only returns something if router-level labels exist,
    regardless of whether traefik.enable is explicitly set."""
    if not labels:
        return []

    routers = {}

    def get_router(name):
        return routers.setdefault(name, {
            "name": name, "rule": None, "entrypoints": None,
            "tls": False, "cert_resolver": None, "service": None,
            "middlewares": None, "port": None,
        })

    for key, value in labels.items():
        if not isinstance(key, str):
            continue
        m = _ROUTER_RULE_RE.match(key)
        if m:
            get_router(m.group(1))["rule"] = value
            continue
        m = _ROUTER_ENTRYPOINTS_RE.match(key)
        if m:
            get_router(m.group(1))["entrypoints"] = value
            continue
        m = _ROUTER_TLS_RE.match(key)
        if m:
            get_router(m.group(1))["tls"] = str(value).strip().lower() in ("true", "1")
            continue
        m = _ROUTER_TLS_RESOLVER_RE.match(key)
        if m:
            r = get_router(m.group(1))
            r["tls"] = True
            r["cert_resolver"] = value
            continue
        m = _ROUTER_SERVICE_RE.match(key)
        if m:
            get_router(m.group(1))["service"] = value
            continue
        m = _ROUTER_MIDDLEWARES_RE.match(key)
        if m:
            get_router(m.group(1))["middlewares"] = value
            continue

    service_ports = {}
    for key, value in labels.items():
        if not isinstance(key, str):
            continue
        m = _SERVICE_PORT_RE.match(key)
        if m:
            service_ports[m.group(1)] = value

    for r in routers.values():
        svc_name = r["service"] or r["name"]
        r["port"] = service_ports.get(svc_name)

    return sorted(routers.values(), key=lambda r: r["name"])
