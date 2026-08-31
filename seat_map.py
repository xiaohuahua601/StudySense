# -*- coding: utf-8 -*-
"""
座位映射: 头框坐标 -> 座位号 -> 占用状态 + 在座时长统计（跟踪增强版）

核心升级（相对纯逐帧判定）:
    1. 占用判定基于跟踪 ID: 同一个人连续多帧在同一座位才算"稳定占用"（抗误检）
    2. 座位会话(seat session): 记录 track_id 从入座到离座的完整时段, 产出在座时长
    3. 离座宽限: 短暂离开(grace 秒内)不切断会话, 拿东西/低头捡笔不掉线
"""

import json
import os
import time
from dataclasses import dataclass, field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEATS_FILE = os.path.join(BASE_DIR, "seats.json")


def seats_file_for(camera_id: str):
    """按教室存座位标定: seats_<camera_id>.json（默认教室 cam_01 兼容旧 seats.json）"""
    if camera_id in ("", "cam_01"):
        return SEATS_FILE
    return os.path.join(BASE_DIR, f"seats_{camera_id}.json")


@dataclass
class Seat:
    id: int
    x1: int
    y1: int
    x2: int
    y2: int
    occupied: bool = False


@dataclass
class SeatSession:
    """一次连续在座会话"""
    seat_id: int
    track_id: int
    start: float               # 入座时间戳
    end: float = None          # 离座时间戳（None = 仍在座）
    grace_deadline: float = None  # 宽限期截止（短暂离座的容忍窗口）

    @property
    def duration(self):
        """会话时长（秒）"""
        return (self.end or time.time()) - self.start

    @property
    def active(self):
        return self.end is None


class SeatMap:
    """座位表: 基于跟踪轨迹的占用判定 + 会话时长统计"""

    def __init__(self, camera_id="cam_01", seats_file=None,
                 confirm_frames=3, grace_seconds=20):
        """
        confirm_frames: 连续 N 次上报都命中同一座位才确认占用
        grace_seconds:  离座宽限期（秒）, 超过才算真正离座
        """
        self.camera_id = camera_id
        if seats_file is None:
            seats_file = seats_file_for(camera_id)
        with open(seats_file, encoding="utf-8") as f:
            cfg = json.load(f)
        self.image_size = cfg["image_size"]
        self.seats = [Seat(**s) for s in cfg["seats"]]

        self.confirm_frames = confirm_frames
        self.grace_seconds = grace_seconds

        # track_id -> (seat_id, 连续命中次数)
        self._track_votes = {}
        # (seat_id,) -> 当前活跃会话
        self._active_sessions = {}
        # 已结束会话的历史（本次运行期）
        self.finished_sessions = []

    # ---------- 轨迹 -> 座位 ----------

    def _locate(self, box):
        """头框中心所在座位号（None = 不在任何座位区）"""
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2
        for seat in self.seats:
            if seat.x1 <= cx <= seat.x2 and seat.y1 <= cy <= seat.y2:
                return seat.id
        return None

    def update(self, tracked):
        """根据跟踪结果刷新座位状态。

        Args:
            tracked: [(track_id, [x1,y1,x2,y2]), ...]  IOUTracker.update 的返回值
        Returns:
            [{"id": 座位号, "occupied": bool}, ...] 全量座位状态
        """
        now = time.time()
        live_ids = {t for t, _ in tracked}

        # 1) 轨迹投票: 连续命中同一座位的次数
        for track_id, box in tracked:
            seat_id = self._locate(box)
            if seat_id is None:
                continue
            prev_seat, votes = self._track_votes.get(track_id, (None, 0))
            votes = votes + 1 if prev_seat == seat_id else 1
            self._track_votes[track_id] = (seat_id, votes)

        # 2) 每个座位唯一主人: 票数最高的确认轨迹
        #    （多人挤进同一座位区域时, 避免每帧"换人"造成会话抖动）
        seat_owner = {}
        for track_id, box in tracked:
            v = self._track_votes.get(track_id)
            if not v or v[1] < self.confirm_frames:
                continue
            sid, votes = v
            cur = seat_owner.get(sid)
            if cur is None or votes > cur[1]:
                seat_owner[sid] = (track_id, votes, box)

        # 3) 会话管理
        for sid, (tid, votes, box) in seat_owner.items():
            session = self._active_sessions.get(sid)
            if session is None:
                self._active_sessions[sid] = SeatSession(
                    seat_id=sid, track_id=tid, start=now)
            elif session.track_id == tid:
                session.grace_deadline = None
            elif session.track_id not in live_ids:
                # 原轨迹消失, 新轨迹接管同一座位 -> 视为同一人继续（自习室常态）
                session.track_id = tid
                session.grace_deadline = None
            else:
                # 原轨迹还活着且另有归属 -> 真换人, 切换会话
                session.end = now
                self.finished_sessions.append(session)
                self._active_sessions[sid] = SeatSession(
                    seat_id=sid, track_id=tid, start=now)

        # 4) 离座宽限
        self._end_left_seats(now, set(seat_owner))
        # 清理消失轨迹的投票
        for tid in list(self._track_votes):
            if tid not in live_ids:
                self._track_votes.pop(tid, None)

        # 刷新座位显示状态（活跃会话内的座位 = 占用）
        for seat in self.seats:
            seat.occupied = seat.id in {s.seat_id for s in self._active_sessions.values()
                                        if s.active}

        return self.status()

    # ---------- 会话管理 ----------

    def _end_left_seats(self, now, seen_seats):
        for seat_id, session in list(self._active_sessions.items()):
            if seat_id in seen_seats or not session.active:
                continue
            # 该座位本帧无确认轨迹 -> 进入或维持宽限
            if session.grace_deadline is None:
                session.grace_deadline = now + self.grace_seconds
            elif now > session.grace_deadline:
                # 宽限期过, 会话结束
                session.end = session.grace_deadline - self.grace_seconds
                self.finished_sessions.append(session)
                del self._active_sessions[seat_id]

    # ---------- 查询 ----------

    def status(self):
        return [{"id": s.id, "occupied": s.occupied} for s in self.seats]

    @property
    def occupied_count(self):
        return sum(s.occupied for s in self.seats)

    @property
    def total(self):
        return len(self.seats)

    def seat_durations(self):
        """各座位当前累计在座时长（秒）: 活跃会话 + 已结束会话"""
        durations = {}
        for s in self._active_sessions.values():
            durations[s.seat_id] = durations.get(s.seat_id, 0) + s.duration
        for s in self.finished_sessions:
            durations[s.seat_id] = durations.get(s.seat_id, 0) + s.duration
        return durations

    def pop_finished_sessions(self):
        """取出并清空已结束的会话（供上报）"""
        out, self.finished_sessions = self.finished_sessions, []
        return out
