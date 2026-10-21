"""SQLite 数据层：建表、轻量迁移与查询助手。

全部数据库访问都发生在 asyncio 事件循环线程（所有路由都是 async def，后台执行也在同一循环），
因此单连接即可，且不存在并发写冲突。外部 HTTP 调用通过 to_thread 执行，不碰数据库。
"""
from __future__ import annotations

import json
import sqlite3
from functools import wraps
from typing import Any, Iterable

from . import config

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
  quota_monthly_cny REAL NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (email TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_settings (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS match_weights (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  w_tag REAL NOT NULL, w_tool REAL NOT NULL, w_perm REAL NOT NULL, w_cost REAL NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, goal TEXT NOT NULL, mode TEXT NOT NULL, lane TEXT,
  status TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
  output_language TEXT NOT NULL DEFAULT 'zh', model_choice TEXT NOT NULL DEFAULT 'auto',
  max_concurrency INTEGER NOT NULL DEFAULT 4, budget_cny REAL NOT NULL, cost_used REAL NOT NULL DEFAULT 0,
  attachments TEXT NOT NULL DEFAULT '[]', playbook_id TEXT, active_version INTEGER,
  settings_snapshot TEXT NOT NULL DEFAULT '{}',
  est_makespan_s REAL, est_serial_s REAL, actual_makespan_s REAL, run_started_at INTEGER,
  error_code TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, updated_at);
CREATE TABLE IF NOT EXISTS plan_versions (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  version INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  deliverable TEXT NOT NULL DEFAULT '{}', optimizations TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL, UNIQUE(task_id, version)
);
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL, node_key TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', acceptance TEXT NOT NULL DEFAULT '[]',
  depends_on TEXT NOT NULL DEFAULT '[]', input_refs TEXT NOT NULL DEFAULT '[]',
  produces_sections TEXT NOT NULL DEFAULT '[]',
  capability_requirements TEXT NOT NULL DEFAULT '{}', agent TEXT NOT NULL DEFAULT '{}',
  tools TEXT NOT NULL DEFAULT '[]', model_tier TEXT NOT NULL DEFAULT 'standard', model_override TEXT,
  requires_approval INTEGER NOT NULL DEFAULT 0, approval_status TEXT NOT NULL DEFAULT 'none',
  est_complexity INTEGER NOT NULL DEFAULT 3, est_duration_s REAL, est_cost_cny REAL,
  rank REAL NOT NULL DEFAULT 0, on_critical_path INTEGER NOT NULL DEFAULT 0,
  redundant_edges TEXT NOT NULL DEFAULT '[]',
  revision_instructions TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending', input_hash TEXT, execution_id TEXT,
  output TEXT, attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, error TEXT,
  spec_state TEXT, started_at INTEGER, finished_at INTEGER,
  UNIQUE(plan_version_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_nodes_task ON nodes(task_id);
CREATE TABLE IF NOT EXISTS executions (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_key TEXT NOT NULL, plan_version INTEGER NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, speculative INTEGER NOT NULL DEFAULT 0, input_hash TEXT NOT NULL,
  input_snapshot TEXT NOT NULL DEFAULT '{}', output TEXT, tool_calls TEXT NOT NULL DEFAULT '[]',
  model_used TEXT, cost_cny REAL NOT NULL DEFAULT 0, error_code TEXT, error TEXT,
  started_at INTEGER NOT NULL, finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_exec_hash ON executions(task_id, node_key, input_hash);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL, node_key TEXT NOT NULL, plan_version INTEGER NOT NULL,
  status TEXT NOT NULL, reason TEXT, decided_by TEXT, created_at INTEGER NOT NULL, decided_at INTEGER,
  kind TEXT NOT NULL DEFAULT 'node', tool TEXT, args TEXT, args_hash TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, type TEXT NOT NULL, node_key TEXT, plan_version INTEGER,
  payload TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL, UNIQUE(task_id, seq)
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, task_id TEXT, node_key TEXT, kind TEXT NOT NULL,
  source TEXT NOT NULL, provider_id TEXT, model_id TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_cny REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL, error_code TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_user ON llm_calls(user_id, created_at);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_key TEXT, section_key TEXT, user_id TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', applied_version INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attention_items (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_key TEXT, plan_version INTEGER, kind TEXT NOT NULL, severity TEXT NOT NULL,
  title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
  answer TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  plan_version INTEGER NOT NULL, title TEXT NOT NULL, sections TEXT NOT NULL,
  cards TEXT NOT NULL DEFAULT '[]',
  markdown TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_documents (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, source TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
  content TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0, cited_count INTEGER NOT NULL DEFAULT 0,
  source_task_id TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_chunks (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL, idx INTEGER NOT NULL, heading TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
  terms TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON kb_chunks(user_id);
CREATE TABLE IF NOT EXISTS capabilities (
  key TEXT PRIMARY KEY, name TEXT NOT NULL, name_en TEXT NOT NULL, description TEXT NOT NULL,
  tags TEXT NOT NULL, tools TEXT NOT NULL, permission TEXT NOT NULL, cost_tier INTEGER NOT NULL,
  default_tier TEXT NOT NULL, system_prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  use_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playbooks (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', sample_goal TEXT NOT NULL,
  template_nodes TEXT NOT NULL, use_count INTEGER NOT NULL DEFAULT 0, source_task_id TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  statement TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global', status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, task_id TEXT, title TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_configs (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, source TEXT NOT NULL, display_name TEXT NOT NULL, base_url TEXT NOT NULL,
  key_ciphertext TEXT, key_hint TEXT, status TEXT NOT NULL DEFAULT 'untested',
  latency_ms INTEGER, probed_at INTEGER, concurrency INTEGER NOT NULL DEFAULT 4,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_models (
  id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES provider_configs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL, model_name TEXT NOT NULL, display_name TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '[]', price_in_per_1m REAL NOT NULL DEFAULT 0,
  price_out_per_1m REAL NOT NULL DEFAULT 0, visible INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS canvas_layouts (
  user_id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  positions TEXT NOT NULL DEFAULT '{}', viewport TEXT NOT NULL DEFAULT '{}',
  collapsed TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL, PRIMARY KEY(user_id, task_id)
);
CREATE TABLE IF NOT EXISTS section_edits (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  plan_version INTEGER NOT NULL, section_key TEXT NOT NULL, user_id TEXT NOT NULL,
  content TEXT NOT NULL, base_hash TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_section_edits ON section_edits(task_id, section_key);

-- ================= v3.0 外部 Agent（L1 MCP 接入，prd/08 §4） =================
-- agents：用户已安装/连接的外部 Agent 注册表（目录本身在 data/agents_catalog.json，不进库）。
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  key TEXT NOT NULL, name TEXT NOT NULL, name_en TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'mcp', domain TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', description_en TEXT NOT NULL DEFAULT '',
  transport TEXT NOT NULL DEFAULT 'stdio', endpoint TEXT NOT NULL DEFAULT '',
  command TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '[]',
  headers TEXT NOT NULL DEFAULT '{}',
  auth TEXT NOT NULL DEFAULT 'none', trust TEXT NOT NULL DEFAULT 'community',
  cost TEXT NOT NULL DEFAULT 'free', latency TEXT NOT NULL DEFAULT 'fast',
  docs TEXT NOT NULL DEFAULT '',
  toolset TEXT NOT NULL DEFAULT '[]',        -- 目录策展的精选工具集（期望：name/risk/desc）
  tools TEXT NOT NULL DEFAULT '[]',          -- 连接后缓存的真实工具（name/desc/risk/schema/fn）
  tool_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'disconnected', error TEXT NOT NULL DEFAULT '',
  cred_hint TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, allow_write INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'catalog',
  builtin TEXT NOT NULL DEFAULT '',          -- 'demo' = 平台内置演示服务器（命令由服务端解析）
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agents_user_key ON agents(user_id, key);
-- agent_credentials：凭据 AESGCM 加密存储（AAD 绑定 user+agent，复用 SECRETS_MASTER_KEY 体系）；永不出现在接口与日志。
CREATE TABLE IF NOT EXISTS agent_credentials (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_key TEXT NOT NULL,
  payload TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_cred ON agent_credentials(user_id, agent_key);
-- agent_calls：外部调用审计账本（不存参数明文，只存参数哈希与结果字节数）。
CREATE TABLE IF NOT EXISTS agent_calls (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', node_key TEXT NOT NULL DEFAULT '',
  agent_key TEXT NOT NULL, tool TEXT NOT NULL, args_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, ms INTEGER NOT NULL DEFAULT 0, result_bytes INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_calls ON agent_calls(user_id, created_at);

-- ================= v3.1 微信扫码登录（F30） =================
-- auth_tickets：一次扫码会话。state 是防 CSRF 的一次性票据，PC 端凭它轮询取登录态。
CREATE TABLE IF NOT EXISTS auth_tickets (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'wechat',
  status TEXT NOT NULL DEFAULT 'pending',        -- pending | confirmed | consumed
  user_id TEXT, next TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_tickets ON auth_tickets(status, expires_at);
-- wechat_identities：微信身份 → 本地用户。同一用户可绑定多个 AppID 下的身份；unionid 优先匹配。
CREATE TABLE IF NOT EXISTS wechat_identities (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  appid TEXT NOT NULL, openid TEXT NOT NULL, unionid TEXT NOT NULL DEFAULT '',
  nickname TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, UNIQUE(appid, openid)
);
CREATE INDEX IF NOT EXISTS idx_wx_union ON wechat_identities(unionid);
CREATE TABLE IF NOT EXISTS tool_operations (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_key TEXT NOT NULL, tool TEXT NOT NULL, status TEXT NOT NULL,
  result TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS team_spaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  request_key TEXT NOT NULL, created_at INTEGER NOT NULL,
  UNIQUE(owner_id, request_key)
);
CREATE TABLE IF NOT EXISTS team_members (
  team_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('owner','member')), joined_at INTEGER NOT NULL,
  PRIMARY KEY(team_id,user_id)
);
CREATE TABLE IF NOT EXISTS team_invites (
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS team_templates (
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
  author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
  title TEXT NOT NULL, description TEXT NOT NULL, version INTEGER NOT NULL,
  request_key TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE(team_id,request_key)
);
CREATE TABLE IF NOT EXISTS team_template_versions (
  template_id TEXT NOT NULL REFERENCES team_templates(id) ON DELETE CASCADE,
  version INTEGER NOT NULL, definition TEXT NOT NULL, digest TEXT NOT NULL,
  created_at INTEGER NOT NULL, PRIMARY KEY(template_id,version)
);
CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);
CREATE INDEX IF NOT EXISTS idx_team_templates_team ON team_templates(team_id,updated_at);
"""

_conn: sqlite3.Connection | None = None
_transactions: list[list] = []

# 轻量迁移：老库缺列时补列（列已存在则跳过），只加列、不改列、不删数据
MIGRATIONS = [
    ("tasks", "completion_quality", "TEXT"),
    ("artifacts", "quality", "TEXT NOT NULL DEFAULT '{}'"),
    ("approvals", "kind", "TEXT NOT NULL DEFAULT 'node'"),   # node=产出确认；tool=危险操作执行前闸门
    ("approvals", "tool", "TEXT"),
    ("approvals", "args", "TEXT"),
    ("approvals", "args_hash", "TEXT"),
    ("artifacts", "cards", "TEXT NOT NULL DEFAULT '[]'"),    # 成果卡片：图 / 文 / 文件（v1.5）
    ("canvas_layouts", "art", "TEXT NOT NULL DEFAULT '{}'"), # 成果节点视图状态：hidden / pinned / focus（v1.6）
    ("nodes", "agents", "TEXT"),                             # 外部 Agent 决策：primary / alternates / attached / gap（v3.0 M2，NULL=无外部需求）
    ("nodes", "agent_needs", "TEXT NOT NULL DEFAULT '[]'"),  # 模型声明的外部能力需求（v3.0 M2）
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(config.DB_PATH), isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


def reset_connection() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn().execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def ex(sql: str, params: Iterable[Any] = ()) -> int:
    cur = conn().execute(sql, tuple(params))
    return cur.rowcount


def insert(table: str, row: dict) -> None:
    cols = ",".join(row)
    marks = ",".join("?" for _ in row)
    conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(_enc(v) for v in row.values()))


def update(table: str, where: dict, values: dict) -> int:
    sets = ",".join(f"{k}=?" for k in values)
    cond = " AND ".join(f"{k}=?" for k in where)
    cur = conn().execute(f"UPDATE {table} SET {sets} WHERE {cond}",
                         tuple(_enc(v) for v in values.values()) + tuple(where.values()))
    return cur.rowcount


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def j(v: str | None, default: Any = None) -> Any:
    """解码 JSON 列。"""
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class Tx:
    """显式事务：with Tx(): ...  —— 用于需要原子性的多步写入（如创建修订版本）。"""

    def __enter__(self):
        self.name = f"tx_{len(_transactions)}"
        conn().execute("BEGIN" if not _transactions else f"SAVEPOINT {self.name}")
        _transactions.append([])
        return self

    def __exit__(self, et, ev, tb):
        callbacks = _transactions.pop()
        if _transactions:
            if et:
                conn().execute(f"ROLLBACK TO {self.name}")
            conn().execute(f"RELEASE {self.name}")
            if not et:
                _transactions[-1].extend(callbacks)
        else:
            conn().execute("ROLLBACK" if et else "COMMIT")
            if not et:
                for callback in callbacks:
                    callback()
        return False


def after_commit(callback) -> None:
    if _transactions:
        _transactions[-1].append(callback)
    else:
        callback()


def atomic(fn):
    """Synchronous domain transitions only: never keep a transaction across an await."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with Tx():
            return fn(*args, **kwargs)
    return wrapped
