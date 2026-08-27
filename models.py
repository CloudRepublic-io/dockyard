from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base


def now_utc():
    return datetime.now(timezone.utc)


class Container(Base):
    """Facts pulled live from the Docker socket (docker inspect).

    `name` is the primary key. For the local host it stays exactly the bare
    container name (e.g. "plex") for full backward compatibility with
    single-host installs. For any other configured host, it's prefixed with
    the host name (e.g. "media-server::plex") so containers with the same
    name on different servers never collide. `display_name` is always just
    the bare container name, for showing in the UI regardless of host."""
    __tablename__ = "containers"

    name = Column(String, primary_key=True)
    display_name = Column(String)                     # bare container name, for display/URLs-free-of-host-prefix
    host = Column(String, default="local")            # which configured host this came from
    status = Column(String)                          # running / exited / paused ...
    image = Column(String)
    created = Column(String)                         # image creation timestamp (raw)
    ports_json = Column(Text, default="[]")           # JSON list of "host:container/proto"
    exposed_ports_json = Column(Text, default="[]")   # JSON list of image-declared EXPOSEd ports (e.g. "80/tcp") -
                                                       # used as a launch-target fallback for host-network containers,
                                                       # which have no formal port-publish record at all
    volumes_json = Column(Text, default="[]")         # JSON list of "source:dest:mode"
    env_keys_json = Column(Text, default="[]")        # JSON list of env var NAMES only
    networks_json = Column(Text, default="[]")        # JSON list of network names
    network_ips_json = Column(Text, default="{}")     # JSON dict {network_name: ip_address}
    network_mode = Column(String, nullable=True)      # bridge / host / none / container:<id> ...
    labels_json = Column(Text, default="{}")          # JSON dict of all container labels
    compose_project = Column(String, nullable=True)   # com.docker.compose.project label
    compose_service = Column(String, nullable=True)   # com.docker.compose.service label
    first_seen = Column(DateTime, default=now_utc)    # set once, never touched on update
    last_synced = Column(DateTime, default=now_utc, onupdate=now_utc)
    archived_at = Column(DateTime, nullable=True)     # set when a documented container vanishes from Docker;
                                                       # cleared again if it reappears. NULL = still live.

    note = relationship("Note", back_populates="container", uselist=False,
                         cascade="all, delete-orphan")


class ComposeService(Base):
    """Facts parsed from docker-compose.yml files on disk (may not be running).
    `id` includes the host name going forward so services on different hosts
    never collide; existing single-host ids just self-heal on next sync since
    stale-format rows get deleted as "no longer seen"."""
    __tablename__ = "compose_services"

    id = Column(String, primary_key=True)   # f"{host}::{compose_file}::{service_name}"
    host = Column(String, default="local")
    compose_file = Column(String)
    service_name = Column(String)
    image = Column(String, nullable=True)
    ports_json = Column(Text, default="[]")
    volumes_json = Column(Text, default="[]")
    env_keys_json = Column(Text, default="[]")
    depends_on_json = Column(Text, default="[]")
    networks_json = Column(Text, default="[]")
    last_synced = Column(DateTime, default=now_utc, onupdate=now_utc)


class Note(Base):
    """Manually-written documentation layered on top of the auto-pulled facts."""
    __tablename__ = "notes"

    container_name = Column(String, ForeignKey("containers.name"), primary_key=True)
    summary = Column(Text, default="")          # one-line "what is this" headline
    description = Column(Text, default="")      # markdown - longer explanation of what it is/why it exists
    usage_notes = Column(Text, default="")      # markdown - how to run/use it, blessed commands
    debug_notes = Column(Text, default="")      # markdown - how to debug / test / logs
    backup_notes = Column(Text, default="")     # markdown - backup & restore steps
    links = Column(Text, default="")            # markdown - links to upstream docs etc
    tags = Column(String, default="")           # comma-separated
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    container = relationship("Container", back_populates="note")
