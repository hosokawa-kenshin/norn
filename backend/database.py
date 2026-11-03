import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from settings import DB_PATH, GOOGLE_CALENDAR_ID

transaction_connection: ContextVar[sqlite3.Connection | None] = ContextVar('transaction_connection', default=None)


class ConnectionLease:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        pass

    def commit(self):
        pass


@contextmanager
def database_transaction(rollback: bool = False):
    if transaction_connection.get() is not None:
        raise RuntimeError('Nested database transaction')
    with closing(open_database()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        token = transaction_connection.set(connection)
        try:
            yield connection
            if rollback:
                connection.rollback()
            else:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            transaction_connection.reset(token)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_database() -> sqlite3.Connection:
    existing = transaction_connection.get()
    if existing is not None:
        return ConnectionLease(existing)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def initialize_database() -> None:
    with closing(open_database()) as connection:
        existing_tables = {
            row['name'] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            ).fetchall()
        }
        if 'routine_occurrences' in existing_tables:
            routine_occurrence_columns = {
                row['name'] for row in connection.execute(
                    'PRAGMA table_info(routine_occurrences)',
                ).fetchall()
            }
            if 'source_task_id' in routine_occurrence_columns and 'routine_id' not in routine_occurrence_columns:
                if 'task_occurrence_links' not in existing_tables:
                    connection.execute(
                        'ALTER TABLE routine_occurrences RENAME TO task_occurrence_links',
                    )
                else:
                    connection.execute(
                        '''INSERT OR IGNORE INTO task_occurrence_links (source_task_id, occurrence_date, task_id)
                           SELECT source_task_id, occurrence_date, task_id FROM routine_occurrences''',
                    )
                    connection.execute('DROP TABLE routine_occurrences')
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
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                routine_occurrence_id TEXT,
                kind TEXT NOT NULL DEFAULT 'fixed'
                    CHECK(kind IN ('fixed', 'task_block', 'routine_block', 'break', 'personal')),
                source TEXT NOT NULL DEFAULT 'user'
                    CHECK(source IN ('user', 'norn', 'google')),
                schedule_status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(schedule_status IN ('scheduled', 'completed', 'cancelled')),
                is_fixed INTEGER NOT NULL DEFAULT 1,
                is_important INTEGER NOT NULL DEFAULT 0,
                is_all_day INTEGER NOT NULL DEFAULT 0,
                recurrence_timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo',
                recurrence_json TEXT NOT NULL DEFAULT '[]',
                google_event_id TEXT UNIQUE,
                google_calendar_id TEXT,
                google_etag TEXT,
                google_updated_at TEXT,
                sync_snapshot TEXT,
                sync_status TEXT NOT NULL DEFAULT 'local',
                conflict_payload TEXT,
                recurring_event_id TEXT,
                original_start_at TEXT,
                is_cancelled INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_events_start_end
                ON events(start_at, end_at);
            CREATE TABLE IF NOT EXISTS google_calendar_sync_state (
                calendar_id TEXT PRIMARY KEY,
                sync_token TEXT,
                last_sync_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS google_calendar_preferences (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                calendar_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS google_calendar_oauth_states (
                state TEXT PRIMARY KEY,
                code_verifier TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
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
                remaining_minutes INTEGER,
                priority TEXT NOT NULL CHECK(priority IN ('low', 'normal', 'high')),
                status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'done', 'cancelled')),
                focus_level INTEGER CHECK(focus_level BETWEEN 1 AND 5),
                preferred_period TEXT,
                splittable INTEGER NOT NULL DEFAULT 1,
                min_session_minutes INTEGER,
                max_session_minutes INTEGER,
                context TEXT NOT NULL DEFAULT '',
                flexibility TEXT NOT NULL DEFAULT 'user_approval'
                    CHECK(flexibility IN ('flexible', 'user_approval', 'fixed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_status_due
                ON tasks(status, due_at);
            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on_task_id),
                CHECK (task_id != depends_on_task_id)
            );
            CREATE TABLE IF NOT EXISTS task_allocations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_task_allocations_start_end
                ON task_allocations(start_at, end_at);
            CREATE INDEX IF NOT EXISTS ix_task_allocations_task_start
                ON task_allocations(task_id, start_at);
            CREATE TABLE IF NOT EXISTS task_occurrence_links (
                source_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                occurrence_date TEXT NOT NULL,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
                PRIMARY KEY (source_task_id, occurrence_date)
            );
            CREATE TABLE IF NOT EXISTS routines (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                routine_type TEXT NOT NULL DEFAULT 'maintenance'
                    CHECK(routine_type IN ('achievement', 'maintenance', 'fixed')),
                frequency TEXT NOT NULL DEFAULT 'weekly',
                target_count INTEGER NOT NULL DEFAULT 1 CHECK(target_count > 0),
                duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                preferred_period TEXT,
                min_count INTEGER NOT NULL DEFAULT 0 CHECK(min_count >= 0),
                max_count INTEGER NOT NULL CHECK(max_count > 0),
                adjustment_policy TEXT NOT NULL DEFAULT 'user_approval'
                    CHECK(adjustment_policy IN ('flexible', 'user_approval', 'fixed')),
                flexible INTEGER NOT NULL DEFAULT 0,
                priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('low', 'normal', 'high')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS routine_plans (
                id TEXT PRIMARY KEY,
                routine_id TEXT NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                target_count INTEGER NOT NULL CHECK(target_count >= 0),
                duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                adjustment_reason TEXT NOT NULL DEFAULT '',
                adjustment_source TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'planned'
                    CHECK(status IN ('planned', 'active', 'completed', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(period_end >= period_start)
            );
            CREATE INDEX IF NOT EXISTS ix_routine_plans_period
                ON routine_plans(routine_id, period_start, period_end);
            CREATE TABLE IF NOT EXISTS routine_occurrences (
                id TEXT PRIMARY KEY,
                plan_id TEXT REFERENCES routine_plans(id) ON DELETE SET NULL,
                routine_id TEXT NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
                schedule_id TEXT REFERENCES events(id) ON DELETE SET NULL,
                scheduled_start TEXT NOT NULL,
                scheduled_end TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN ('scheduled', 'completed', 'partial', 'skipped', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(scheduled_end > scheduled_start)
            );
            CREATE INDEX IF NOT EXISTS ix_routine_occurrences_routine_start
                ON routine_occurrences(routine_id, scheduled_start);
            CREATE TABLE IF NOT EXISTS execution_logs (
                id TEXT PRIMARY KEY,
                schedule_id TEXT REFERENCES events(id) ON DELETE SET NULL,
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                routine_id TEXT REFERENCES routines(id) ON DELETE SET NULL,
                occurrence_id TEXT REFERENCES routine_occurrences(id) ON DELETE SET NULL,
                started_at TEXT,
                ended_at TEXT,
                actual_minutes INTEGER CHECK(actual_minutes IS NULL OR actual_minutes >= 0),
                status TEXT NOT NULL CHECK(status IN ('completed', 'partial', 'skipped', 'cancelled')),
                note TEXT NOT NULL DEFAULT '',
                resume_point TEXT NOT NULL DEFAULT '',
                interruption_reason TEXT NOT NULL DEFAULT '',
                progress_percent INTEGER CHECK(progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100),
                recorded_source TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                CHECK(started_at IS NOT NULL OR ended_at IS NOT NULL OR status IN ('skipped', 'cancelled'))
            );
            CREATE INDEX IF NOT EXISTS ix_execution_logs_task_created
                ON execution_logs(task_id, created_at);
            CREATE TABLE IF NOT EXISTS work_sessions (
                id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                routine_id TEXT REFERENCES routines(id) ON DELETE CASCADE,
                occurrence_id TEXT REFERENCES routine_occurrences(id) ON DELETE CASCADE,
                schedule_id TEXT REFERENCES events(id) ON DELETE SET NULL,
                started_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedule_notices (
                notice_key TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                item_id TEXT NOT NULL,
                end_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            '''
        )
        # 承認待ちの提案は廃止したため、旧版の提案表を削除する
        connection.execute('DROP TABLE IF EXISTS schedule_proposals')
        task_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(tasks)').fetchall()
        }
        task_migrations = {
            'project_id': 'TEXT REFERENCES projects(id) ON DELETE SET NULL',
            'remaining_minutes': 'INTEGER',
            'focus_level': 'INTEGER CHECK(focus_level BETWEEN 1 AND 5)',
            'preferred_period': 'TEXT',
            'splittable': 'INTEGER NOT NULL DEFAULT 1',
            'min_session_minutes': 'INTEGER',
            'max_session_minutes': 'INTEGER',
            'context': "TEXT NOT NULL DEFAULT ''",
            'flexibility': "TEXT NOT NULL DEFAULT 'user_approval'",
        }
        for column, definition in task_migrations.items():
            if column not in task_columns:
                connection.execute(f'ALTER TABLE tasks ADD COLUMN {column} {definition}')
        # 初期値を0にしていた旧版のタスクを一度だけ分割可能へ揃える
        if connection.execute('PRAGMA user_version').fetchone()[0] < 1:
            connection.execute('UPDATE tasks SET splittable = 1')
            connection.execute('PRAGMA user_version = 1')
        goal_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(goals)').fetchall()
        }
        if 'last_reviewed_at' not in goal_columns:
            connection.execute('ALTER TABLE goals ADD COLUMN last_reviewed_at TEXT')
        event_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(events)').fetchall()
        }
        event_migrations = {
            'task_id': 'TEXT REFERENCES tasks(id) ON DELETE SET NULL',
            'routine_occurrence_id': 'TEXT',
            'kind': "TEXT NOT NULL DEFAULT 'fixed'",
            'source': "TEXT NOT NULL DEFAULT 'user'",
            'schedule_status': "TEXT NOT NULL DEFAULT 'scheduled'",
            'is_fixed': 'INTEGER NOT NULL DEFAULT 1',
            'is_important': 'INTEGER NOT NULL DEFAULT 0',
            'is_all_day': 'INTEGER NOT NULL DEFAULT 0',
            'recurrence_timezone': "TEXT NOT NULL DEFAULT 'Asia/Tokyo'",
            'recurrence_json': "TEXT NOT NULL DEFAULT '[]'",
            'google_event_id': 'TEXT',
            'google_calendar_id': 'TEXT',
            'google_etag': 'TEXT',
            'google_updated_at': 'TEXT',
            'sync_snapshot': 'TEXT',
            'sync_status': "TEXT NOT NULL DEFAULT 'local'",
            'conflict_payload': 'TEXT',
            'recurring_event_id': 'TEXT',
            'original_start_at': 'TEXT',
            'is_cancelled': 'INTEGER NOT NULL DEFAULT 0',
            'deleted_at': 'TEXT',
        }
        for column, definition in event_migrations.items():
            if column not in event_columns:
                connection.execute(f'ALTER TABLE events ADD COLUMN {column} {definition}')
        log_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(execution_logs)').fetchall()
        }
        if 'occurrence_id' not in log_columns:
            connection.execute(
                'ALTER TABLE execution_logs ADD COLUMN occurrence_id TEXT REFERENCES routine_occurrences(id) ON DELETE SET NULL',
            )
        if 'interruption_reason' not in log_columns:
            connection.execute("ALTER TABLE execution_logs ADD COLUMN interruption_reason TEXT NOT NULL DEFAULT ''")
        if 'progress_percent' not in log_columns:
            connection.execute(
                'ALTER TABLE execution_logs ADD COLUMN progress_percent INTEGER CHECK(progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100)',
            )
        connection.execute(
            '''UPDATE events SET source = CASE WHEN google_event_id IS NOT NULL THEN 'google' ELSE 'user' END,
               is_fixed = 1 WHERE kind = 'fixed' AND source = 'user' AND is_fixed = 0''',
        )
        allocation_indexes = connection.execute(
            "PRAGMA index_list('task_allocations')",
        ).fetchall()
        if any(index['unique'] and index['origin'] == 'u' for index in allocation_indexes):
            connection.execute(
                '''CREATE TABLE task_allocations_rebuilt (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )''',
            )
            connection.execute(
                '''INSERT INTO task_allocations_rebuilt
                   (id, task_id, start_at, end_at, created_at, updated_at)
                   SELECT id, task_id, start_at, end_at, created_at, updated_at FROM task_allocations''',
            )
            connection.execute('DROP TABLE task_allocations')
            connection.execute('ALTER TABLE task_allocations_rebuilt RENAME TO task_allocations')
            connection.execute(
                'CREATE INDEX IF NOT EXISTS ix_task_allocations_start_end ON task_allocations(start_at, end_at)',
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS ix_task_allocations_task_start ON task_allocations(task_id, start_at)',
            )
        connection.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_events_google_event_id ON events(google_event_id)'
        )
        connection.execute(
            'INSERT OR IGNORE INTO google_calendar_sync_state (calendar_id, updated_at) VALUES (?, ?)',
            (GOOGLE_CALENDAR_ID, now_iso()),
        )
        connection.execute(
            'INSERT OR IGNORE INTO google_calendar_preferences (id, calendar_id, updated_at) VALUES (1, ?, ?)',
            (GOOGLE_CALENDAR_ID, now_iso()),
        )
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
