# -*- coding: utf-8 -*-
"""
多目标跟踪器: IOU + 质心匹配（纯 NumPy 实现，零依赖）

设计要点:
    - 贪心 IOU 匹配（IoU > 阈值才算同一目标），辅以质心距离兜底（头小幅移动时 IoU 可能低）
    - 轨迹需连续命中 max_age 帧未匹配才销毁（抗单帧漏检）
    - 新轨迹需连续 min_hits 帧命中才确认（抗单帧误检）
"""

import time

import numpy as np


class Track:
    """单条轨迹"""

    def __init__(self, track_id, box, frame_id):
        self.id = track_id
        self.box = box                     # [x1,y1,x2,y2]
        self.hits = 1                      # 连续命中帧数
        self.age = 0                       # 连续未命中帧数
        self.born_frame = frame_id
        self.confirmed = False             # min_hits 后确认
        self.total_frames = 1              # 累计出现帧数
        self.first_seen = None             # 首次确认时间戳
        self.last_seen = None              # 最后命中时间戳

    def predict(self):
        """简单老化: 未匹配时年龄 +1（无运动模型，头部移动靠 IoU 容忍）"""
        self.age += 1

    def update(self, box):
        self.box = box
        self.hits += 1
        self.total_frames += 1
        self.age = 0


def iou_matrix(boxes_a, boxes_b):
    """NxM IoU 矩阵"""
    if not len(boxes_a) or not len(boxes_b):
        return np.zeros((len(boxes_a), len(boxes_b)))
    a = np.asarray(boxes_a)  # N x 4
    b = np.asarray(boxes_b)  # M x 4
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:], b[None, :, 2:])
    wh = np.clip(rb - lt, 0, None)
    inter = wh[..., 0] * wh[..., 1]
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-9)


class IOUTracker:
    """IOU 质心跟踪器"""

    def __init__(self, iou_threshold=0.25, max_age=12, min_hits=3):
        """
        iou_threshold: IoU 低于此值不视为同一目标（改用质心距离兜底）
        max_age:       连续多少帧未匹配则删除轨迹（抗漏检窗口）
        min_hits:      新轨迹连续命中多少帧才确认（抗误检）
        """
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self.min_hits = min_hits
        self.tracks = []
        self._next_id = 1
        self.frame_id = 0
        self.finished = []            # 已离开者的会话归档

    def update(self, detections):
        """输入当前帧检测, 返回已确认轨迹 [(track_id, box), ...]

        detections: [([x1,y1,x2,y2], score), ...]  HeadDetector.detect 的返回值
        """
        self.frame_id += 1
        now = time.time()
        det_boxes = [d[0] for d in detections]

        # 1) 匹配: 与现有轨迹做 IOU
        matched_tracks, matched_dets = set(), set()
        if self.tracks and det_boxes:
            iou = iou_matrix([t.box for t in self.tracks], det_boxes)
            # 贪心取全局最大 IoU 依次配对
            order = np.dstack(np.unravel_index(np.argsort(-iou, axis=None), iou.shape))[0]
            for ti, di in order:
                if ti in matched_tracks or di in matched_dets:
                    continue
                if iou[ti, di] < self.iou_threshold:
                    # 质心距离兜底: 头部快速移动时 IoU 低但质心仍接近
                    t_box = self.tracks[ti].box
                    d_box = det_boxes[di]
                    tc = np.array([(t_box[0] + t_box[2]) / 2, (t_box[1] + t_box[3]) / 2])
                    dc = np.array([(d_box[0] + d_box[2]) / 2, (d_box[1] + d_box[3]) / 2])
                    t_size = max(t_box[2] - t_box[0], t_box[3] - t_box[1], 1)
                    if np.linalg.norm(tc - dc) > 1.2 * t_size:
                        continue  # 太远，不是同一目标
                matched_tracks.add(ti)
                matched_dets.add(di)
                self.tracks[ti].update(det_boxes[di])

        # 2) 未匹配轨迹老化
        for ti, t in enumerate(self.tracks):
            if ti not in matched_tracks:
                t.predict()

        # 3) 新检测开新轨迹
        for di, box in enumerate(det_boxes):
            if di in matched_dets:
                continue
            self.tracks.append(Track(self._next_id, box, self.frame_id))
            self._next_id += 1

        # 4) 确认与销毁; 确认时间戳在确认/命中时刷新
        alive = []
        for t in self.tracks:
            if t.hits >= self.min_hits and not t.confirmed:
                t.confirmed = True
                t.first_seen = now
            if t.confirmed and t.age == 0:
                t.last_seen = now
            if t.age > self.max_age and t.confirmed:
                # 确认轨迹超龄销毁 -> 归档为"人的会话"（在场时段）
                self.finished.append({"track_id": t.id,
                                      "start": t.first_seen,
                                      "end": t.last_seen or now})
                continue
            if t.age > 3 and not t.confirmed:
                continue  # 未确认的短命轨迹直接清理
            alive.append(t)
        self.tracks = alive

        return [(t.id, t.box) for t in self.tracks if t.confirmed]

    def pop_finished_sessions(self):
        """取出并清空已离开者的会话（供上报）"""
        out, self.finished = self.finished, []
        return out

    def flush_remaining_sessions(self):
        """收尾: 仍在场的轨迹强制归档到 finished（不弹出, 由上报逻辑统一取）"""
        now = time.time()
        for t in self.tracks:
            if t.confirmed:
                self.finished.append({"track_id": t.id,
                                      "start": t.first_seen,
                                      "end": t.last_seen or now})
        self.tracks = []
