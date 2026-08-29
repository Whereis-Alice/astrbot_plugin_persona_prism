"""SQLite 持久层。

上游用 JSON 文件按 user_id 全局键控存画像，导致同一个人在 A 群的画像会
被 B 群的结果覆盖，而且只留最新一条。这里改成 SQLite：

* 画像按 (平台, 群, 用户) 分区并保留历史多条；
* 群名 / 用户名一起落库，WebUI 才能显示"人话"而不是一串 ID；
* 语料独立成表并按 message_id 唯一约束，天然幂等；
* 退出名单、每日配额、扫描游标、自定义提示词都在同一个库里，备份只要一个文件。

sqlite3 是标准库，插件因此不需要任何额外的存储依赖。所有阻塞调用都
包在 asyncio.to_thread 里，不会卡住事件循环。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from .models import CorpusMessage, PortraitRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS portraits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL DEFAULT '',
    umo           TEXT NOT NULL DEFAULT '',
    group_id      TEXT NOT NULL DEFAULT '',
    group_name    TEXT NOT NULL DEFAULT '',
    user_id       TEXT NOT NULL DEFAULT '',
    user_name     TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'portrait',
    kind_label    TEXT NOT NULL DEFAULT '',
    theme         TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    text          TEXT NOT NULL DEFAULT '',
    sample_size   INTEGER NOT NULL DEFAULT 0,
    corpus_chars  INTEGER NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL DEFAULT 0,
    model         TEXT NOT NULL DEFAULT '',
    card_file     TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_portraits_scope
    ON portraits (platform, group_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_portraits_time ON portraits (created_at DESC);

CREATE TABLE IF NOT EXISTS corpus (
    platform    TEXT NOT NULL DEFAULT '',
    group_id    TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    user_name   TEXT NOT NULL DEFAULT '',
    message_id  TEXT NOT NULL,
    ts          INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT '',
    is_reply    INTEGER NOT NULL DEFAULT 0,
    -- 被回复消息的 message_id（用来反推「谁被回复了」，恋爱成分要用）
    reply_to    TEXT NOT NULL DEFAULT '',
    -- 这条消息里的图片数量
    images      INTEGER NOT NULL DEFAULT 0,
    -- 这条消息 @ 到的 user_id，逗号分隔
    at_ids      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (platform, group_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_user
    ON corpus (platform, group_id, user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_corpus_ts ON corpus (ts);

-- 只能从 notice 事件拿到的互动计数，按「平台+群+人+日」聚合。
-- 消息类指标（发言数 / 回复 / 艾特 / 图片…）一律从 corpus 现算，不在这里双写。
CREATE TABLE IF NOT EXISTS interactions (
    platform          TEXT NOT NULL DEFAULT '',
    group_id          TEXT NOT NULL DEFAULT '',
    user_id           TEXT NOT NULL DEFAULT '',
    day               TEXT NOT NULL DEFAULT '',
    poke_sent         INTEGER NOT NULL DEFAULT 0,
    poke_received     INTEGER NOT NULL DEFAULT 0,
    reaction_sent     INTEGER NOT NULL DEFAULT 0,
    reaction_received INTEGER NOT NULL DEFAULT 0,
    recall_count      INTEGER NOT NULL DEFAULT 0,
    -- 当天最后一次算出的恋爱成分综合分，-1 表示还没算过。只用来做趋势提示。
    love_total        INTEGER NOT NULL DEFAULT -1,
    updated_at        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, group_id, user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_interactions_day ON interactions (day);

CREATE TABLE IF NOT EXISTS groups_meta (
    platform    TEXT NOT NULL DEFAULT '',
    group_id    TEXT NOT NULL DEFAULT '',
    group_name  TEXT NOT NULL DEFAULT '',
    theme       TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, group_id)
);

CREATE TABLE IF NOT EXISTS scan_state (
    platform     TEXT NOT NULL DEFAULT '',
    group_id     TEXT NOT NULL DEFAULT '',
    oldest_seq   TEXT NOT NULL DEFAULT '',
    newest_seq   TEXT NOT NULL DEFAULT '',
    exhausted    INTEGER NOT NULL DEFAULT 0,
    last_scan    INTEGER NOT NULL DEFAULT 0,
    -- 这个群实测可用的翻页游标字段（message_seq / message_id），空串表示还没试出来。
    cursor_field TEXT NOT NULL DEFAULT '',
    -- 累计成功往前翻过的页数，用来回答「到底挖了多深」。
    depth_pages  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, group_id)
);

CREATE TABLE IF NOT EXISTS optouts (
    platform    TEXT NOT NULL DEFAULT '',
    group_id    TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    user_name   TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT 'self',
    created_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, group_id, user_id)
);

CREATE TABLE IF NOT EXISTS quota (
    day       TEXT NOT NULL,
    group_id  TEXT NOT NULL DEFAULT '',
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, group_id)
);

CREATE TABLE IF NOT EXISTS prompt_entries (
    key         TEXT PRIMARY KEY,
    command     TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL DEFAULT '',
    structured  INTEGER NOT NULL DEFAULT 1,
    layout      TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1,
    backend     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_time ON runs (created_at DESC);
"""


def _now() -> int:
    return int(time.time())


class PrismStore:
    """人格棱镜的全部持久化状态。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    #: 首版之后追加的列。sqlite 的 ALTER TABLE 没有 IF NOT EXISTS，只能先探再加。
    _MIGRATIONS: ClassVar[dict[str, tuple[tuple[str, str], ...]]] = {
        "prompt_entries": (("layout", "TEXT NOT NULL DEFAULT ''"),),
        "scan_state": (
            ("cursor_field", "TEXT NOT NULL DEFAULT ''"),
            ("depth_pages", "INTEGER NOT NULL DEFAULT 0"),
        ),
        "corpus": (
            ("reply_to", "TEXT NOT NULL DEFAULT ''"),
            ("images", "INTEGER NOT NULL DEFAULT 0"),
            ("at_ids", "TEXT NOT NULL DEFAULT ''"),
        ),
    }

    def _migrate(self) -> None:
        """给旧版本建好的库补上新增列，必要时做一次性数据自愈。

        调用方需已持有 self._lock。
        """
        added: set[tuple[str, str]] = set()
        for table, columns in self._MIGRATIONS.items():
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            existing = {str(row["name"]) for row in rows}
            for name, ddl in columns:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    added.add((table, name))
        if ("scan_state", "cursor_field") in added:
            #: v1.1.2 及更早版本只用 message_seq 当游标。在部分协议端上这个值不被
            #: get_group_msg_history 接受，第二页会原地返回同一批消息，被旧代码误判成
            #: 「已挖到群历史尽头」并永久写进 exhausted，此后每次画像都只补拉最新一页。
            #: 升级时把所有断点清零，让这些群重新从最新一页开始自适应地往前挖。
            self._conn.execute(
                "UPDATE scan_state SET exhausted = 0, oldest_seq = '', depth_pages = 0",
            )

    # -- 基础设施 -----------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def _write(self, statements: Iterable[tuple[str, Sequence[Any]]]) -> None:
        with self._lock:
            for sql, params in statements:
                self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        rows = self._query(sql, params)
        if not rows:
            return default
        value = rows[0][0]
        return default if value is None else value

    # ==================================================================
    # 语料
    # ==================================================================
    def add_messages(
        self,
        platform: str,
        group_id: str,
        rows: Sequence[CorpusMessage],
    ) -> int:
        """幂等写入语料。message_id 冲突时刷新用户名，并给老语料补齐新增列。"""
        if not rows:
            return 0
        payload = [
            (
                platform,
                group_id,
                msg.user_id,
                msg.user_name,
                msg.message_id,
                msg.ts,
                msg.text,
                1 if msg.is_reply else 0,
                msg.reply_to,
                max(0, int(msg.images or 0)),
                msg.at_ids,
            )
            for msg in rows
            if msg.message_id and msg.user_id
        ]
        if not payload:
            return 0
        with self._lock:
            before = self._conn.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
            self._conn.executemany(
                """
                INSERT INTO corpus
                    (platform, group_id, user_id, user_name, message_id, ts, text,
                     is_reply, reply_to, images, at_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, group_id, message_id) DO UPDATE SET
                    user_name = CASE
                        WHEN excluded.user_name != '' THEN excluded.user_name
                        ELSE corpus.user_name
                    END,
                    -- 老版本入库的语料没有这三列，重新扫到时顺手补上
                    reply_to = CASE
                        WHEN excluded.reply_to != '' THEN excluded.reply_to
                        ELSE corpus.reply_to
                    END,
                    images = MAX(corpus.images, excluded.images),
                    at_ids = CASE
                        WHEN excluded.at_ids != '' THEN excluded.at_ids
                        ELSE corpus.at_ids
                    END
                """,
                payload,
            )
            after = self._conn.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
            self._conn.commit()
        return max(0, after - before)

    def fetch_user_corpus(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        limit: int = 4000,
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT message_id, user_id, user_name, text, ts, is_reply, reply_to, images, at_ids
            FROM corpus
            WHERE platform = ? AND group_id = ? AND user_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (platform, group_id, user_id, max(1, limit)),
        )
        return [
            {
                "message_id": row["message_id"],
                "user_id": row["user_id"],
                "user_name": row["user_name"],
                "text": row["text"],
                "ts": row["ts"],
                "is_reply": bool(row["is_reply"]),
                "reply_to": row["reply_to"],
                "images": int(row["images"] or 0),
                "at_ids": row["at_ids"],
            }
            for row in reversed(rows)
        ]

    def corpus_stats(self, platform: str = "", group_id: str = "") -> dict[str, int]:
        where, params = _scope_where(platform, group_id)
        row = self._query(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT user_id) AS users,
                   COALESCE(MIN(ts), 0) AS oldest,
                   COALESCE(MAX(ts), 0) AS newest
            FROM corpus {where}
            """,
            params,
        )[0]
        return {
            "total": int(row["total"] or 0),
            "users": int(row["users"] or 0),
            "oldest": int(row["oldest"] or 0),
            "newest": int(row["newest"] or 0),
        }

    def message_owner(self, platform: str, group_id: str, message_id: str) -> str:
        """按消息 ID 反查作者。

        上游为此单独维护了一张 message_owner_index 表，从不清理，会无限膨胀；
        语料库本身就存了 message_id，直接查即可，也跟着语料的保留策略一起过期。
        """
        if not message_id:
            return ""
        return str(
            self._scalar(
                "SELECT user_id FROM corpus WHERE platform = ? AND group_id = ? AND message_id = ?",
                (platform, group_id, str(message_id)),
                "",
            ),
        )

    def latest_user_name(self, platform: str, group_id: str, user_id: str) -> str:
        return str(
            self._scalar(
                """
                SELECT user_name FROM corpus
                WHERE platform = ? AND group_id = ? AND user_id = ? AND user_name != ''
                ORDER BY ts DESC LIMIT 1
                """,
                (platform, group_id, user_id),
                "",
            ),
        )

    def prune_corpus(
        self,
        *,
        retention_days: int = 30,
        max_per_group: int = 20000,
    ) -> int:
        """按保留天数 + 每群条数上限清理语料。返回删除条数。"""
        removed = 0
        with self._lock:
            if retention_days > 0:
                cutoff = _now() - retention_days * 86400
                cur = self._conn.execute(
                    "DELETE FROM corpus WHERE ts > 0 AND ts < ?",
                    (cutoff,),
                )
                removed += cur.rowcount or 0
            if max_per_group > 0:
                groups = self._conn.execute(
                    """
                    SELECT platform, group_id, COUNT(*) AS n FROM corpus
                    GROUP BY platform, group_id HAVING n > ?
                    """,
                    (max_per_group,),
                ).fetchall()
                for row in groups:
                    excess = int(row["n"]) - max_per_group
                    cur = self._conn.execute(
                        """
                        DELETE FROM corpus
                        WHERE rowid IN (
                            SELECT rowid FROM corpus
                            WHERE platform = ? AND group_id = ?
                            ORDER BY ts ASC LIMIT ?
                        )
                        """,
                        (row["platform"], row["group_id"], excess),
                    )
                    removed += cur.rowcount or 0
            self._conn.commit()
        return removed

    def clear_user_corpus(self, platform: str, group_id: str, user_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM corpus WHERE platform = ? AND group_id = ? AND user_id = ?",
                (platform, group_id, user_id),
            )
            self._conn.execute(
                "DELETE FROM interactions WHERE platform = ? AND group_id = ? AND user_id = ?",
                (platform, group_id, user_id),
            )
            self._conn.commit()
        return cur.rowcount or 0

    def clear_group_corpus(self, platform: str, group_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM corpus WHERE platform = ? AND group_id = ?",
                (platform, group_id),
            )
            self._conn.execute(
                "DELETE FROM scan_state WHERE platform = ? AND group_id = ?",
                (platform, group_id),
            )
            self._conn.execute(
                "DELETE FROM interactions WHERE platform = ? AND group_id = ?",
                (platform, group_id),
            )
            self._conn.commit()
        return cur.rowcount or 0

    # ==================================================================
    # 互动计数（恋爱成分用）
    # ==================================================================
    #: 允许累加的计数列。白名单挡住 SQL 拼接风险。
    INTERACTION_FIELDS: ClassVar[tuple[str, ...]] = (
        "poke_sent",
        "poke_received",
        "reaction_sent",
        "reaction_received",
        "recall_count",
    )

    def bump_interaction(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        day: str,
        field: str,
        delta: int = 1,
    ) -> None:
        """给某人某天的互动计数加数。字段名必须在白名单里。"""
        if not (group_id and user_id and day) or delta == 0:
            return
        if field not in self.INTERACTION_FIELDS:
            msg = f"unknown interaction field: {field}"
            raise ValueError(msg)
        self._write([
            (
                f"""
                INSERT INTO interactions (platform, group_id, user_id, day, {field}, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, group_id, user_id, day) DO UPDATE SET
                    {field} = interactions.{field} + excluded.{field},
                    updated_at = excluded.updated_at
                """,
                (platform, group_id, user_id, day, int(delta), _now()),
            ),
        ])

    def interaction_counts(
        self,
        platform: str,
        group_id: str,
        day: str,
    ) -> dict[str, dict[str, int]]:
        """取本群某天所有人的互动计数。"""
        rows = self._query(
            """
            SELECT user_id, poke_sent, poke_received, reaction_sent,
                   reaction_received, recall_count
            FROM interactions
            WHERE platform = ? AND group_id = ? AND day = ?
            """,
            (platform, group_id, day),
        )
        return {
            str(row["user_id"]): {name: int(row[name] or 0) for name in self.INTERACTION_FIELDS}
            for row in rows
        }

    def set_love_total(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        day: str,
        total: int,
    ) -> None:
        """记下当天算出的综合分，明天用来做趋势提示。"""
        if not (group_id and user_id and day):
            return
        self._write([
            (
                """
                INSERT INTO interactions (platform, group_id, user_id, day, love_total, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, group_id, user_id, day) DO UPDATE SET
                    love_total = excluded.love_total,
                    updated_at = excluded.updated_at
                """,
                (platform, group_id, user_id, day, int(total), _now()),
            ),
        ])

    def love_total(self, platform: str, group_id: str, user_id: str, day: str) -> int | None:
        value = self._scalar(
            """
            SELECT love_total FROM interactions
            WHERE platform = ? AND group_id = ? AND user_id = ? AND day = ?
            """,
            (platform, group_id, user_id, day),
            -1,
        )
        total = int(value)
        return None if total < 0 else total

    def window_rows(
        self,
        platform: str,
        group_id: str,
        start_ts: int,
        end_ts: int,
        limit: int = 20000,
    ) -> list[dict[str, Any]]:
        """取一个时间窗内本群的全部语料，供恋爱成分现算。"""
        rows = self._query(
            """
            SELECT message_id, user_id, user_name, text, ts, is_reply, reply_to, images, at_ids
            FROM corpus
            WHERE platform = ? AND group_id = ? AND ts >= ? AND ts < ?
            ORDER BY ts ASC
            LIMIT ?
            """,
            (platform, group_id, int(start_ts), int(end_ts), max(1, limit)),
        )
        return [
            {
                "message_id": row["message_id"],
                "user_id": row["user_id"],
                "user_name": row["user_name"],
                "text": row["text"],
                "ts": int(row["ts"] or 0),
                "is_reply": bool(row["is_reply"]),
                "reply_to": row["reply_to"],
                "images": int(row["images"] or 0),
                "at_ids": row["at_ids"],
            }
            for row in rows
        ]

    def prune_interactions(self, *, retention_days: int = 30) -> int:
        """按天清理互动计数。"""
        if retention_days <= 0:
            return 0
        cutoff = time.strftime("%Y-%m-%d", time.localtime(_now() - retention_days * 86400))
        with self._lock:
            cur = self._conn.execute("DELETE FROM interactions WHERE day < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount or 0
    # ==================================================================
    # 群元信息 / 主题
    # ==================================================================
    def touch_group(self, platform: str, group_id: str, group_name: str = "") -> None:
        if not group_id:
            return
        self._write(
            [
                (
                    """
                    INSERT INTO groups_meta (platform, group_id, group_name, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(platform, group_id) DO UPDATE SET
                        group_name = CASE
                            WHEN excluded.group_name != '' THEN excluded.group_name
                            ELSE groups_meta.group_name
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (platform, group_id, group_name, _now()),
                ),
            ],
        )

    def group_name(self, platform: str, group_id: str) -> str:
        return str(
            self._scalar(
                "SELECT group_name FROM groups_meta WHERE platform = ? AND group_id = ?",
                (platform, group_id),
                "",
            ),
        )

    def group_theme(self, platform: str, group_id: str) -> str:
        return str(
            self._scalar(
                "SELECT theme FROM groups_meta WHERE platform = ? AND group_id = ?",
                (platform, group_id),
                "",
            ),
        )

    def set_group_theme(self, platform: str, group_id: str, theme: str) -> None:
        self._write(
            [
                (
                    """
                    INSERT INTO groups_meta (platform, group_id, theme, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(platform, group_id) DO UPDATE SET
                        theme = excluded.theme, updated_at = excluded.updated_at
                    """,
                    (platform, group_id, theme, _now()),
                ),
            ],
        )

    # ==================================================================
    # 扫描游标
    # ==================================================================
    def get_scan_state(self, platform: str, group_id: str) -> dict[str, Any]:
        rows = self._query(
            "SELECT * FROM scan_state WHERE platform = ? AND group_id = ?",
            (platform, group_id),
        )
        if not rows:
            return {
                "oldest_seq": "",
                "newest_seq": "",
                "exhausted": False,
                "last_scan": 0,
                "cursor_field": "",
                "depth_pages": 0,
            }
        row = rows[0]
        return {
            "oldest_seq": row["oldest_seq"] or "",
            "newest_seq": row["newest_seq"] or "",
            "exhausted": bool(row["exhausted"]),
            "last_scan": int(row["last_scan"] or 0),
            "cursor_field": row["cursor_field"] or "",
            "depth_pages": int(row["depth_pages"] or 0),
        }

    def set_scan_state(
        self,
        platform: str,
        group_id: str,
        *,
        oldest_seq: str = "",
        newest_seq: str = "",
        exhausted: bool = False,
        cursor_field: str = "",
        depth_pages: int = -1,
    ) -> None:
        """写回一个群的回溯断点。

        空串 / 负数表示"保持原值"，这样调用方可以只更新自己关心的那几个字段。
        exhausted 是唯一每次都覆盖的字段——它代表"本次判断"，不该被历史值粘住。
        """
        self._write(
            [
                (
                    """
                    INSERT INTO scan_state
                        (platform, group_id, oldest_seq, newest_seq, exhausted,
                         last_scan, cursor_field, depth_pages)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, group_id) DO UPDATE SET
                        oldest_seq = CASE
                            WHEN excluded.oldest_seq != '' THEN excluded.oldest_seq
                            ELSE scan_state.oldest_seq
                        END,
                        newest_seq = CASE
                            WHEN excluded.newest_seq != '' THEN excluded.newest_seq
                            ELSE scan_state.newest_seq
                        END,
                        exhausted = excluded.exhausted,
                        last_scan = excluded.last_scan,
                        cursor_field = CASE
                            WHEN excluded.cursor_field != '' THEN excluded.cursor_field
                            ELSE scan_state.cursor_field
                        END,
                        depth_pages = CASE
                            WHEN excluded.depth_pages >= 0 THEN excluded.depth_pages
                            ELSE scan_state.depth_pages
                        END
                    """,
                    (
                        platform,
                        group_id,
                        oldest_seq,
                        newest_seq,
                        1 if exhausted else 0,
                        _now(),
                        cursor_field,
                        depth_pages,
                    ),
                ),
            ],
        )

    def reset_scan_state(self, platform: str = "", group_id: str = "") -> int:
        """清掉回溯断点，让下次画像从最新一页重新往前挖。

        给「棱镜重扫」用：万一某个群的断点被协议端的怪异行为带歪了（或者用户换了
        协议端 / 机器人重新入群），不需要删整个库就能重新开始。不传参数表示全部清空。
        """
        where, params = _scope_where(platform, group_id)
        rows = self._query(f"SELECT COUNT(*) FROM scan_state{where}", params)
        removed = int(rows[0][0] or 0) if rows else 0
        self._write([(f"DELETE FROM scan_state{where}", params)])
        return removed

    # ==================================================================
    # 画像记录
    # ==================================================================
    def save_portrait(self, record: PortraitRecord, *, history_limit: int = 20) -> int:
        created = record.created_at or _now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO portraits (
                    platform, umo, group_id, group_name, user_id, user_name,
                    kind, kind_label, theme, payload_json, text,
                    sample_size, corpus_chars, confidence, model, card_file, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.platform,
                    record.umo,
                    record.group_id,
                    record.group_name,
                    record.user_id,
                    record.user_name,
                    record.kind,
                    record.kind_label,
                    record.theme,
                    json.dumps(record.payload, ensure_ascii=False),
                    record.text,
                    record.sample_size,
                    record.corpus_chars,
                    record.confidence,
                    record.model,
                    record.card_file,
                    created,
                ),
            )
            new_id = int(cur.lastrowid or 0)
            if history_limit > 0:
                self._conn.execute(
                    """
                    DELETE FROM portraits WHERE id IN (
                        SELECT id FROM portraits
                        WHERE platform = ? AND group_id = ? AND user_id = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (record.platform, record.group_id, record.user_id, history_limit),
                )
            self._conn.commit()
        return new_id

    def attach_card(self, record_id: int, card_file: str) -> None:
        self._write(
            [("UPDATE portraits SET card_file = ? WHERE id = ?", (card_file, record_id))],
        )

    def latest_portrait(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        kind: str = "",
    ) -> PortraitRecord | None:
        sql = """
            SELECT * FROM portraits
            WHERE platform = ? AND group_id = ? AND user_id = ?
        """
        params: list[Any] = [platform, group_id, user_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        rows = self._query(sql, params)
        return PortraitRecord.from_row(rows[0]) if rows else None

    def recent_themes(self, platform: str, group_id: str, limit: int = 2) -> list[str]:
        """本群最近几张卡用过的主题，最近的在前。自动挡靠它避免连着撞主题。"""
        rows = self._query(
            """
            SELECT theme FROM portraits
            WHERE platform = ? AND group_id = ? AND theme != ''
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (platform, group_id, max(1, limit)),
        )
        return [str(row["theme"]) for row in rows]
    def user_history(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[PortraitRecord]:
        rows = self._query(
            """
            SELECT * FROM portraits
            WHERE platform = ? AND group_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (platform, group_id, user_id, max(1, limit)),
        )
        return [PortraitRecord.from_row(row) for row in rows]

    def list_records(
        self,
        *,
        group_id: str = "",
        user_id: str = "",
        kind: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 30,
    ) -> tuple[list[PortraitRecord], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if query:
            clauses.append(
                "(user_name LIKE ? OR user_id LIKE ? OR group_name LIKE ? OR group_id LIKE ? OR text LIKE ?)",
            )
            like = f"%{query}%"
            params.extend([like] * 5)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = int(self._scalar(f"SELECT COUNT(*) FROM portraits{where}", params, 0))
        rows = self._query(
            f"""
            SELECT * FROM portraits{where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, max(1, min(200, limit)), max(0, offset)],
        )
        return [PortraitRecord.from_row(row) for row in rows], total

    def get_record(self, record_id: int) -> PortraitRecord | None:
        rows = self._query("SELECT * FROM portraits WHERE id = ?", (record_id,))
        return PortraitRecord.from_row(rows[0]) if rows else None

    def delete_record(self, record_id: int) -> str:
        """删除一条画像，返回它的卡片文件名（供调用方顺手删图）。"""
        record = self.get_record(record_id)
        if record is None:
            return ""
        self._write([("DELETE FROM portraits WHERE id = ?", (record_id,))])
        return record.card_file

    def purge_records(self, *, group_id: str = "", user_id: str = "") -> list[str]:
        clauses: list[str] = []
        params: list[Any] = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cards = [
            str(row["card_file"])
            for row in self._query(f"SELECT card_file FROM portraits{where}", params)
            if row["card_file"]
        ]
        self._write([(f"DELETE FROM portraits{where}", params)])
        return cards

    def group_tree(self) -> list[dict[str, Any]]:
        """WebUI 记录页需要的 群 → 用户 两级目录（带名称）。"""
        rows = self._query(
            """
            SELECT p.group_id AS group_id,
                   COALESCE(NULLIF(p.group_name, ''), g.group_name, '') AS group_name,
                   p.user_id AS user_id,
                   p.user_name AS user_name,
                   COUNT(*) AS total,
                   MAX(p.created_at) AS latest
            FROM portraits p
            LEFT JOIN groups_meta g
                ON g.platform = p.platform AND g.group_id = p.group_id
            GROUP BY p.group_id, p.user_id
            ORDER BY latest DESC
            """,
        )
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            gid = row["group_id"] or ""
            bucket = buckets.setdefault(
                gid,
                {
                    "group_id": gid,
                    "group_name": row["group_name"] or ("私聊" if not gid else f"群 {gid}"),
                    "total": 0,
                    "latest": 0,
                    "members": [],
                },
            )
            if row["group_name"]:
                bucket["group_name"] = row["group_name"]
            bucket["total"] += int(row["total"] or 0)
            bucket["latest"] = max(bucket["latest"], int(row["latest"] or 0))
            bucket["members"].append(
                {
                    "user_id": row["user_id"] or "",
                    "user_name": row["user_name"] or row["user_id"] or "未知用户",
                    "total": int(row["total"] or 0),
                    "latest": int(row["latest"] or 0),
                },
            )
        tree = list(buckets.values())
        for bucket in tree:
            bucket["members"].sort(key=lambda m: m["latest"], reverse=True)
        tree.sort(key=lambda b: b["latest"], reverse=True)
        return tree

    def overview(self) -> dict[str, Any]:
        day_start = _now() - 86400
        kinds = [
            {"kind": row["kind"], "total": int(row["n"])}
            for row in self._query(
                "SELECT kind, COUNT(*) AS n FROM portraits GROUP BY kind ORDER BY n DESC",
            )
        ]
        runs = self._query(
            """
            SELECT COUNT(*) AS total, SUM(ok) AS ok_count,
                   COALESCE(AVG(elapsed_ms), 0) AS avg_ms
            FROM runs WHERE created_at >= ?
            """,
            (_now() - 7 * 86400,),
        )[0]
        total_runs = int(runs["total"] or 0)
        ok_runs = int(runs["ok_count"] or 0)
        return {
            "portraits": int(self._scalar("SELECT COUNT(*) FROM portraits", (), 0)),
            "groups": int(
                self._scalar("SELECT COUNT(DISTINCT group_id) FROM portraits", (), 0),
            ),
            "users": int(
                self._scalar("SELECT COUNT(DISTINCT user_id) FROM portraits", (), 0),
            ),
            "today": int(
                self._scalar(
                    "SELECT COUNT(*) FROM portraits WHERE created_at >= ?",
                    (day_start,),
                    0,
                ),
            ),
            "corpus": self.corpus_stats(),
            "kinds": kinds,
            "optouts": int(self._scalar("SELECT COUNT(*) FROM optouts", (), 0)),
            "runs_7d": total_runs,
            "success_rate": (ok_runs / total_runs) if total_runs else 1.0,
            "avg_elapsed_ms": int(runs["avg_ms"] or 0),
        }

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "kind": row["kind"],
                "ok": bool(row["ok"]),
                "backend": row["backend"],
                "error": row["error"],
                "elapsed_ms": int(row["elapsed_ms"] or 0),
                "created_at": int(row["created_at"] or 0),
            }
            for row in self._query(
                "SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(200, limit)),),
            )
        ]

    def log_run(
        self,
        *,
        group_id: str,
        user_id: str,
        kind: str,
        ok: bool,
        backend: str = "",
        error: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        self._write(
            [
                (
                    """
                    INSERT INTO runs
                        (group_id, user_id, kind, ok, backend, error, elapsed_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        user_id,
                        kind,
                        1 if ok else 0,
                        backend,
                        error[:400],
                        elapsed_ms,
                        _now(),
                    ),
                ),
                (
                    "DELETE FROM runs WHERE id IN ("
                    " SELECT id FROM runs ORDER BY created_at DESC, id DESC"
                    " LIMIT -1 OFFSET 500)",
                    (),
                ),
            ],
        )

    # ==================================================================
    # 退出名单
    # ==================================================================
    def add_optout(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        user_name: str = "",
        reason: str = "self",
    ) -> None:
        self._write(
            [
                (
                    """
                    INSERT INTO optouts
                        (platform, group_id, user_id, user_name, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, group_id, user_id) DO UPDATE SET
                        user_name = excluded.user_name, reason = excluded.reason
                    """,
                    (platform, group_id, user_id, user_name, reason, _now()),
                ),
            ],
        )

    def remove_optout(self, platform: str, group_id: str, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM optouts WHERE platform = ? AND group_id = ? AND user_id = ?",
                (platform, group_id, user_id),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    def is_opted_out(self, platform: str, group_id: str, user_id: str) -> bool:
        return bool(
            self._query(
                "SELECT 1 FROM optouts WHERE platform = ? AND group_id = ? AND user_id = ?",
                (platform, group_id, user_id),
            ),
        )

    def opted_out_ids(self, platform: str, group_id: str) -> list[str]:
        """本群所有退出统计的人。榜单类玩法要整体排除他们。"""
        return [
            str(row["user_id"])
            for row in self._query(
                "SELECT user_id FROM optouts WHERE platform = ? AND group_id = ?",
                (platform, group_id),
            )
        ]

    def list_optouts(self) -> list[dict[str, Any]]:
        return [
            {
                "platform": row["platform"],
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "user_name": row["user_name"] or row["user_id"],
                "reason": row["reason"],
                "created_at": int(row["created_at"] or 0),
            }
            for row in self._query("SELECT * FROM optouts ORDER BY created_at DESC")
        ]

    # ==================================================================
    # 每日配额
    # ==================================================================
    def bump_quota(self, group_id: str, day: str) -> int:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO quota (day, group_id, count) VALUES (?, ?, 1)
                ON CONFLICT(day, group_id) DO UPDATE SET count = quota.count + 1
                """,
                (day, group_id),
            )
            self._conn.execute("DELETE FROM quota WHERE day < ?", (day,))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM quota WHERE day = ? AND group_id = ?",
                (day, group_id),
            ).fetchone()
        return int(row["count"]) if row else 1

    def quota_used(self, group_id: str, day: str) -> int:
        return int(
            self._scalar(
                "SELECT count FROM quota WHERE day = ? AND group_id = ?",
                (day, group_id),
                0,
            ),
        )

    def release_quota(self, group_id: str, day: str) -> None:
        """分析失败时把配额还回去，别让用户白吃一次额度。"""
        self._write(
            [
                (
                    "UPDATE quota SET count = MAX(0, count - 1) WHERE day = ? AND group_id = ?",
                    (day, group_id),
                ),
            ],
        )

    # ==================================================================
    # 自定义提示词
    # ==================================================================
    def list_prompt_entries(self) -> list[dict[str, Any]]:
        return [
            {
                "key": row["key"],
                "command": row["command"],
                "label": row["label"],
                "prompt": row["prompt"],
                "structured": bool(row["structured"]),
                "layout": row["layout"] or "",
                "enabled": bool(row["enabled"]),
                "updated_at": int(row["updated_at"] or 0),
            }
            for row in self._query("SELECT * FROM prompt_entries ORDER BY key")
        ]

    def upsert_prompt_entry(
        self,
        key: str,
        *,
        command: str,
        label: str,
        prompt: str,
        structured: bool = True,
        layout: str = "",
        enabled: bool = True,
    ) -> None:
        self._write(
            [
                (
                    """
                    INSERT INTO prompt_entries
                        (key, command, label, prompt, structured, layout, enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        command = excluded.command,
                        label = excluded.label,
                        prompt = excluded.prompt,
                        structured = excluded.structured,
                        layout = excluded.layout,
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        command,
                        label,
                        prompt,
                        1 if structured else 0,
                        layout,
                        1 if enabled else 0,
                        _now(),
                    ),
                ),
            ],
        )

    def delete_prompt_entry(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM prompt_entries WHERE key = ?", (key,))
            self._conn.commit()
        return bool(cur.rowcount)


def _scope_where(platform: str, group_id: str) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if group_id:
        clauses.append("group_id = ?")
        params.append(group_id)
    return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params


class AsyncStore:
    """把 PrismStore 的同步方法统一包成 to_thread 协程。

    用 __getattr__ 动态代理，省掉几十个一模一样的 async 包装函数。
    """

    __slots__ = ("sync",)

    def __init__(self, store: PrismStore) -> None:
        self.sync = store

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.sync, name)
        if not callable(target):
            return target

        async def runner(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(target, *args, **kwargs)

        runner.__name__ = name
        return runner
