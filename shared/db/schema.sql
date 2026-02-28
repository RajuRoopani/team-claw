-- Team Claw — PostgreSQL schema
-- Initialized automatically by Docker on first run

CREATE TABLE IF NOT EXISTS threads (
    id          VARCHAR(36) PRIMARY KEY,
    title       VARCHAR(500),
    status      VARCHAR(50)  DEFAULT 'active',
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id                  VARCHAR(36) PRIMARY KEY,
    thread_id           VARCHAR(36) REFERENCES threads(id) ON DELETE CASCADE,
    from_role           VARCHAR(100) NOT NULL,
    to_role             VARCHAR(100) NOT NULL,
    type                VARCHAR(100) NOT NULL,
    content             TEXT        NOT NULL,
    priority            VARCHAR(50)  DEFAULT 'normal',
    artifacts           JSONB        DEFAULT '[]',
    parent_message_id   VARCHAR(36),
    metadata            JSONB        DEFAULT '{}',
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_thread    ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_from_role ON messages(from_role);
CREATE INDEX IF NOT EXISTS idx_messages_to_role   ON messages(to_role);
CREATE INDEX IF NOT EXISTS idx_messages_created   ON messages(created_at DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id          VARCHAR(36) PRIMARY KEY,
    thread_id   VARCHAR(36) REFERENCES threads(id),
    title       VARCHAR(500) NOT NULL,
    description TEXT,
    assignee    VARCHAR(100),
    status      VARCHAR(50)  DEFAULT 'pending',   -- pending|in_progress|review|done
    created_by  VARCHAR(100),
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_thread   ON tasks(thread_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);

-- ── Phase 4: persistent memory, team wiki, token telemetry ────────────────

CREATE TABLE IF NOT EXISTS agent_memories (
    agent_role  TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_role, key)
);

CREATE TABLE IF NOT EXISTS team_wiki (
    title       TEXT        PRIMARY KEY,
    content     TEXT        NOT NULL,
    author      TEXT        NOT NULL DEFAULT 'unknown',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_metrics (
    id              BIGSERIAL   PRIMARY KEY,
    agent_role      TEXT        NOT NULL,
    thread_id       TEXT,
    model           TEXT        NOT NULL,
    input_tokens    INTEGER     NOT NULL DEFAULT 0,
    output_tokens   INTEGER     NOT NULL DEFAULT 0,
    recorded_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_metrics_role ON agent_metrics(agent_role);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_time ON agent_metrics(recorded_at DESC);

-- ── Phase 5: CI results ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ci_results (
    id          BIGSERIAL   PRIMARY KEY,
    task_id     TEXT,
    thread_id   TEXT,
    passed      INTEGER     NOT NULL DEFAULT 0,
    failed      INTEGER     NOT NULL DEFAULT 0,
    total       INTEGER     NOT NULL DEFAULT 0,
    exit_code   INTEGER     NOT NULL,
    output      TEXT,
    ran_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ci_results_task   ON ci_results(task_id);
CREATE INDEX IF NOT EXISTS idx_ci_results_thread ON ci_results(thread_id);

-- ── Phase 8: tool execution telemetry ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS tool_executions (
    id          BIGSERIAL   PRIMARY KEY,
    agent_role  TEXT        NOT NULL,
    tool_name   TEXT        NOT NULL,
    thread_id   TEXT,
    duration_ms INTEGER     NOT NULL DEFAULT 0,
    success     BOOLEAN     NOT NULL DEFAULT TRUE,
    error       TEXT        DEFAULT '',
    executed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tool_exec_agent ON tool_executions(agent_role);
CREATE INDEX IF NOT EXISTS idx_tool_exec_tool  ON tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_exec_time  ON tool_executions(executed_at DESC);

-- ── Phase 9: human-in-the-loop ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS human_questions (
    id          BIGSERIAL   PRIMARY KEY,
    thread_id   TEXT        NOT NULL,
    from_role   TEXT        NOT NULL,
    question    TEXT        NOT NULL,
    context     TEXT        DEFAULT '',
    answered    BOOLEAN     NOT NULL DEFAULT FALSE,
    answer      TEXT        DEFAULT '',
    answered_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hq_thread     ON human_questions(thread_id);
CREATE INDEX IF NOT EXISTS idx_hq_unanswered ON human_questions(answered) WHERE NOT answered;
