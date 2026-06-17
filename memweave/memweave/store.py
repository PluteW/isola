"""SQLite 真相源（SDD v2 §1）。系统唯一 source of truth：状态/幂等/纠正/恢复全依赖它。

守护的设计承诺（→ test-plan-v1 §3）：
  T-INV-1 event 幂等 / T-INV-2 message 幂等 / T-INV-3 单活跃 decision /
  T-INV-4 write_job 幂等 / T-INV-5 memory 幂等 / T-INV-7 retired 不召回 /
  T-REC-1~4 崩溃恢复扫描。

实现层加固：
  - recovery_scan 每个 job 在单事务内提交（commit+write+done 原子），并补偿 COMMITTED+pending 半状态；
  - 状态迁移条件更新（_ALLOWED_FROM 守护，禁止 CORRECTED→COMMITTED 等非法回退）；
  - job 领取条件更新（仅 pending→running，返回是否抢占成功）；
  - 幂等改 INSERT-first（以唯一约束为准，冲突返回既有），消除"先查后插"TOCTOU；
  - CHECK 值域约束防状态拼写绕过；recall 强制 project 带 project_id 防跨项目召回污染。
已知限制（v0.1，记入待办）：未加外键（待集成层用完整 message→decision→memory 链补）；单进程线程模型（check_same_thread=False + busy_timeout 兜底）。
"""
from __future__ import annotations
import sqlite3
import hashlib
import json
import time
import uuid
from .models import (
    InboundMessage, RouteDecision, MemoryItem,
    WAITING_CONFIRMATION, TENTATIVE, COMMITTED, CORRECTED,
    JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_CANCELLED,
)
from .writeback import info_gate, durability, sensitive_gate

# 合法状态转移（守护"纠正不污染"：CORRECTED 是终态，COMMITTED 不可回退）
_ALLOWED_FROM = {
    WAITING_CONFIRMATION: (TENTATIVE,),                 # 确认后 dispatch 失败 → 回退可重试
    TENTATIVE: (WAITING_CONFIRMATION,),                 # 低置信确认后才进 TENTATIVE
    COMMITTED: (TENTATIVE,),                            # 仅 TENTATIVE 可提交
    CORRECTED: (WAITING_CONFIRMATION, TENTATIVE, COMMITTED),  # 任意活跃态可被纠正
}

_TABLES = {"events", "messages", "route_decisions", "corrections", "write_jobs", "memory_items"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, platform_msg_id TEXT, kind TEXT,
  payload_json TEXT, received_at REAL, processed_at REAL);

CREATE TABLE IF NOT EXISTS messages(
  msg_id TEXT PRIMARY KEY, platform_msg_id TEXT UNIQUE, user_id TEXT,
  text TEXT, reply_to_msg_id TEXT, thread_id TEXT, mentions_json TEXT,
  attachments_json TEXT, chat_id TEXT, platform_ts INTEGER, created_at REAL);

CREATE TABLE IF NOT EXISTS route_decisions(
  decision_id TEXT PRIMARY KEY, msg_id TEXT, route_type TEXT, project_id INTEGER,
  referenced_id INTEGER, confidence_score REAL, needs_confirmation INTEGER,
  judge_raw TEXT, latency_ms INTEGER,
  state TEXT CHECK(state IN ('WAITING_CONFIRMATION','TENTATIVE','COMMITTED','CORRECTED')),
  dispatch_state TEXT CHECK(dispatch_state IN ('pending','sent','failed')),
  card_msg_id TEXT, parent_decision_id TEXT, committed_at REAL, created_at REAL);
-- 单活跃不变量（T-INV-3）：同一 msg_id 至多一个 active decision
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_decision ON route_decisions(msg_id)
  WHERE state IN ('WAITING_CONFIRMATION','TENTATIVE','COMMITTED');

CREATE TABLE IF NOT EXISTS corrections(
  correction_id TEXT PRIMARY KEY, decision_id TEXT, from_pid INTEGER, to_pid INTEGER,
  user_id TEXT, card_msg_id TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS write_jobs(
  job_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE, source_msg_id TEXT, due_at REAL,
  status TEXT CHECK(status IN ('pending','running','done','cancelled','failed')),
  attempts INTEGER, last_error TEXT, started_at REAL, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_jobs_status_due ON write_jobs(status, due_at);

CREATE TABLE IF NOT EXISTS memory_items(
  item_id TEXT PRIMARY KEY,
  scope_level TEXT CHECK(scope_level IN ('project','global','role')),
  project_id INTEGER, content TEXT, source_msg_id TEXT, decision_id TEXT UNIQUE,
  content_hash TEXT, state TEXT CHECK(state IN ('active','retired')),
  version INTEGER,
  durability TEXT CHECK(durability IN ('stable','tentative')),  -- v0.2 记忆价值门标签
  created_at REAL, retired_at REAL);
"""


def _id() -> str:
    return uuid.uuid4().hex          # 不截短：真相源主键不冒碰撞风险


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


class Store:
    """单进程使用。check_same_thread=False + busy_timeout 兜底偶发跨线程访问。"""

    def __init__(self, path: str = ":memory:", running_timeout_s: float = 300):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.running_timeout_s = running_timeout_s
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ---------- events：平台事件幂等（T-INV-1） ----------
    def record_event(self, event_id: str, platform_msg_id: str, kind: str,
                     payload: str = "", now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        try:
            self.db.execute(
                "INSERT INTO events(event_id,platform_msg_id,kind,payload_json,received_at) "
                "VALUES(?,?,?,?,?)", (event_id, platform_msg_id, kind, payload, now))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False                # 仅 event_id 主键冲突 = 重复事件（events 无其他约束）

    # ---------- messages：消息幂等（T-INV-2，INSERT-first 消除 TOCTOU） ----------
    def upsert_message(self, m: InboundMessage, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        try:
            self.db.execute(
                "INSERT INTO messages(msg_id,platform_msg_id,user_id,text,reply_to_msg_id,"
                "thread_id,mentions_json,attachments_json,chat_id,platform_ts,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (m.msg_id, m.platform_msg_id, m.user_id, m.text, m.reply_to_msg_id, m.thread_id,
                 json.dumps(m.mentions, ensure_ascii=False), json.dumps(m.attachments, ensure_ascii=False),
                 m.chat_id, m.platform_ts, now))
            self.db.commit()
            return m.msg_id
        except sqlite3.IntegrityError:  # platform_msg_id 已存在 → 返回既有
            row = self.db.execute(
                "SELECT msg_id FROM messages WHERE platform_msg_id=?", (m.platform_msg_id,)).fetchone()
            return row["msg_id"] if row else m.msg_id

    def get_message(self, msg_id: str):
        return self.db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()

    # ---------- decisions：单活跃不变量（T-INV-3） ----------
    def insert_decision(self, d: RouteDecision, now: float | None = None) -> None:
        """同 msg_id 已有 active decision → sqlite3.IntegrityError（partial unique）。"""
        now = now if now is not None else time.time()
        self.db.execute(
            "INSERT INTO route_decisions(decision_id,msg_id,route_type,project_id,referenced_id,"
            "confidence_score,needs_confirmation,judge_raw,latency_ms,state,dispatch_state,"
            "card_msg_id,parent_decision_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.decision_id, d.msg_id, d.route_type, d.project_id, d.referenced_id,
             d.confidence_score, int(d.needs_confirmation), d.judge_raw, d.latency_ms,
             d.state, d.dispatch_state, d.card_msg_id, d.parent_decision_id, now))
        self.db.commit()

    def get_decision(self, decision_id: str):
        return self.db.execute(
            "SELECT * FROM route_decisions WHERE decision_id=?", (decision_id,)).fetchone()

    def _transition_nc(self, decision_id: str, new_state: str, now: float) -> int:
        """条件状态迁移（不 commit）：仅当当前状态在 _ALLOWED_FROM[new_state] 中才更新。"""
        allowed = _ALLOWED_FROM.get(new_state, ())
        if not allowed:
            return 0
        ph = ",".join("?" * len(allowed))
        extra, args = "", [new_state]
        if new_state == COMMITTED:
            extra = ",committed_at=?"; args.append(now)
        args += list(allowed) + [decision_id]
        cur = self.db.execute(
            f"UPDATE route_decisions SET state=?{extra} WHERE state IN ({ph}) AND decision_id=?",
            args)
        return cur.rowcount

    def set_decision_state(self, decision_id: str, new_state: str, now: float | None = None) -> bool:
        """状态机转移，带合法性守护。返回是否成功（非法转移返回 False，状态不变）。"""
        now = now if now is not None else time.time()
        rc = self._transition_nc(decision_id, new_state, now)
        self.db.commit()
        return rc == 1

    def update_dispatch(self, decision_id: str, dispatch_state: str | None = None,
                        card_msg_id: str | None = None, now: float | None = None):
        """更新 dispatch_state / card_msg_id（普通字段，非状态机转移）。"""
        sets, args = [], []
        if dispatch_state is not None:
            sets.append("dispatch_state=?"); args.append(dispatch_state)
        if card_msg_id is not None:
            sets.append("card_msg_id=?"); args.append(card_msg_id)
        if not sets:
            return
        args.append(decision_id)
        self.db.execute(f"UPDATE route_decisions SET {','.join(sets)} WHERE decision_id=?", args)
        self.db.commit()

    def delete_decision(self, decision_id: str):
        """dispatch 失败（无副作用发生）时删除半落库 decision，释放单活跃锁以便重发。"""
        self.db.execute("DELETE FROM route_decisions WHERE decision_id=?", (decision_id,))
        self.db.commit()

    def set_decision_project(self, decision_id: str, project_id: int):
        """确认/纠正时更新归属项目（普通字段，非状态机转移）。"""
        self.db.execute(
            "UPDATE route_decisions SET project_id=? WHERE decision_id=?", (project_id, decision_id))
        self.db.commit()

    # ---------- corrections ----------
    def insert_correction(self, decision_id: str, from_pid, to_pid, user_id: str,
                          card_msg_id: str | None = None, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        cid = _id()
        self.db.execute(
            "INSERT INTO corrections(correction_id,decision_id,from_pid,to_pid,user_id,"
            "card_msg_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (cid, decision_id, from_pid, to_pid, user_id, card_msg_id, now))
        self.db.commit()
        return cid

    # ---------- write_jobs：任务幂等（T-INV-4）+ 条件领取 ----------
    def enqueue_write(self, decision_id: str, source_msg_id: str, due_at: float,
                      now: float | None = None) -> str | None:
        now = now if now is not None else time.time()
        try:
            jid = _id()
            self.db.execute(
                "INSERT INTO write_jobs(job_id,decision_id,source_msg_id,due_at,status,attempts,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (jid, decision_id, source_msg_id, due_at, JOB_PENDING, 0, now))
            self.db.commit()
            return jid
        except sqlite3.IntegrityError:
            return None                 # 同 decision_id 已入队，幂等

    def mark_job_running(self, job_id: str, now: float | None = None) -> bool:
        """仅 pending→running，返回是否抢占成功（防重复领取）。"""
        now = now if now is not None else time.time()
        cur = self.db.execute(
            "UPDATE write_jobs SET status=?,started_at=?,attempts=attempts+1 "
            "WHERE job_id=? AND status=?", (JOB_RUNNING, now, job_id, JOB_PENDING))
        self.db.commit()
        return cur.rowcount == 1

    def get_job_by_decision(self, decision_id: str):
        return self.db.execute(
            "SELECT * FROM write_jobs WHERE decision_id=?", (decision_id,)).fetchone()

    def cancel_write_job(self, decision_id: str) -> int:
        """纠正时即时取消未执行的写任务（pending/running → cancelled）。"""
        cur = self.db.execute(
            "UPDATE write_jobs SET status=? WHERE decision_id=? AND status IN (?,?)",
            (JOB_CANCELLED, decision_id, JOB_PENDING, JOB_RUNNING))
        self.db.commit()
        return cur.rowcount

    # ---------- memory：写入幂等（T-INV-5）+ retired 不召回（T-INV-7） ----------
    def _write_memory_nc(self, item: MemoryItem, now: float) -> str | None:
        """去重门(content_hash 精确)→ INSERT-first(decision_id 幂等)。不 commit。
        同项目同内容已 active → 返回既有 item_id（去重）；decision_id 冲突 → None。"""
        h = item.content_hash or _hash(item.content)
        dup = self.db.execute(
            "SELECT item_id FROM memory_items WHERE project_id=? AND content_hash=? AND state='active'",
            (item.project_id, h)).fetchone()
        if dup:
            return dup["item_id"]                 # 去重门：同项目同内容不重复沉淀
        iid = _id()
        try:
            self.db.execute(
                "INSERT INTO memory_items(item_id,scope_level,project_id,content,source_msg_id,"
                "decision_id,content_hash,state,version,durability,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (iid, item.scope_level, item.project_id, item.content, item.source_msg_id,
                 item.decision_id, h, item.state, item.version, item.durability, now))
            return iid
        except sqlite3.IntegrityError:
            return None

    def write_memory(self, item: MemoryItem, now: float | None = None) -> str | None:
        now = now if now is not None else time.time()
        iid = self._write_memory_nc(item, now)
        self.db.commit()
        if iid:
            return iid
        row = self.db.execute(
            "SELECT item_id FROM memory_items WHERE decision_id=?", (item.decision_id,)).fetchone()
        return row["item_id"] if row else None

    def recall(self, scope: dict, query: str = "", k: int = 5) -> list:
        """scope={'level':'project','project_id':N}；仅 active。
        project 级强制带 project_id，否则返回空（防跨项目召回污染）。"""
        level = scope.get("level", "project")
        if level == "project" and scope.get("project_id") is None:
            return []
        sql = "SELECT * FROM memory_items WHERE state='active' AND scope_level=?"
        args = [level]
        if scope.get("project_id") is not None:
            sql += " AND project_id=?"; args.append(scope["project_id"])
        if query:
            sql += " AND content LIKE ?"; args.append(f"%{query}%")
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(k)
        return self.db.execute(sql, args).fetchall()

    def retire(self, decision_id: str | None = None, source_msg_id: str | None = None,
               now: float | None = None) -> int:
        now = now if now is not None else time.time()
        if decision_id:
            cur = self.db.execute(
                "UPDATE memory_items SET state='retired',retired_at=? WHERE decision_id=? AND state='active'",
                (now, decision_id))
        elif source_msg_id:
            cur = self.db.execute(
                "UPDATE memory_items SET state='retired',retired_at=? WHERE source_msg_id=? AND state='active'",
                (now, source_msg_id))
        else:
            return 0
        self.db.commit()
        return cur.rowcount

    # ---------- 恢复扫描（T-REC-1~4）：每 job 单事务，含崩溃半状态补偿 ----------
    def recovery_scan(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        counts = {"committed": 0, "cancelled": 0, "retried": 0, "compensated": 0}
        due = self.db.execute(
            "SELECT * FROM write_jobs WHERE status=? AND due_at<=?", (JOB_PENDING, now)).fetchall()
        for job in due:
            with self.db:                       # 原子事务：commit+write+done 一起提交或一起回滚
                d = self.get_decision(job["decision_id"])
                if d is None:
                    self._set_job_nc(job["job_id"], JOB_CANCELLED); counts["cancelled"] += 1
                elif d["state"] == CORRECTED:
                    self._set_job_nc(job["job_id"], JOB_CANCELLED); counts["cancelled"] += 1
                elif d["state"] == TENTATIVE:
                    self._transition_nc(d["decision_id"], COMMITTED, now)
                    self._commit_write_nc(d, now)
                    self._set_job_nc(job["job_id"], JOB_DONE); counts["committed"] += 1
                elif d["state"] == COMMITTED:    # 崩溃补偿：decision 已提交但 job 未完成
                    self._commit_write_nc(d, now)   # write_memory 幂等，安全重入
                    self._set_job_nc(job["job_id"], JOB_DONE); counts["compensated"] += 1
                # WAITING_CONFIRMATION 不会有 write_job（dispatch 前不入队），故不出现（T-REC-4）
        running = self.db.execute(
            "SELECT * FROM write_jobs WHERE status=?", (JOB_RUNNING,)).fetchall()
        for job in running:
            if job["started_at"] is not None and (now - job["started_at"]) > self.running_timeout_s:
                with self.db:
                    self._set_job_nc(job["job_id"], JOB_PENDING); counts["retried"] += 1
        return counts

    def _set_job_nc(self, job_id: str, status: str):
        self.db.execute("UPDATE write_jobs SET status=? WHERE job_id=?", (status, job_id))

    def _commit_write_nc(self, d_row, now: float):
        """写入门链（项目内沉淀，不 commit）：作用域门（有归属项目）+ 信息量门（info_gate）
        → 去重门在 _write_memory_nc。"""
        m = self.get_message(d_row["msg_id"])
        if not m or d_row["project_id"] is None:
            return                                    # 作用域门：须有归属项目
        if not info_gate(m["text"]):
            return                                    # 信息量门：零信号/空/极短不沉淀
        if sensitive_gate(m["text"]):
            return                                    # 敏感信息门：含凭据/密钥/私钥不沉淀
        self._write_memory_nc(MemoryItem(
            scope_level="project", project_id=d_row["project_id"], content=m["text"],
            source_msg_id=d_row["msg_id"], decision_id=d_row["decision_id"],
            content_hash=_hash(m["text"]), durability=durability(m["text"])), now)

    # ---------- 测试/运维辅助 ----------
    def count(self, table: str) -> int:
        if table not in _TABLES:
            raise ValueError(f"unknown table: {table}")
        return self.db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    def close(self):
        self.db.close()
