# -*- coding: utf-8 -*-
"""
自习室座位监测 - 后端 API（多教室版）

接口:
    POST /api/rooms          注册教室（camera_id + 名称 + 座位总数）
    GET  /api/rooms          教室列表
    DELETE /api/rooms/{cid}  删除教室
    GET  /api/overview       多教室实时总览（监控中心卡片墙数据）
    POST /api/report         edge_agent 上报采样数据（人数/座位状态）
    GET  /api/stats          实时统计（?camera_id= 指定教室）
    GET  /api/seats          座位列表与当前占用
    GET  /api/history        历史曲线（?hours=24&camera_id=）
    GET  /api/daily          按小时聚合的日报数据（?camera_id=）

本地启动: uvicorn server:app --reload --port 8000
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional

import pymysql
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ---------- 配置 ----------
DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "root"),
    "database": "study_room",
    "charset": "utf8mb4",
}

app = FastAPI(title="自习室座位监测系统", version="0.1.0")


def get_db():
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)


# ---------- 建表（应用启动时自动执行） ----------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rooms (
    camera_id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    total_seats INT NOT NULL DEFAULT 0,
    location VARCHAR(128) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 旧表补充 location 列（幂等: MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS, 先查列是否存在）
ALTER_ROOMS_SQL = "SELECT COUNT(*) AS n FROM information_schema.columns " \
                  "WHERE table_schema='study_room' AND table_name='rooms' AND column_name='location'"

CREATE_RECORDS_SQL = """
CREATE TABLE IF NOT EXISTS records (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts DATETIME NOT NULL,
    camera_id VARCHAR(32) NOT NULL DEFAULT 'cam_01',
    head_count INT NOT NULL,
    occupied_count INT NOT NULL,
    seat_status JSON NOT NULL,
    INDEX idx_ts (ts),
    INDEX idx_cam_ts (camera_id, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS seat_sessions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    camera_id VARCHAR(32) NOT NULL,
    seat_id INT NOT NULL,
    track_id INT NOT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME NOT NULL,
    duration_sec INT NOT NULL,
    INDEX idx_cam_seat (camera_id, seat_id),
    INDEX idx_start (start_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_ALERTS_SQL = """
CREATE TABLE IF NOT EXISTS camera_alerts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts DATETIME NOT NULL,
    camera_id VARCHAR(32) NOT NULL,
    alert_type VARCHAR(16) NOT NULL COMMENT 'blocked/moved/recovered',
    message VARCHAR(255) NOT NULL,
    INDEX idx_cam_ts (camera_id, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


@app.on_event("startup")
def init_db():
    # 先不指定库连接, 建库后再正常使用
    cfg = dict(DB_CONFIG)
    cfg.pop("database")
    db = pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)
    try:
        with db.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS study_room DEFAULT CHARSET utf8mb4")
        db.commit()
    finally:
        db.close()

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(ALTER_ROOMS_SQL)
            if cur.fetchone()["n"] == 0:
                cur.execute("ALTER TABLE rooms ADD COLUMN location "
                            "VARCHAR(128) NOT NULL DEFAULT ''")
            cur.execute(CREATE_RECORDS_SQL)
            cur.execute(CREATE_SESSIONS_SQL)
            cur.execute(CREATE_ALERTS_SQL)
        db.commit()
    finally:
        db.close()


# ---------- 请求/响应模型 ----------

class RoomIn(BaseModel):
    camera_id: str = Field(..., min_length=1, max_length=32,
                           description="教室摄像头唯一标识，如 room_201")
    name: str = Field(..., min_length=1, max_length=64, description="教室名称，如 三教201")
    total_seats: int = Field(0, ge=0, description="座位总数")
    location: str = Field("", max_length=128, description="教室地址，如 三教2楼东侧")


class Room(BaseModel):
    camera_id: str
    name: str
    total_seats: int
    location: str = ""
    created_at: Optional[datetime] = None


class Report(BaseModel):
    head_count: int = Field(..., ge=0)
    occupied_count: int = Field(..., ge=0)
    seat_status: Optional[list[dict]] = None   # 已废弃（座位由 rooms.total_seats 管理）
    camera_id: str = "cam_01"
    ts: Optional[datetime] = None    # 不传则用服务器时间
    sessions: Optional[list[dict]] = None  # 已结束的人的会话（edge_agent 增量上报）


class SessionIn(BaseModel):
    seat_id: int
    track_id: int
    start: datetime
    end: datetime


class Stats(BaseModel):
    head_count: int
    occupied_count: int
    total_seats: int
    occupancy_rate: float
    seat_status: list[dict]
    last_update: Optional[datetime]


# ---------- 接口 ----------

# ===== 教室管理 =====

@app.post("/api/rooms", response_model=Room)
def create_room(room: RoomIn):
    """注册教室（重复注册 = 更新信息）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO rooms (camera_id, name, total_seats, location) VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name=%s, total_seats=%s, location=%s",
                (room.camera_id, room.name, room.total_seats, room.location,
                 room.name, room.total_seats, room.location))
            cur.execute("SELECT * FROM rooms WHERE camera_id=%s", (room.camera_id,))
            row = cur.fetchone()
        db.commit()
        return Room(**row)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/api/rooms", response_model=list[Room])
def list_rooms():
    """教室列表"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM rooms ORDER BY created_at")
            return [Room(**r) for r in cur.fetchall()]
    finally:
        db.close()


@app.put("/api/rooms/{camera_id}")
def update_room(camera_id: str, room: RoomIn):
    """修改教室信息（名称/座位数/地址; camera_id 不可改, 是关联键）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT camera_id FROM rooms WHERE camera_id=%s", (camera_id,))
            if not cur.fetchone():
                raise HTTPException(404, f"教室不存在: {camera_id}")
            cur.execute(
                "UPDATE rooms SET name=%s, total_seats=%s, location=%s "
                "WHERE camera_id=%s",
                (room.name, room.total_seats, room.location, camera_id))
            cur.execute("SELECT * FROM rooms WHERE camera_id=%s", (camera_id,))
            row = cur.fetchone()
        db.commit()
        return Room(**row)
    finally:
        db.close()


@app.delete("/api/rooms/{camera_id}")
def delete_room(camera_id: str, purge: bool = False):
    """删除教室（默认保留历史数据, ?purge=true 连同该教室全部记录一起删）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM rooms WHERE camera_id=%s", (camera_id,))
            deleted = cur.rowcount
            if deleted and purge:
                cur.execute("DELETE FROM records WHERE camera_id=%s", (camera_id,))
                cur.execute("DELETE FROM seat_sessions WHERE camera_id=%s", (camera_id,))
                cur.execute("DELETE FROM camera_alerts WHERE camera_id=%s", (camera_id,))
        db.commit()
        if not deleted:
            raise HTTPException(404, f"教室不存在: {camera_id}")
        return {"ok": True}
    finally:
        db.close()


@app.get("/api/overview")
def overview():
    """多教室实时总览: 以注册教室为准（无上报数据的教室也显示, 置为 0/未上报）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            # rooms 为主表 LEFT JOIN 每教室最新一条记录, 新注册教室也能出现
            cur.execute(
                "SELECT rm.camera_id, rm.name, rm.location, rm.total_seats AS room_seats, "
                "       r.head_count, r.occupied_count, r.ts "
                "FROM rooms rm "
                "LEFT JOIN records r ON r.id = ("
                "    SELECT id FROM records r2 WHERE r2.camera_id = rm.camera_id "
                "    ORDER BY ts DESC LIMIT 1)"
                "ORDER BY rm.created_at")
            rows = cur.fetchall()
        rooms = []
        total_heads = total_occ = total_seats = 0
        for r in rows:
            has_data = r["head_count"] is not None
            n_seats = r["room_seats"] or 0
            head = r["head_count"] or 0
            occ = r["occupied_count"] or 0
            rooms.append({
                "camera_id": r["camera_id"],
                "name": r["name"] or r["camera_id"],
                "location": r.get("location") or "",
                "head_count": head,
                "occupied_count": occ,
                "total_seats": n_seats,
                "occupancy_rate": round(occ / n_seats, 4) if n_seats else 0.0,
                "last_update": r["ts"] if has_data else None,
            })
            total_heads += head
            total_occ += occ
            total_seats += n_seats
        return {
            "rooms": rooms,
            "summary": {
                "room_count": len(rooms),
                "total_heads": total_heads,
                "total_occupied": total_occ,
                "total_seats": total_seats,
                "occupancy_rate": round(total_occ / total_seats, 4) if total_seats else 0.0,
            },
        }
    finally:
        db.close()


@app.post("/api/report")
def report(data: Report):
    db = get_db()
    try:
        with db.cursor() as cur:
            # occupied 封顶到教室总座位数（检测人数可能瞬时超过座位数, 如多人站立/误检）
            cur.execute("SELECT total_seats FROM rooms WHERE camera_id=%s",
                        (data.camera_id,))
            room = cur.fetchone()
            occ = data.occupied_count
            if room and room["total_seats"]:
                occ = min(occ, room["total_seats"])
            cur.execute(
                "INSERT INTO records (ts, camera_id, head_count, occupied_count, seat_status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (data.ts or datetime.now(), data.camera_id,
                 data.head_count, occ,
                 json.dumps(data.seat_status or [], ensure_ascii=False)),
            )
            # 增量上报已结束的座位会话
            if data.sessions:
                for s in data.sessions:
                    start = s["start"] if isinstance(s["start"], datetime) \
                        else datetime.fromisoformat(s["start"])
                    end = s["end"] if isinstance(s["end"], datetime) \
                        else datetime.fromisoformat(s["end"])
                    dur = int((end - start).total_seconds())
                    if dur <= 0:
                        continue
                    cur.execute(
                        "INSERT INTO seat_sessions (camera_id, seat_id, track_id, "
                        "start_time, end_time, duration_sec) VALUES (%s, %s, %s, %s, %s, %s)",
                        (data.camera_id, s["seat_id"], s["track_id"],
                         start, end, dur))
        db.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/api/duration")
def duration(camera_id: str = "cam_01", day: Optional[str] = None):
    """在座时长统计（人维度会话: 一个人从出现到离开为一段）

    返回: 会话数、人均在座时长、累计在座时长、最长单次
    day: YYYY-MM-DD，默认今天
    """
    target = day or datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n, COALESCE(AVG(duration_sec), 0) AS avg_sec, "
                "       COALESCE(SUM(duration_sec), 0) AS total_sec, "
                "       COALESCE(MAX(duration_sec), 0) AS max_sec "
                "FROM seat_sessions WHERE camera_id=%s AND DATE(start_time)=%s",
                (camera_id, target))
            overall = cur.fetchone()
        return {
            "day": target,
            "camera_id": camera_id,
            "overall": {
                "session_count": overall["n"],
                "avg_minutes": round(overall["avg_sec"] / 60, 1),
                "total_minutes": round(overall["total_sec"] / 60, 1),
                "longest_minutes": round(overall["max_sec"] / 60, 1),
            },
        }
    finally:
        db.close()


@app.get("/api/stats", response_model=Stats)
def stats(camera_id: str = "cam_01"):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT head_count, occupied_count, seat_status, ts FROM records "
                "WHERE camera_id=%s ORDER BY ts DESC LIMIT 1", (camera_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "暂无数据，请等待 edge_agent 上报")
            # 总座位数以教室注册信息为准（座位固定, 人数动态）
            cur.execute("SELECT total_seats FROM rooms WHERE camera_id=%s", (camera_id,))
            room = cur.fetchone()
        seats = json.loads(row["seat_status"] or "[]")
        total = room["total_seats"] if room else len(seats)
        occ = min(row["occupied_count"], total)
        return Stats(
            head_count=row["head_count"], occupied_count=occ, total_seats=total,
            occupancy_rate=round(occ / total, 4) if total else 0.0,
            seat_status=seats, last_update=row["ts"],
        )
    finally:
        db.close()


@app.get("/api/seats")
def seats(camera_id: str = "cam_01"):
    """座位列表 + 当前占用（无历史数据时仅返回座位列表）"""
    try:
        return stats(camera_id)
    except HTTPException:
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT seat_status FROM records WHERE camera_id=%s "
                    "ORDER BY ts DESC LIMIT 1", (camera_id,))
                row = cur.fetchone()
        finally:
            db.close()
        if row:
            return {"seat_status": json.loads(row["seat_status"])}
        raise HTTPException(404, "暂无数据")


@app.get("/api/history")
def history(hours: int = 24, camera_id: str = "cam_01"):
    """最近 N 小时的采样序列（供看板画曲线）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT ts, head_count, occupied_count FROM records "
                "WHERE camera_id=%s AND ts >= %s ORDER BY ts",
                (camera_id, datetime.now() - timedelta(hours=hours)))
            rows = cur.fetchall()
        return {"points": [
            {"ts": r["ts"].strftime("%Y-%m-%d %H:%M:%S"),
             "head_count": r["head_count"], "occupied_count": r["occupied_count"]}
            for r in rows]}
    finally:
        db.close()


@app.get("/api/daily")
def daily(day: Optional[str] = None, camera_id: str = "cam_01"):
    """按小时聚合（day 格式 YYYY-MM-DD，默认今天），供 Ollama 生成自然语言日报"""
    target = day or datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT HOUR(ts) AS h, AVG(head_count) AS avg_heads, AVG(occupied_count) AS avg_occ, "
                "MAX(head_count) AS max_heads, COUNT(*) AS samples "
                "FROM records WHERE camera_id=%s AND DATE(ts)=%s GROUP BY HOUR(ts) ORDER BY h",
                (camera_id, target))
            rows = cur.fetchall()
        return {"day": target, "hours": rows}
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ===== 摄像头异常告警 =====

class AlertIn(BaseModel):
    camera_id: str = "cam_01"
    alert_type: str = Field(..., pattern="^(blocked|moved|recovered)$")
    message: str = Field(..., min_length=1, max_length=255)
    ts: Optional[datetime] = None


@app.post("/api/alerts")
def create_alert(alert: AlertIn):
    """边缘端上报摄像头异常（遮挡/移动/恢复）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO camera_alerts (ts, camera_id, alert_type, message) "
                "VALUES (%s, %s, %s, %s)",
                (alert.ts or datetime.now(), alert.camera_id,
                 alert.alert_type, alert.message))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.get("/api/alerts")
def list_alerts(hours: int = 24, camera_id: Optional[str] = None):
    """最近告警（默认 24h 全部教室, 大屏顶栏红色告警条数据源）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            if camera_id:
                cur.execute(
                    "SELECT ts, camera_id, alert_type, message FROM camera_alerts "
                    "WHERE ts >= NOW() - INTERVAL %s HOUR AND camera_id=%s "
                    "ORDER BY ts DESC LIMIT 50", (hours, camera_id))
            else:
                cur.execute(
                    "SELECT ts, camera_id, alert_type, message FROM camera_alerts "
                    "WHERE ts >= NOW() - INTERVAL %s HOUR "
                    "ORDER BY ts DESC LIMIT 50", (hours,))
            return cur.fetchall()
    finally:
        db.close()


# ===== RAG 问答助手（Text2SQL + Ollama） =====

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")

# 提供给 LLM 的库表说明（只读视图描述）
_SCHEMA_DESC = """数据库 study_room, 表:
1. rooms: 教室注册表
   - camera_id VARCHAR 教室标识(英文, 如 room_201/testroom)
   - name VARCHAR 教室中文名称(如 三教201)
   - total_seats INT 座位总数
   注意: 用户提到英文标识(如 testroom)时用 camera_id 匹配, 提到中文名称时用 name 匹配; 优先 camera_id。
2. records: 边缘端周期上报的实时监测记录
   - ts DATETIME 时间
   - camera_id VARCHAR 教室标识
   - head_count INT 检测人数
   - occupied_count INT 占用座位数(已封顶不超过该教室 total_seats)
   注意: 查询"当前/现在/是否有空座"等实时状态时, 每个教室必须取其最新一条记录
   (如 WHERE ts=(SELECT MAX(ts) FROM records r2 WHERE r2.camera_id=records.camera_id)),
   空座数 = rooms.total_seats - occupied_count, 不要用全天平均值。
3. seat_sessions: 人的在场会话(一个人从出现到离开)
   - camera_id VARCHAR 教室标识
   - track_id INT 跟踪ID(每次在场一个ID)
   - start_time DATETIME 进入时间
   - end_time DATETIME 离开时间
   - duration_sec INT 在场秒数
4. camera_alerts: 摄像头异常告警
   - ts DATETIME, camera_id, alert_type(blocked遮挡/moved移动/recovered恢复), message"""


def _ollama_chat(prompt: str, system: str = "", timeout: int = 120) -> str:
    """调用 Ollama chat 接口"""
    import urllib.request
    payload = {"model": OLLAMA_MODEL, "prompt": prompt,
               "system": system, "stream": False}
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("response", "").strip()


_SQL_FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "truncate",
                  "create", "grant", "revoke", "set", "call", "lock")


def _safe_sql(sql: str) -> Optional[str]:
    """校验 LLM 生成的 SQL: 只允许单条只读 SELECT, 否则返回 None"""
    sql = sql.strip()
    # 剥离 LLM 可能输出的 markdown 代码围栏（```sql ... ```）
    if sql.startswith("```"):
        sql = sql.split("```")[1] if sql.count("```") >= 2 else sql
        sql = sql.removeprefix("sql").removeprefix("SQL")
    sql = sql.strip().rstrip(";").strip()
    low = sql.lower()
    if not low.startswith("select") and not low.startswith("with"):
        return None
    for kw in _SQL_FORBIDDEN:
        if kw in low:
            return None
    return sql


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    camera_id: Optional[str] = None   # 限定教室(可选)


@app.post("/api/ask")
def ask(data: AskIn):
    """自然语言问答: 问题 -> LLM生成SQL -> 查库 -> LLM组织答案

    结构化时序数据用 Text2SQL 比向量检索更准。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    scope = f"当前限定教室 camera_id='{data.camera_id}'。" if data.camera_id \
        else "可查询全部教室, 回答时注明教室。"

    # ---- 1) LLM 生成 SQL ----
    sql_system = ("你是 MySQL 专家。只输出一条可直接执行的 SELECT 语句, "
                  "不要 markdown 代码块, 不要解释。禁止任何写操作。"
                  "SQL 尽量简洁, 避免不必要的自连接。")
    sql_prompt = (
        f"今天是 {today}。{scope}\n{_SCHEMA_DESC}\n\n"
        f"用户问题: {data.question}\n请生成查询 SQL:")
    try:
        raw_sql = _ollama_chat(sql_prompt, sql_system)
    except Exception as e:
        raise HTTPException(502, f"Ollama 不可达: {e}")

    sql = _safe_sql(raw_sql)
    if sql is None:
        raise HTTPException(400, f"生成的 SQL 不安全或非查询语句: {raw_sql!r}")

    # ---- 2) 执行（只读, 限 50 行）; 失败时带错误回喂 LLM 重生成一次 ----
    rows = None
    for attempt in range(2):
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(f"SELECT * FROM ({sql}) AS q LIMIT 50")
                rows = cur.fetchall()
            break
        except pymysql.MySQLError as e:
            if attempt == 1:
                raise HTTPException(400, f"SQL 执行失败: {e}\nSQL: {sql}")
            retry_prompt = (
                f"你之前生成的SQL执行报错。\nSQL: {sql}\n"
                f"MySQL错误: {e}\n用户问题: {data.question}\n"
                f"请修正后只输出一条正确的 SELECT 语句:")
            try:
                fixed = _ollama_chat(retry_prompt, sql_system)
            except Exception:
                raise HTTPException(502, "Ollama 不可达")
            sql = _safe_sql(fixed)
            if sql is None:
                raise HTTPException(400, f"修复后的 SQL 不安全: {fixed!r}")
        finally:
            db.close()

    # ---- 3) LLM 组织自然语言答案 ----
    rows_text = json.dumps(rows, ensure_ascii=False, default=str)
    ans_system = ("你是自习室管理助手, 用简洁中文回答, 基于数据不要编造。"
                  "duration_sec 等时长字段单位是秒, 回答时换算成分钟或小时（如 13500秒→225分钟→3小时45分）。")
    ans_prompt = (f"用户问题: {data.question}\n执行的SQL: {sql}\n"
                  f"查询结果(JSON, 空列表=无数据): {rows_text}\n请回答:")
    try:
        answer = _ollama_chat(ans_prompt, ans_system)
    except Exception as e:
        answer = f"(SQL 执行成功但组织答案失败: {e})\n原始数据: {rows_text}"

    return {"question": data.question, "sql": sql, "rows": rows, "answer": answer}


# ---------- 可视化大屏（web/index.html） ----------
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/ui", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"), html=True))
