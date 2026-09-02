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

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import pymysql
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import BaseModel, Field, field_validator

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

# ---------- CORS 跨域白名单（仪表盘独立部署时用） ----------
# 环境变量 CORS_ORIGINS: 逗号分隔的来源白名单, 如 "http://localhost:8080,http://192.168.1.50:8080"
# 默认为空 = 不开启跨域（门户 SSR 同源部署不受影响）; 大屏页面无需登录, 不携带 cookie
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    )

# 门户 SSR 与 API 同源部署(8000), 不再需要 CORS 跨域; 统一注入安全响应头
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith(("/portal", "/api/auth")):
        resp.headers.setdefault("Cache-Control", "no-store")   # 会话相关页面与接口不落缓存
    if request.url.path.startswith("/portal"):
        # CSP: 仅允许自身 + ECharts CDN; 内联脚本/样式(单文件页面风格)保留
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
    return resp

# ---------- 用户认证配置 ----------
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", secrets.token_hex(32))
TOKEN_TTL = 7 * 24 * 3600        # token 有效期 7 天


def _hash_pwd(password: str, salt: str = None):
    """PBKDF2-SHA256 口令哈希, 返回 (salt, hash) 十六进制"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return salt, digest.hex()


def _verify_pwd(password: str, salt: str, expected: str) -> bool:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return hmac.compare_digest(digest.hex(), expected)


def _b64(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    import base64
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def _make_token(uid: int, username: str, role: str) -> str:
    payload = _b64(json.dumps({"uid": uid, "username": username, "role": role,
                               "exp": int(time.time()) + TOKEN_TTL}))
    sig = hmac.new(TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _parse_token(token: str) -> Optional[dict]:
    try:
        payload, sig = token.split(".")
        expect = hmac.new(TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_unb64(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


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

# 每个教室最新一条记录的视图: 供 LLM 直接查"当前人数/当前空座", 免写复杂子查询
CREATE_LATEST_VIEW_SQL = """
CREATE OR REPLACE VIEW latest_records AS
SELECT camera_id, head_count, occupied_count, ts
FROM records rec
WHERE ts = (SELECT MAX(ts) FROM records r2 WHERE r2.camera_id = rec.camera_id)
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

CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(32) NOT NULL UNIQUE,
    password_salt CHAR(32) NOT NULL,
    password_hash CHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'user' COMMENT 'admin/user',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login DATETIME DEFAULT NULL
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
            cur.execute(CREATE_LATEST_VIEW_SQL)
            cur.execute(CREATE_SESSIONS_SQL)
            cur.execute(CREATE_ALERTS_SQL)
            cur.execute(CREATE_USERS_SQL)
            # 种子管理员: 首次启动写入 admin/admin123（之后可改密码）
            cur.execute("SELECT COUNT(*) AS n FROM users")
            if cur.fetchone()["n"] == 0:
                salt, ph = _hash_pwd("admin123")
                cur.execute("INSERT INTO users (username, password_salt, password_hash, role) "
                            "VALUES ('admin', %s, %s, 'admin')", (salt, ph))
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

    @field_validator("created_at", mode="before")
    @classmethod
    def _zero_date_to_none(cls, v):
        """MySQL 零值日期('0000-00-00')无法被解析为 datetime, 兜底转为 None"""
        if isinstance(v, str) and v.startswith("0000-00-00"):
            return None
        return v


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


@app.get("/api/trends")
def trends(days: int = 7, camera_id: Optional[str] = None):
    """历史规律分析: 最近 N 天 × 24 小时的平均占用率热力矩阵 + 黄金时段推荐
    - camera_id 为空时统计全馆（各教室 occupied 占 total_seats 的总占比）
    - 返回 heatmap[day][hour] = 平均占用率(0~1), days 按最近到最远排列
    - golden: 全周(按星期×小时聚合)空座率最高的 Top 时段"""
    days = max(2, min(days, 30))
    db = get_db()
    try:
        with db.cursor() as cur:
            # 按天×小时聚合平均占用率: 单教室用该教室占用率, 全馆用 SUM(occ)/SUM(seats)
            if camera_id:
                cur.execute(
                    "SELECT DATE(ts) AS d, HOUR(ts) AS h, "
                    "       AVG(r.occupied_count / rm.total_seats) AS avg_rate "
                    "FROM records r JOIN rooms rm ON rm.camera_id = r.camera_id "
                    "WHERE r.camera_id=%s AND ts >= CURDATE() - INTERVAL %s DAY "
                    "GROUP BY DATE(ts), HOUR(ts)",
                    (camera_id, days - 1))
            else:
                # 全馆: 每个采样时刻的整体占用率 = 各教室占用之和 / 座位总数, 再按时段平均
                cur.execute(
                    "SELECT t.d AS d, t.h AS h, AVG(t.rate) AS avg_rate FROM ("
                    "  SELECT DATE(r.ts) AS d, HOUR(r.ts) AS h, r.ts, "
                    "         SUM(r.occupied_count) / (SELECT SUM(total_seats) FROM rooms) AS rate "
                    "  FROM records r WHERE r.ts >= CURDATE() - INTERVAL %s DAY "
                    "  GROUP BY r.ts, d, h"
                    ") t GROUP BY t.d, t.h",
                    (days - 1,))
            rows = cur.fetchall()
    finally:
        db.close()

    # 组装热力矩阵
    today = datetime.now().date()
    date_list = [today - timedelta(days=i) for i in range(days)]      # 最近→最远
    hm = {d.strftime("%Y-%m-%d"): [None] * 24 for d in date_list}
    for r in rows:
        key = r["d"].strftime("%Y-%m-%d") if hasattr(r["d"], "strftime") else str(r["d"])
        if key in hm and 0 <= r["h"] <= 23:
            hm[key][r["h"]] = round(float(r["avg_rate"] or 0), 4)

    # 黄金时段: 按星期×小时聚合（样本不足的跳过）, 取空座率最高 Top3
    by_dow_hour = {}
    for r in rows:
        d = r["d"]
        dow = d.weekday() if hasattr(d, "weekday") else 0
        by_dow_hour.setdefault((dow, r["h"]), []).append(float(r["avg_rate"] or 0))
    slots = []
    for (dow, h), vals in by_dow_hour.items():
        if len(vals) >= max(1, days // 7):   # 至少出现过一天的样本
            slots.append({"dow": dow, "hour": h,
                          "free_rate": round(1 - sum(vals) / len(vals), 4),
                          "samples": len(vals)})
    slots.sort(key=lambda x: -x["free_rate"])
    golden = slots[:3]                        # 空座率最高 Top3 = 推荐
    # 高峰时段从剩余时段里挑最挤的, 与黄金时段互斥, 避免"既推荐又避开"
    golden_keys = {(g["dow"], g["hour"]) for g in golden}
    peak = sorted((s for s in slots if (s["dow"], s["hour"]) not in golden_keys),
                  key=lambda x: x["free_rate"])[:3]
    return {
        "days": days,
        "camera_id": camera_id,
        "dates": [d.strftime("%Y-%m-%d") for d in date_list],
        "heatmap": hm,
        "golden": golden,
        "peak": peak,
    }


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
1. rooms: 教室注册表（只有静态属性, 没有任何人数/占用列!）
   - camera_id VARCHAR 教室标识(英文, 如 room_201/testroom)
   - name VARCHAR 教室中文名称(如 三教201)
   - total_seats INT 座位总数
   注意: 用户提到英文标识(如 testroom)时用 camera_id 匹配, 提到中文名称时用 name 匹配; 优先 camera_id。
2. latest_records(视图): 每个教室"最新一条"监测记录——查"当前人数/当前空座"必须用它, 已自动取好, 无需任何子查询!
   - camera_id VARCHAR 教室标识
   - head_count INT 当前检测人数
   - occupied_count INT 当前占用座位数
   - ts DATETIME 最新上报时间
   当前空座数 = rooms.total_seats - latest_records.occupied_count。
3. records: 边缘端周期上报的历史监测明细（查历史/趋势/平均值时才用它）
   - ts DATETIME 时间, camera_id, head_count INT, occupied_count INT
   注意: 不要用 records 查"当前/现在"状态(要过滤最新记录很复杂, 容易写错);
   也禁止 GROUP BY + MAX(occupied_count) 之类写法——那是历史峰值不是当前值。
4. seat_sessions: 人的在场会话(一个人从出现到离开)
   - camera_id VARCHAR 教室标识
   - track_id INT 跟踪ID(每次在场一个ID)
   - start_time DATETIME 进入时间
   - end_time DATETIME 离开时间
   - duration_sec INT 在场秒数
5. camera_alerts: 摄像头异常告警
   - ts DATETIME, camera_id, alert_type(blocked遮挡/moved移动/recovered恢复), message

历史规律问题提示: "过去一周哪天/哪个时段人最多(最少)" 可用 SQL 查询, 如:
SELECT DAYOFWEEK(ts) AS dow, HOUR(ts) AS h, AVG(head_count) AS avg_heads
FROM records WHERE ts >= NOW() - INTERVAL 7 DAY GROUP BY dow, h ORDER BY avg_heads DESC;
"明天/今天什么时间去最空/最容易找到空座" 同理取 avg 最低的时段, 直接套用:
SELECT HOUR(ts) AS hour, AVG(head_count) AS avg_heads
FROM records WHERE ts >= NOW() - INTERVAL 7 DAY
GROUP BY HOUR(ts) ORDER BY avg_heads ASC LIMIT 3;
注意: 只能用 HOUR(ts)/DAYOFWEEK(ts) 等函数提取时间, 不存在名为 DATE 的列。

参考示例（各教室当前人数, 按人数降序, 适用于"哪个教室人最多/最挤/最热门"）:
SELECT r.camera_id, r.name, r.location, t.occupied_count
FROM rooms r JOIN latest_records t ON t.camera_id = r.camera_id
ORDER BY t.occupied_count DESC;
参考示例（各教室当前空座数, 按空座降序, 适用于"哪个教室最空/空座最多"）:
SELECT r.camera_id, r.name, r.location, r.total_seats - t.occupied_count AS free_seats
FROM rooms r JOIN latest_records t ON t.camera_id = r.camera_id
ORDER BY free_seats DESC;
参考示例（某个教室当前人数, 适用于"X教室现在有几人/多少人"）:
SELECT r.camera_id, r.name, r.location, t.head_count, t.occupied_count
FROM rooms r JOIN latest_records t ON t.camera_id = r.camera_id
WHERE r.name LIKE '%教室中文名%';   -- 问"几人"时回答 head_count
注意区分方向: "人最多/最挤" = 按 occupied_count(或 head_count) 降序;
"最空/空位最多/最空闲" = 按 free_seats(空座数) 降序——两者相反, 不要混淆!
"最空"和"空位最多"是同一个意思, 都按空座数降序。

重要: rooms 表没有 occupied_count/head_count 列, 写 r.occupied_count 或 r.head_count 会直接报错
(Unknown column)! 人数/占用类数据必须 JOIN latest_records(当前) 或 records(历史) 获得。
MySQL 开启了 only_full_group_by 严格模式: SELECT 中的非聚合列必须全部出现在 GROUP BY 子句中,
否则报 1055 错误; 拿不准就不用 GROUP BY, 直接 JOIN 后 ORDER BY 排序即可。
涉及教室的查询, SELECT 中必须包含 r.name(教室中文名称)和 r.location(地址)列, 便于面向用户展示。
问"明天/将来"的问题时没有未来数据, 一律用历史数据按星期×小时聚合平均来推测。"""


def _ollama_chat(prompt: str, system: str = "", timeout: int = 120) -> str:
    """调用 Ollama chat 接口（低温采样: SQL生成/数值读数需要确定性, 高温易幻觉）"""
    import urllib.request
    payload = {"model": OLLAMA_MODEL, "prompt": prompt,
               "system": system, "stream": False,
               "options": {"temperature": 0.1}}
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


# ---------- AI 问答·意图路由 ----------
# 高频问题走"固定 SQL 模板 + Python 确定性渲染答案", 完全不经 LLM —— 无论什么模型、
# 什么采样温度, 答案都由数据唯一确定, 从架构上杜绝"数对了但说错了"的幻觉。
# LLM Text2SQL 仅作为长尾问题的兜底。

def _room_label(row: dict) -> str:
    """教室名(地址); 无地址只写教室名"""
    loc = row.get("location")
    return f"{row['name']}({loc})" if loc else row["name"]


def _match_room_name(db, question: str) -> Optional[str]:
    """问题中是否出现某个已注册教室名(含别名), 返回该教室名"""
    with db.cursor() as cur:
        cur.execute("SELECT name FROM rooms")
        names = [r["name"] for r in cur.fetchall()]
    for name in names:
        if name and name in question:
            return name
    return None


def _route_intent(db, question: str, camera_id: Optional[str]) -> Optional[tuple]:
    """识别高频问题意图。命中返回 (kind, sql), 未命中返回 None(走 LLM)"""
    q = question.strip()
    if camera_id and not re.fullmatch(r"[\w\-]+", camera_id):
        camera_id = None   # 白名单校验, 防 SQL 注入
    scope = f"WHERE r.camera_id = '{camera_id}'" if camera_id else ""
    base = ("FROM rooms r JOIN latest_records t ON t.camera_id = r.camera_id "
            f"{scope}")

    # ① 时段推荐: "明天/今天什么时间去最空/最容易找到空座"
    if re.search(r"(什么时间|什么时候|几点|哪个时段|啥时候)", q) and \
       re.search(r"(空|座)", q):
        sql = ("SELECT HOUR(ts) AS hour, ROUND(AVG(head_count),1) AS avg_heads "
               "FROM records WHERE ts >= NOW() - INTERVAL 7 DAY "
               "GROUP BY HOUR(ts) ORDER BY avg_heads ASC LIMIT 3")
        return "best_time", sql

    room = _match_room_name(db, q)

    # ② 指定教室当前人数: "X教室现在有几人/多少人"
    if room and re.search(r"(几人|多少人|人数|有没有人|有人吗)", q):
        safe_room = room.replace("'", "''")
        sql = (f"SELECT r.name, r.location, t.head_count, t.occupied_count, "
               f"r.total_seats - t.occupied_count AS free_seats "
               f"FROM rooms r JOIN latest_records t ON t.camera_id = r.camera_id "
               f"WHERE r.name = '{safe_room}'")
        return "room_count", sql

    # ③ 指定教室当前空位: "X教室有多少空位/空座"
    if room and re.search(r"(空位|空座|空着|还有座)", q):
        safe_room = room.replace("'", "''")
        sql = (f"SELECT r.name, r.location, t.head_count, t.occupied_count, "
               f"r.total_seats - t.occupied_count AS free_seats "
               f"FROM rooms r JOIN latest_records t ON t.camera_id = r.camera_id "
               f"WHERE r.name = '{safe_room}'")
        return "room_free", sql

    # ④ 全局人最多/最少
    if re.search(r"(人最多|最多人|最挤|最热门)", q):
        sql = (f"SELECT r.name, r.location, t.occupied_count, t.head_count, "
               f"r.total_seats - t.occupied_count AS free_seats {base} "
               f"ORDER BY t.occupied_count DESC LIMIT 5")
        return "most_people", sql
    if re.search(r"(人最少|最少人|人少)", q):
        sql = (f"SELECT r.name, r.location, t.occupied_count, t.head_count, "
               f"r.total_seats - t.occupied_count AS free_seats {base} "
               f"ORDER BY t.occupied_count ASC LIMIT 5")
        return "least_people", sql

    # ⑤ 全局最空/空位最多、空位最少
    if re.search(r"(最空|最空闲|空位最多|空座最多|最多空位|最多空座)", q):
        sql = (f"SELECT r.name, r.location, t.occupied_count, t.head_count, "
               f"r.total_seats - t.occupied_count AS free_seats {base} "
               f"ORDER BY free_seats DESC LIMIT 5")
        return "most_free", sql
    if re.search(r"(空位最少|空座最少|最少空位|最难找座)", q):
        sql = (f"SELECT r.name, r.location, t.occupied_count, t.head_count, "
               f"r.total_seats - t.occupied_count AS free_seats {base} "
               f"ORDER BY free_seats ASC LIMIT 5")
        return "least_free", sql

    return None


def _render_intent_answer(kind: str, rows: list, question: str) -> str:
    """把路由 SQL 的查询结果渲染成确定性中文答案(不经 LLM)"""
    if not rows:
        return "暂无数据，请稍后再试。"

    if kind == "best_time":
        slots = "、".join(
            f"{int(r['hour'])}:00-{int(r['hour']) + 1}:00（历史平均 {r['avg_heads']} 人）"
            for r in rows)
        return f"根据近一周历史数据，容易找到空座的时间段是：{slots}。建议避开高峰、错峰前往。"

    r = rows[0]
    label = _room_label(r)
    if kind == "most_people":
        return f"当前在座人数最多的是 {label}：在座 {r['occupied_count']} 人，剩余空位 {r['free_seats']} 个。"
    if kind == "least_people":
        return f"当前在座人数最少的是 {label}：在座 {r['occupied_count']} 人，剩余空位 {r['free_seats']} 个。"
    if kind == "most_free":
        return f"当前最空的是 {label}：剩余空位 {r['free_seats']} 个，在座 {r['occupied_count']} 人。"
    if kind == "least_free":
        return f"当前最难找到空位的是 {label}：仅剩 {r['free_seats']} 个空位，在座 {r['occupied_count']} 人。"
    if kind == "room_count":
        n = r["head_count"]
        if n == 0:
            return f"{label} 当前没有人在座，空位 {r['free_seats']} 个。"
        return f"{label} 当前在座约 {n} 人，空位 {r['free_seats']} 个。"
    if kind == "room_free":
        return f"{label} 当前空位 {r['free_seats']} 个，在座 {r['occupied_count']} 人。"
    return "暂不支持的问题，请换个问法。"


@app.post("/api/ask")
def ask(data: AskIn):
    """自然语言问答: 高频问题走意图路由(固定SQL+确定性答案),
    长尾问题走 LLM Text2SQL(问题 -> LLM生成SQL -> 查库 -> LLM组织答案)。

    结构化时序数据用 Text2SQL 比向量检索更准。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    scope = f"当前限定教室 camera_id='{data.camera_id}'。" if data.camera_id \
        else "可查询全部教室, 回答时注明教室。"

    # ---- 0) 意图路由: 高频问题直接走固定 SQL, 确定性答案, 不经 LLM ----
    routed = None
    db = get_db()
    try:
        routed = _route_intent(db, data.question, data.camera_id)
    except Exception:
        routed = None   # 路由失败(如表不存在)则降级走 LLM
    if routed:
        kind, sql = routed
        try:
            with db.cursor() as cur:
                cur.execute(f"SELECT * FROM ({sql}) AS q LIMIT 50")
                rows = cur.fetchall()
            return {"question": data.question, "sql": sql, "rows": rows,
                    "route": "rule",
                    "answer": _render_intent_answer(kind, rows, data.question)}
        except pymysql.MySQLError:
            pass   # 固定 SQL 也失败(极端情况)则降级走 LLM
        finally:
            db.close()
    else:
        db.close()

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

    # ---- 2) 执行（只读, 限 50 行）; 失败时带错误+schema 回喂 LLM 重生成（最多重试 2 次） ----
    rows = None
    for attempt in range(3):
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(f"SELECT * FROM ({sql}) AS q LIMIT 50")
                rows = cur.fetchall()
            break
        except pymysql.MySQLError as e:
            if attempt == 2:
                raise HTTPException(400, f"SQL 执行失败: {e}")
            retry_prompt = (
                f"你之前生成的SQL执行报错。\nSQL: {sql}\n"
                f"MySQL错误: {e}\n用户问题: {data.question}\n\n"
                f"数据库结构如下, 请对照检查列名后再修正:\n{_SCHEMA_DESC}\n"
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
                  "『最空』『最空闲』指空闲座位最多, 不是座位最少。"
                  "回答『最多/最少/最高/最低』类问题时, 必须逐一核对查询结果中每一行的数值, "
                  "严格取数值最大/最小的那一行作答, 不要凭教室名称或常识猜测。"
                  "duration_sec 等时长字段单位是秒, 回答时换算成分钟或小时（如 13500秒→225分钟→3小时45分）。"
                  "提到教室时必须用教室中文名称(即查询结果中 name 列的值), "
                  "禁止在答案中出现 camera_id 之类的英文内部标识。"
                  "推荐或介绍教室时格式为'教室名(地址)'，地址必须用查询结果中 location 列的原值, "
                  "不要编造、不要改写; 若查询结果不含地址信息则只写教室名。"
                  "时段按小时聚合时用整点区间表述(如 14:00-15:00), 不要编造分钟。")
    ans_prompt = (f"用户问题: {data.question}\n执行的SQL: {sql}\n"
                  f"查询结果(JSON, 空列表=无数据): {rows_text}\n请回答:")
    try:
        answer = _ollama_chat(ans_prompt, ans_system)
    except Exception as e:
        answer = f"(SQL 执行成功但组织答案失败: {e})\n原始数据: {rows_text}"

    return {"question": data.question, "sql": sql, "rows": rows,
            "route": "llm", "answer": answer}


# ---------- 用户认证与用户管理（门户 SSR） ----------

class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_\u4e00-\u9fa5]+$")
    password: str = Field(..., min_length=6, max_length=64)
    nickname: Optional[str] = None   # 预留


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None


class UserCreateIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_\u4e00-\u9fa5]+$")
    password: str = Field(..., min_length=6, max_length=64)
    role: str = Field("user", pattern=r"^(admin|user)$")


class UserUpdateIn(BaseModel):
    role: Optional[str] = Field(None, pattern=r"^(admin|user)$")
    password: Optional[str] = Field(None, min_length=6, max_length=64)


# FastAPI 依赖: 解析 Bearer Token
from fastapi import Header  # noqa: E402


def current_user(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """双通道认证: Bearer Token(编程调用) 或 HttpOnly Cookie(门户会话)
    Cookie 会话下的写操作要求 X-Requested-With 自定义头 —— 跨站表单/图片无法携带,
    作为 CSRF 防护(SameSite=Strict 之外的第二道保险)"""
    token = None
    if authorization:
        scheme, t = get_authorization_scheme_param(authorization)
        if scheme.lower() == "bearer" and t:
            token = t
    elif request.cookies.get("st_session"):
        if request.method not in ("GET", "HEAD", "OPTIONS") and \
                request.headers.get("x-requested-with") != "XMLHttpRequest":
            raise HTTPException(403, "缺少 CSRF 校验头")
        token = request.cookies["st_session"]
    if not token:
        raise HTTPException(401, "未登录")
    data = _parse_token(token)
    if not data:
        raise HTTPException(401, "登录已过期, 请重新登录")
    return data


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


@app.post("/api/auth/register", response_model=UserOut)
def register(data: RegisterIn):
    """自助注册（一律普通用户, 管理员由后台创建）"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username=%s", (data.username,))
            if cur.fetchone():
                raise HTTPException(400, "用户名已存在")
            salt, ph = _hash_pwd(data.password)
            cur.execute("INSERT INTO users (username, password_salt, password_hash, role) "
                        "VALUES (%s, %s, %s, 'user')", (data.username, salt, ph))
            uid = cur.lastrowid
            cur.execute("SELECT id, username, role, created_at, last_login FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
        db.commit()
        return UserOut(**row)
    finally:
        db.close()


_login_fails = {}   # username -> [连续失败次数, 锁定截止时间戳]; 内存态, 重启清零


@app.post("/api/auth/login")
def login(data: LoginIn, request: Request):
    # 暴力破解防护: 同一用户名连续失败 5 次锁定 15 分钟
    now = time.time()
    fails = _login_fails.get(data.username)
    if fails and fails[1] > now:
        raise HTTPException(429, f"尝试次数过多, 请 {int((fails[1] - now) // 60) + 1} 分钟后再试")
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username=%s", (data.username,))
            row = cur.fetchone()
        if not row or not _verify_pwd(data.password, row["password_salt"], row["password_hash"]):
            count = (fails[0] + 1) if fails else 1
            _login_fails[data.username] = [count, now + 900] if count >= 5 else [count, 0]
            raise HTTPException(401, "用户名或密码错误")
        with db.cursor() as cur:
            cur.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (row["id"],))
        db.commit()
        _login_fails.pop(data.username, None)
        token = _make_token(row["id"], row["username"], row["role"])
        resp = JSONResponse({"token": token,
                             "user": {"id": row["id"], "username": row["username"], "role": row["role"]}})
        # HttpOnly: JS 完全不可读, XSS 偷不走; SameSite=Strict: 跨站请求不带 Cookie
        resp.set_cookie("st_session", token, httponly=True, samesite="strict",
                         max_age=TOKEN_TTL, path="/")
        return resp
    finally:
        db.close()


@app.get("/api/auth/me", response_model=UserOut)
def me(user: dict = Depends(current_user)):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id, username, role, created_at, last_login FROM users WHERE id=%s",
                        (user["uid"],))
            row = cur.fetchone()
        if not row:
            raise HTTPException(401, "用户不存在")
        return UserOut(**row)
    finally:
        db.close()


@app.get("/api/admin/users", response_model=list[UserOut])
def admin_list_users(_: dict = Depends(require_admin)):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id, username, role, created_at, last_login FROM users ORDER BY id")
            return [UserOut(**r) for r in cur.fetchall()]
    finally:
        db.close()


@app.post("/api/admin/users", response_model=UserOut)
def admin_create_user(data: UserCreateIn, _: dict = Depends(require_admin)):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username=%s", (data.username,))
            if cur.fetchone():
                raise HTTPException(400, "用户名已存在")
            salt, ph = _hash_pwd(data.password)
            cur.execute("INSERT INTO users (username, password_salt, password_hash, role) "
                        "VALUES (%s, %s, %s, %s)", (data.username, salt, ph, data.role))
            uid = cur.lastrowid
            cur.execute("SELECT id, username, role, created_at, last_login FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
        db.commit()
        return UserOut(**row)
    finally:
        db.close()


@app.put("/api/admin/users/{uid}", response_model=UserOut)
def admin_update_user(uid: int, data: UserUpdateIn, admin: dict = Depends(require_admin)):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "用户不存在")
            if data.role:
                if row["id"] == admin["uid"]:
                    raise HTTPException(400, "不能修改自己的角色")
                cur.execute("UPDATE users SET role=%s WHERE id=%s", (data.role, uid))
            if data.password:
                salt, ph = _hash_pwd(data.password)
                cur.execute("UPDATE users SET password_salt=%s, password_hash=%s WHERE id=%s",
                            (salt, ph, uid))
            cur.execute("SELECT id, username, role, created_at, last_login FROM users WHERE id=%s", (uid,))
            new = cur.fetchone()
        db.commit()
        return UserOut(**new)
    finally:
        db.close()


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, admin: dict = Depends(require_admin)):
    if uid == admin["uid"]:
        raise HTTPException(400, "不能删除自己")
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id=%s", (uid,))
            if not cur.fetchone():
                raise HTTPException(404, "用户不存在")
            cur.execute("DELETE FROM users WHERE id=%s", (uid,))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- 可视化大屏（web/index.html） ----------
from fastapi.responses import JSONResponse  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/ui", StaticFiles(directory=os.path.join(BASE_DIR, "web"), html=True))

# ---------- 管理门户（SSR 多页应用: Jinja2 模板 + HttpOnly Cookie 会话） ----------
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/portal-static", StaticFiles(directory=os.path.join(BASE_DIR, "web", "portal_static")), name="portal_static")


def _page_user(request: Request) -> Optional[dict]:
    t = request.cookies.get("st_session")
    return _parse_token(t) if t else None


def _require_page(request: Request, admin: bool = False):
    """SSR 页面守卫: 未登录 → 重定向登录页; 无权限 → 重定向总览(带 denied 标记)"""
    u = _page_user(request)
    if not u:
        return RedirectResponse("/portal/login", status_code=302), None
    if admin and u.get("role") != "admin":
        return RedirectResponse("/portal/overview?denied=1", status_code=302), None
    return None, u


@app.get("/portal/login", name="portal_login")
def portal_login(request: Request):
    if _page_user(request):                       # 已登录直接进应用
        return RedirectResponse("/portal/overview", status_code=302)
    return templates.TemplateResponse(request, "portal/login.html")


@app.get("/portal")
@app.get("/portal/overview")
def portal_overview(request: Request):
    r, u = _require_page(request)
    if r:
        return r
    return templates.TemplateResponse(request, "portal/overview.html",
                                      {"user": u, "active": "overview"})


@app.get("/portal/trends")
def portal_trends(request: Request):
    r, u = _require_page(request)
    if r:
        return r
    # 历史规律面向普通用户(选座/避峰参考), 管理员不需要
    if u.get("role") == "admin":
        return RedirectResponse("/portal/overview?denied=trends", status_code=302)
    return templates.TemplateResponse(request, "portal/trends.html",
                                      {"user": u, "active": "trends"})


@app.get("/portal/ai")
def portal_ai(request: Request):
    r, u = _require_page(request)
    if r:
        return r
    return templates.TemplateResponse(request, "portal/ai.html",
                                      {"user": u, "active": "ai"})


@app.get("/portal/rooms")
def portal_rooms(request: Request):
    r, u = _require_page(request, admin=True)
    if r:
        return r
    return templates.TemplateResponse(request, "portal/rooms.html",
                                      {"user": u, "active": "rooms"})


@app.get("/portal/users")
def portal_users(request: Request):
    r, u = _require_page(request, admin=True)
    if r:
        return r
    return templates.TemplateResponse(request, "portal/users.html",
                                      {"user": u, "active": "users"})


@app.post("/portal/logout")
def portal_logout():
    resp = RedirectResponse("/portal/login", status_code=302)
    resp.delete_cookie("st_session", path="/")
    return resp
