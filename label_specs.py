"""
Known documentation-oriented label namespaces that some images already set:

- OCI annotations: https://github.com/opencontainers/image-spec/blob/master/annotations.md
- Label Schema (legacy but still seen in the wild): http://label-schema.org/rc1/
- dockyard.* - Dockyard's own keys, either hand-added (self-annotation, see
  docker-compose.yml) or written back by the "write to compose file" feature
  (compose_writer.py) - recognized here too, so that loop actually closes.

If a container has any of these, Dockyard surfaces them as "publisher-provided
docs" alongside your own notes, instead of ignoring them.
"""

OCI_KEYS = {
    "org.opencontainers.image.title": "Title",
    "org.opencontainers.image.description": "Description",
    "org.opencontainers.image.documentation": "Documentation",
    "org.opencontainers.image.source": "Source repository",
    "org.opencontainers.image.url": "Project URL",
    "org.opencontainers.image.version": "Version",
}

LABEL_SCHEMA_KEYS = {
    "org.label-schema.usage": "Usage",
    "org.label-schema.docker.cmd": "Run command",
    "org.label-schema.docker.cmd.devel": "Devel run command",
    "org.label-schema.docker.cmd.test": "Test command",
    "org.label-schema.docker.cmd.help": "Help command",
    "org.label-schema.docker.params": "Params",
    "org.label-schema.vcs-url": "Source repository",
    "org.label-schema.url": "Project URL",
}

DOCKYARD_KEYS = {
    "dockyard.summary": "Summary (from labels)",
    "dockyard.tags": "Tags (from labels)",
    "dockyard.docs": "Docs link (from labels)",
}

ALL_KEYS = {**OCI_KEYS, **LABEL_SCHEMA_KEYS, **DOCKYARD_KEYS}


def extract_publisher_docs(labels: dict) -> dict:
    """Return {friendly_name: value} for any recognized documentation label present."""
    if not labels:
        return {}
    found = {}
    for key, friendly_name in ALL_KEYS.items():
        if key in labels and labels[key]:
            found[friendly_name] = labels[key]
    return found
