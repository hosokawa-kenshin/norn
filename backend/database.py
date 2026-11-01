import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from settings import DB_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_database() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def initialize_database() -> None:
    with closing(open_database()) as connection:
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_messages_conversation
                ON messages(conversation_id, id);
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL CHECK(category IN ('fact', 'preference', 'episode')),
                content TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('explicit', 'inferred')),
                source_conversation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_memories_category_updated
                ON memories(category, updated_at);
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                vector BLOB NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_extraction_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                last_attempted_at TEXT,
                last_processed_message_id INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO memory_extraction_state (id) VALUES (1);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_events_start_end
                ON events(start_at, end_at);
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'cancelled')),
                target_date TEXT,
                last_reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_goals_status_target
                ON goals(status, target_date);
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                goal_id TEXT REFERENCES goals(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'cancelled')),
                target_date TEXT,
                next_action TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_projects_goal_status
                ON projects(goal_id, status);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                due_at TEXT,
                estimated_minutes INTEGER,
                priority TEXT NOT NULL CHECK(priority IN ('low', 'normal', 'high')),
                status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'done', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_status_due
                ON tasks(status, due_at);
            CREATE TABLE IF NOT EXISTS task_allocations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_task_allocations_start_end
                ON task_allocations(start_at, end_at);
            '''
        )
        task_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(tasks)').fetchall()
        }
        if 'project_id' not in task_columns:
            connection.execute(
                'ALTER TABLE tasks ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE SET NULL'
            )
        goal_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(goals)').fetchall()
        }
        if 'last_reviewed_at' not in goal_columns:
            connection.execute('ALTER TABLE goals ADD COLUMN last_reviewed_at TEXT')
        connection.execute(
            'CREATE INDEX IF NOT EXISTS ix_tasks_project_status ON tasks(project_id, status)'
        )
        connection.commit()


def get_conversation_messages(conversation_id: str, limit: int = 20) -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            '''
            SELECT role, content FROM messages
            WHERE conversation_id = ? ORDER BY id DESC LIMIT ?
            ''',
            (conversation_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]
