import sqlite3
from storage_copilot import config

def get_db():
    conn = sqlite3.connect(config.LEDGER_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        
        # Check if action_ledger needs migration to support write_file and trash undo
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='action_ledger'")
        row = cur.fetchone()
        if row and "write_file" not in row[0]:
            cur.execute("PRAGMA foreign_keys = OFF;")
            cur.execute("ALTER TABLE action_ledger RENAME TO action_ledger_old;")
            cur.execute("""
            CREATE TABLE action_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                action_type TEXT CHECK(action_type IN ('make_dir', 'move', 'copy', 'trash', 'write_file')) NOT NULL,
                source_path TEXT,
                destination_path TEXT,
                file_size_bytes INTEGER DEFAULT 0,
                sha256_checksum TEXT,
                undo_action_type TEXT CHECK(undo_action_type IN ('remove_dir', 'move', 'delete_copy', 'untrash', 'trash')) NOT NULL,
                undo_source_path TEXT,
                undo_destination_path TEXT,
                status TEXT CHECK(status IN ('PRE_LOGGED', 'EXECUTED', 'FAILED', 'REVERTED')) NOT NULL,
                executed_at TIMESTAMP,
                reverted_at TIMESTAMP,
                error_message TEXT,
                FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            """)
            cur.execute("""
                INSERT INTO action_ledger 
                SELECT id, batch_id, step_index, action_type, source_path, destination_path, file_size_bytes, 
                       sha256_checksum, undo_action_type, undo_source_path, undo_destination_path, status, 
                       executed_at, reverted_at, error_message 
                FROM action_ledger_old;
            """)
            cur.execute("DROP TABLE action_ledger_old;")
            cur.execute("PRAGMA foreign_keys = ON;")

        # Check if batches needs migration to support CANCELLED
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='batches'")
        brow = cur.fetchone()
        if brow and "CANCELLED" not in brow[0]:
            cur.execute("PRAGMA foreign_keys = OFF;")
            cur.execute("ALTER TABLE batches RENAME TO batches_old;")
            cur.execute("""
            CREATE TABLE batches (
                batch_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                intent TEXT NOT NULL,
                total_actions INTEGER NOT NULL,
                executed_actions INTEGER DEFAULT 0,
                status TEXT CHECK(status IN ('PENDING', 'RUNNING', 'COMPLETED', 'CANCELLED', 'FAILED', 'ROLLED_BACK')) NOT NULL,
                completed_at TIMESTAMP,
                rolled_back_at TIMESTAMP
            );
            """)
            cur.execute("INSERT INTO batches SELECT * FROM batches_old;")
            cur.execute("DROP TABLE batches_old;")
            cur.execute("PRAGMA foreign_keys = ON;")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            intent TEXT NOT NULL,
            total_actions INTEGER NOT NULL,
            executed_actions INTEGER DEFAULT 0,
            status TEXT CHECK(status IN ('PENDING', 'RUNNING', 'COMPLETED', 'CANCELLED', 'FAILED', 'ROLLED_BACK')) NOT NULL,
            completed_at TIMESTAMP,
            rolled_back_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS action_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            action_type TEXT CHECK(action_type IN ('make_dir', 'move', 'copy', 'trash', 'write_file')) NOT NULL,
            source_path TEXT,
            destination_path TEXT,
            file_size_bytes INTEGER DEFAULT 0,
            sha256_checksum TEXT,
            undo_action_type TEXT CHECK(undo_action_type IN ('remove_dir', 'move', 'delete_copy', 'untrash', 'trash')) NOT NULL,
            undo_source_path TEXT,
            undo_destination_path TEXT,
            status TEXT CHECK(status IN ('PRE_LOGGED', 'EXECUTED', 'FAILED', 'REVERTED')) NOT NULL,
            executed_at TIMESTAMP,
            reverted_at TIMESTAMP,
            error_message TEXT,
            FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS trash_index (
            trash_id TEXT PRIMARY KEY,
            action_id INTEGER NOT NULL,
            original_rel_path TEXT NOT NULL,
            trashed_rel_path TEXT NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            sha256_checksum TEXT,
            trashed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            purged_at TIMESTAMP,
            FOREIGN KEY(action_id) REFERENCES action_ledger(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS file_content_index (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size_bytes INTEGER NOT NULL,
            sanitized_text TEXT,
            is_corrupt INTEGER DEFAULT 0,
            is_scanned INTEGER DEFAULT 0,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_actions_batch ON action_ledger(batch_id);
        CREATE INDEX IF NOT EXISTS idx_actions_status ON action_ledger(status);
        CREATE INDEX IF NOT EXISTS idx_trash_original ON trash_index(original_rel_path);
        CREATE INDEX IF NOT EXISTS idx_fci_path ON file_content_index(path);
        """)


def get_indexed_file(path: str, mtime: float, size_bytes: int):
    """Retrieves cached file content snippet and health if mtime and size_bytes match."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT path, mtime, size_bytes, sanitized_text, is_corrupt, is_scanned
                FROM file_content_index
                WHERE path = ? AND ABS(mtime - ?) < 0.001 AND size_bytes = ?
            """, (path, mtime, size_bytes))
            row = cur.fetchone()
            if row:
                return {
                    "path": row["path"],
                    "mtime": row["mtime"],
                    "size_bytes": row["size_bytes"],
                    "sanitized_text": row["sanitized_text"],
                    "is_corrupt": bool(row["is_corrupt"]),
                    "is_scanned": bool(row["is_scanned"])
                }
    except Exception:
        pass
    return None


def upsert_indexed_file(
    path: str,
    mtime: float,
    size_bytes: int,
    sanitized_text: str,
    is_corrupt: bool = False,
    is_scanned: bool = False
) -> None:
    """Inserts or replaces file content snippet and health metadata in SQLite index."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO file_content_index
                (path, mtime, size_bytes, sanitized_text, is_corrupt, is_scanned, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (path, mtime, size_bytes, sanitized_text, int(is_corrupt), int(is_scanned)))
            conn.commit()
    except Exception:
        pass


def get_canonical_index_stats() -> dict:
    """Returns canonical index counts across file_content_index."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), SUM(is_corrupt), SUM(is_scanned) FROM file_content_index")
            row = cur.fetchone()
            total = row[0] or 0
            corrupt = row[1] or 0
            scanned = row[2] or 0
            return {
                "total_indexed": total,
                "corrupt_files": corrupt,
                "scanned_or_binary_files": scanned
            }
    except Exception:
        return {"total_indexed": 0, "corrupt_files": 0, "scanned_or_binary_files": 0}
