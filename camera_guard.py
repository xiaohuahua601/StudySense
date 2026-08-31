# -*- coding: utf-8 -*-
"""
摄像头异常检测（边缘端轻量守卫, 无新模型依赖）

两类异常:
    1. 遮挡:   画面突然黑掉/糊掉（亮度骤降+方差骤降）且人数归零
    2. 移动:   帧间差分像素比例超阈值（摄像头被挪动/画面剧变）

用法:
    guard = CameraGuard()
    ...
    for frame in frames:
        people = ...  # 跟踪人数
        alert = guard.update(frame, people)
        if alert:
            print(alert["type"], alert["message"])

    guard.reset()  # 异常恢复后清状态
"""

import time

import cv2
import numpy as np


class CameraGuard:
    """基于图像统计量的异常检测, 每帧调用一次 update"""

    def __init__(self,
                 # 遮挡判定
                 block_brightness_thresh=35,     # 平均亮度低于此判为黑
                 block_var_thresh=80,           # 拉普拉斯方差低于此判为糊/纯色
                 block_confirm_frames=8,         # 连续 N 帧异常才确认（防闪断误报）
                 # 移动判定
                 move_diff_ratio=0.25,           # 帧间变化像素占比超过此值
                 move_confirm_frames=4,          # 连续 N 帧超阈值才确认
                 # 冷却
                 alert_cooldown=60.0):           # 同类告警冷却秒数（防刷屏）
        self.brightness_thresh = block_brightness_thresh
        self.var_thresh = block_var_thresh
        self.block_confirm = block_confirm_frames
        self.move_ratio = move_diff_ratio
        self.move_confirm = move_confirm_frames
        self.cooldown = alert_cooldown

        self._prev_gray = None
        self._block_streak = 0
        self._move_streak = 0
        self._last_alert = {}          # type -> timestamp
        self._was_abnormal = False     # 上帧是否处于异常状态（用于恢复通知）

    def _cooled(self, alert_type, now):
        last = self._last_alert.get(alert_type, 0.0)
        return now - last >= self.cooldown

    def update(self, frame, people_count):
        """每帧调用。返回告警 dict 或 None。

        frame: 当前帧（BGR）
        people_count: 跟踪确认的人数（遮挡时人数应归零）
        """
        now = time.time()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 缩小到固定尺寸再算统计量, 大画面也只需几毫秒
        gray_small = cv2.resize(gray, (160, 90))

        # ---- 1) 遮挡: 暗 + 糊 + 无人 ----
        brightness = float(gray_small.mean())
        sharpness = float(cv2.Laplacian(gray_small, cv2.CV_64F).var())
        blocked = (brightness < self.brightness_thresh
                   and sharpness < self.var_thresh
                   and people_count == 0)
        if blocked:
            self._block_streak += 1
        else:
            self._block_streak = 0

        # ---- 2) 移动: 帧间差分 ----
        moved = False
        if self._prev_gray is not None:
            diff = cv2.absdiff(gray_small, self._prev_gray)
            ratio = float((diff > 25).sum()) / diff.size
            if ratio > self.move_ratio:
                self._move_streak += 1
                moved = True
            else:
                self._move_streak = 0
        self._prev_gray = gray_small

        # ---- 3) 判定输出 ----
        alert = None
        is_abnormal = False
        if self._block_streak >= self.block_confirm:
            is_abnormal = True
            if self._cooled("blocked", now):
                alert = {"type": "blocked",
                         "message": "摄像头疑似被遮挡（画面黑暗/模糊且无人）"}
                self._last_alert["blocked"] = now
        elif self._move_streak >= self.move_confirm:
            is_abnormal = True
            if self._cooled("moved", now):
                alert = {"type": "moved",
                         "message": "摄像头疑似被移动（画面内容剧变）"}
                self._last_alert["moved"] = now

        # 异常 -> 正常 的恢复通知（一次性）
        recovered = False
        if self._was_abnormal and not is_abnormal:
            recovered = True
            self._was_abnormal = False
        elif is_abnormal:
            self._was_abnormal = True

        if alert:
            alert["recovered"] = False
            return alert
        if recovered:
            return {"type": "recovered",
                    "message": "摄像头画面已恢复正常",
                    "recovered": True}
        return None

    def reset(self):
        """手动重置状态（如切换视频源）"""
        self._prev_gray = None
        self._block_streak = 0
        self._move_streak = 0
        self._was_abnormal = False
