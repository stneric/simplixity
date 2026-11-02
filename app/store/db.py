"""SQLite persistence: connection, schema, and additive migrations.

Chats and messages are durable here; Redis holds only live run state and can be
lost without losing history.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

# The profile everything belongs to when nobody has picked one, and the owner of
# every row written before profiles existed. See `api.deps.current_user`.
DEFAULT_USER = "default"

TABLES = """
CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sources    TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL UNIQUE,
    run_id     TEXT,
    query      TEXT,
    answer     TEXT,
    blocks     TEXT,
    status     TEXT,
    depth      TEXT,
    model      TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Who is using the install. No password is a NULL hash, and a profile with
-- none opens with a click. See store/profiles.py for why this is not an
-- account system and does not try to be.
CREATE TABLE IF NOT EXISTS profiles (
    id            TEXT PRIMARY KEY,
    password_hash TEXT,
    created_at    TEXT NOT NULL
);

-- A project is a folder for chats and the corpus made out of them: ask a
-- question inside one and the answers already written there are searched
-- alongside the web, and cited the same way.
CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- A reading list being built: the conversation that set it up, the brief that
-- came out of it, and a log of every search round. Optionally filed under a
-- project, without a foreign key for the same reason chats have none: deleting
-- a project must never take the work with it. See store/literature.py.
--
-- A row with `chat_id` set is not a reading list but the files attached to
-- that chat (store/attachments.py): it has no papers, no brief and no rounds,
-- and the literature page never lists it. Kept in this table so an attached
-- PDF is stored, cut up, embedded, searched and opened in the reader by
-- exactly the code that does it for a paper's full text.
CREATE TABLE IF NOT EXISTS lit_collections (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    project_id TEXT,
    name       TEXT NOT NULL,
    brief      TEXT NOT NULL DEFAULT '{}',
    talk       TEXT NOT NULL DEFAULT '[]',
    rounds     TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Every paper a collection has ever been shown, decided or not. Excluded ones
-- stay: they are what keeps a later round from offering them again, and what
-- "not like these" is computed from.
CREATE TABLE IF NOT EXISTS lit_papers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id TEXT NOT NULL REFERENCES lit_collections(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    data          TEXT NOT NULL,
    summary       TEXT,
    bibtex        TEXT,
    decision      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    round         INTEGER NOT NULL DEFAULT 1,
    origin        TEXT NOT NULL DEFAULT 'search',
    score         REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    UNIQUE (collection_id, key)
);

-- The full texts behind a reading list. The PDF itself is kept in the database
-- rather than beside it, so the one file that is backed up is the whole of the
-- library: a PDF that survives on disk without its row, or a row whose file
-- went missing, is the inconsistency this avoids. Keyed by content hash, so a
-- PDF uploaded twice is stored once. See store/library.py.
CREATE TABLE IF NOT EXISTS lit_blobs (
    sha256 TEXT PRIMARY KEY,
    data   BLOB NOT NULL
);
-- One uploaded PDF. `paper_id` is NULL while it is unassigned: an upload that
-- could not be matched to a paper with certainty waits for the person rather
-- than being filed under a guess. `match` says how it was assigned.
CREATE TABLE IF NOT EXISTS lit_files (
    id            TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES lit_collections(id) ON DELETE CASCADE,
    paper_id      INTEGER REFERENCES lit_papers(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    pages         INTEGER NOT NULL DEFAULT 0,
    text_pages    INTEGER NOT NULL DEFAULT 0,
    match         TEXT NOT NULL DEFAULT '',
    warnings      TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    UNIQUE (collection_id, sha256)
);
-- The extracted text, page by page, so every passage knows the page it is on.
CREATE TABLE IF NOT EXISTS lit_pages (
    file_id TEXT NOT NULL REFERENCES lit_files(id) ON DELETE CASCADE,
    page    INTEGER NOT NULL,
    text    TEXT NOT NULL,
    PRIMARY KEY (file_id, page)
);
-- What is searched. The vector is stored with the name of the model that made
-- it: vectors from two models live in different spaces, and comparing them
-- would rank by noise while looking like it worked.
CREATE TABLE IF NOT EXISTS lit_chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL REFERENCES lit_files(id) ON DELETE CASCADE,
    page    INTEGER NOT NULL,
    text    TEXT NOT NULL,
    vector  BLOB,
    model   TEXT
);
"""

# Applied *after* the migrations: an index over a column that a migration is
# about to add cannot be created before it exists, and on an upgraded install
# that ordering is the difference between booting and not.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_run  ON messages(run_id);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chats_user    ON chats(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_lit_user      ON lit_collections(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_lit_project   ON lit_collections(project_id);
CREATE INDEX IF NOT EXISTS idx_lit_chat      ON lit_collections(chat_id);
CREATE INDEX IF NOT EXISTS idx_lit_papers    ON lit_papers(collection_id, decision);
CREATE INDEX IF NOT EXISTS idx_lit_files     ON lit_files(collection_id, paper_id);
CREATE INDEX IF NOT EXISTS idx_lit_chunks    ON lit_chunks(file_id, page);
"""

# Every profile that owns something gets a row, so history written before the
# profiles table existed (all of it the default profile's) shows up in the
# picker instead of being stranded behind a name nobody can choose.
BACKFILL_PROFILES = """
INSERT OR IGNORE INTO profiles (id, created_at)
    SELECT user_id, '' FROM chats
    UNION SELECT user_id, '' FROM projects
    UNION SELECT user_id, '' FROM lit_collections
    UNION SELECT substr(key, 4), '' FROM settings WHERE key LIKE 'ui:%';
"""

# Columns added after a release shipped. `CREATE TABLE IF NOT EXISTS` silently
# skips an existing table, so without this an upgraded install keeps the old
# shape and fails on first write.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("chats", "updated_at", "ALTER TABLE chats ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"),
    ("messages", "answer", "ALTER TABLE messages ADD COLUMN answer TEXT"),
    ("messages", "depth", "ALTER TABLE messages ADD COLUMN depth TEXT"),
    ("messages", "model", "ALTER TABLE messages ADD COLUMN model TEXT"),
    # When the answer was written, shown beside it. NULL for answers stored
    # before it was recorded, and for runs that never finished.
    ("messages", "finished_at", "ALTER TABLE messages ADD COLUMN finished_at TEXT"),
    # No foreign key: SQLite cannot add one with ALTER TABLE, and the reference
    # would have to be `ON DELETE SET NULL` anyway: deleting a project must
    # never take the chats with it. `delete_project` clears the column itself.
    ("chats", "project_id", "ALTER TABLE chats ADD COLUMN project_id TEXT"),
    # Profiles. The default hands every existing row to the default profile, so
    # an upgraded install opens on exactly the history it had before.
    (
        "chats",
        "user_id",
        f"ALTER TABLE chats ADD COLUMN user_id TEXT NOT NULL DEFAULT '{DEFAULT_USER}'",
    ),
    (
        "projects",
        "user_id",
        f"ALTER TABLE projects ADD COLUMN user_id TEXT NOT NULL DEFAULT '{DEFAULT_USER}'",
    ),
    # Files attached to a chat (store/attachments.py). NULL for a reading list.
    ("lit_collections", "chat_id", "ALTER TABLE lit_collections ADD COLUMN chat_id TEXT"),
    # What an uploaded file is: a PDF, or text read out of Word, Markdown or a
    # plain text file. Every file before attachments was a PDF.
    ("lit_files", "kind", "ALTER TABLE lit_files ADD COLUMN kind TEXT NOT NULL DEFAULT 'pdf'"),
]


async def connect(database_path: str) -> aiosqlite.Connection:
    _ensure_writable(Path(database_path).parent)
    conn = await aiosqlite.connect(database_path)
    try:
        conn.row_factory = aiosqlite.Row
        # WAL keeps the SSE-heavy read path from blocking on the writer.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        # Concurrent runs share one connection; a brief wait beats an immediate
        # "database is locked".
        await conn.execute("PRAGMA busy_timeout=5000")

        await conn.executescript(TABLES)
        await _migrate(conn)
        await conn.executescript(INDEXES)
        await conn.executescript(BACKFILL_PROFILES)
        await conn.commit()
    except Exception:
        # aiosqlite runs a worker thread per connection; leaving one open on a
        # failed startup hangs the process instead of reporting the error.
        await conn.close()
        raise
    return conn


def _ensure_writable(directory: Path) -> None:
    """Fail with an explanation rather than sqlite3's "unable to open database file".

    On a bind-mounted volume the directory arrives owned by root while the app
    runs as uid 1000, and that one line of context is the difference between a
    one-command fix and an afternoon.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot create the data directory {directory}. "
            f"If it is a mounted volume, give it to uid 1000: chown -R 1000:1000 <host path>."
        ) from exc

    if not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"The data directory {directory} is not writable by uid {os.getuid()}. "
            f"If it is a mounted volume, run: chown -R 1000:1000 <host path>, "
            f"or set DATABASE_PATH to somewhere writable."
        )


async def _migrate(conn: aiosqlite.Connection) -> None:
    for table, column, statement in MIGRATIONS:
        if await _has_column(conn, table, column):
            continue
        log.info("migrating: adding %s.%s", table, column)
        await conn.execute(statement)


async def _has_column(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        return any(row["name"] == column for row in await cursor.fetchall())
