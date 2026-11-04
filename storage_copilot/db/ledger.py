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

        CREATE INDEX IF NOT EXISTS idx_actions_batch ON action_ledger(batch_id);
        CREATE INDEX IF NOT EXISTS idx_actions_status ON action_ledger(status);
        CREATE INDEX IF NOT EXISTS idx_trash_original ON trash_index(original_rel_path);
        """)
