import time
import logging
import threading

from hosts import load_hosts_config, docker_client_for
from sync import sync_docker_host

logger = logging.getLogger("dockyard.events")

_RELEVANT_ACTIONS = {"start", "die", "create", "destroy", "stop", "pause", "unpause", "rename"}

_stop_flag = threading.Event()
_threads = []


def _listen_forever(host_cfg: dict):
    host_name = host_cfg["name"]
    while not _stop_flag.is_set():
        try:
            client = docker_client_for(host_cfg)
            logger.info("[%s] Docker event listener connected", host_name)
            for event in client.events(decode=True):
                if _stop_flag.is_set():
                    break
                if event.get("Type") != "container":
                    continue
                action = event.get("Action")
                if action not in _RELEVANT_ACTIONS:
                    continue
                attrs = event.get("Actor", {}).get("Attributes", {})
                logger.info("[%s] Docker event '%s' for %s - resyncing", host_name, action, attrs.get("name", "?"))
                try:
                    sync_docker_host(host_cfg)
                except Exception:
                    logger.exception("[%s] Resync after event '%s' failed", host_name, action)
        except Exception as e:
            if _stop_flag.is_set():
                break
            logger.warning("[%s] Docker event listener disconnected (%s), retrying in 5s", host_name, e)
            _stop_flag.wait(5)


def start_event_listener():
    """Starts one listener thread per configured host. Safe to call once at
    app startup."""
    global _threads
    if _threads and any(t.is_alive() for t in _threads):
        return
    _stop_flag.clear()
    _threads = []
    for host_cfg in load_hosts_config():
        t = threading.Thread(target=_listen_forever, args=(host_cfg,),
                              name=f"docker-events-{host_cfg['name']}", daemon=True)
        t.start()
        _threads.append(t)


def stop_event_listener():
    _stop_flag.set()
