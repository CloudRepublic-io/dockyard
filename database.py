import os
import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("dockyard.database")

DATA_DIR = os.environ.get("DOCKYARD_DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "dockyard.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _auto_migrate():
    """Adds any columns the models define that are missing from an existing,
    already-created database (e.g. upgrading from an older version of Dockyard).
    SQLAlchemy's create_all() only creates whole tables that don't exist yet -
    it never alters an existing table - so this fills that gap for simple
    additive changes (new nullable columns), then backfills sensible defaults
    for any pre-existing rows so old single-host data keeps working exactly
    as it did before."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # brand new table, create_all already handled it
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(engine.dialect)
                logger.info("Migrating: adding column %s.%s (%s)", table.name, col.name, col_type)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))

        if inspector.has_table("containers"):
            conn.execute(text("UPDATE containers SET host = 'local' WHERE host IS NULL"))
            conn.execute(text("UPDATE containers SET display_name = name WHERE display_name IS NULL"))
        if inspector.has_table("compose_services"):
            conn.execute(text("UPDATE compose_services SET host = 'local' WHERE host IS NULL"))


def check_writable() -> bool:
    """Fails loudly and specifically if the data directory/database isn't
    actually writable, instead of letting this surface as a confusing generic
    'attempt to write a readonly database' traceback buried inside the first
    sync attempt. Almost always a volume/permissions problem, not a bug in
    Dockyard itself."""
    problems = []

    test_path = os.path.join(DATA_DIR, ".dockyard-write-test")
    try:
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
    except OSError as e:
        problems.append(f"can't create a test file in {DATA_DIR}: {e}")

    if os.path.exists(DB_PATH) and not os.access(DB_PATH, os.W_OK):
        problems.append(f"{DB_PATH} itself is not writable (file permissions/ownership)")

    if problems:
        logger.error(
            "STARTUP CHECK FAILED - the data directory isn't fully writable (%s). Dockyard "
            "will still start, but EVERY sync will fail with 'attempt to write a readonly "
            "database'. This is almost always a volume/permissions problem, not an application "
            "bug - check that the data volume isn't mounted read-only (:ro), that its ownership "
            "matches the user this container runs as, and that the underlying host disk isn't "
            "full or remounted read-only.",
            "; ".join(problems),
        )
        return False
    return True


def init_db():
    import models  # noqa: F401 ensures models are registered before create_all
    Base.metadata.create_all(bind=engine)
    _auto_migrate()
    check_writable()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
