# -*- coding: utf-8 -*-
"""
边缘采集代理: 视频源实时检测 -> 多目标跟踪 -> 人数统计 -> 周期上报（多教室版）

设计（简化版）:
    座位是固定的（注册教室时填总数）, 人是动的 -> 不做头->座位映射
    - 人数   = 跟踪确认后的轨迹数（比逐帧检测更稳, 抗误检/漏检抖动）
    - 空位   = 教室总座位数 - 人数
    - 会话   = 一个人从出现到离开的在场时段（人均在座时长的数据源）

链路: HeadDetector(ONNX) -> IOUTracker(轨迹) -> FastAPI

运行:
    python edge_agent.py                                    # 摄像头（默认教室 cam_01）
    python edge_agent.py --camera-id room_201               # 指定教室
    python edge_agent.py --source 图片路径.jpg               # 单张图片测试（检测+上报一次即退出）
    python edge_agent.py --source 视频路径.mp4               # 视频文件测试（完整跟踪链路）
    python edge_agent.py --interval 5                       # 上报间隔改为 5 秒
需先启动 server.py
"""

import argparse
import os
import time
from datetime import datetime

import cv2
import requests

from camera_guard import CameraGuard
from head_detector import HeadDetector
from tracker import IOUTracker

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
FLIP_FRAME = True


def report(payload):
    try:
        r = requests.post(f"{API_BASE}/api/report", json=payload, timeout=5)
        print(f"[report] {r.status_code} heads={payload['head_count']}"
              + (f" sessions=+{len(payload.get('sessions') or [])}"
                 if payload.get("sessions") else ""))
        return r.status_code == 200
    except requests.RequestException as e:
        print(f"[report] 失败: {e}")
        return False


def report_alert(camera_id, alert):
    """上报摄像头异常（遮挡/移动/恢复）"""
    try:
        r = requests.post(f"{API_BASE}/api/alerts", json={
            "camera_id": camera_id,
            "alert_type": alert["type"],
            "message": alert["message"],
        }, timeout=5)
        print(f"[alert] {r.status_code} {alert['type']}: {alert['message']}")
        return r.status_code == 200
    except requests.RequestException as e:
        print(f"[alert] 上报失败: {e}")
        return False


def build_payload(camera_id, head_count, tracker):
    """组装上报数据: 实时人数 + 已离开者的会话（增量）"""
    sessions = None
    finished = tracker.pop_finished_sessions()
    if finished:
        sessions = [{"seat_id": 0, "track_id": s["track_id"],
                     "start": datetime.fromtimestamp(s["start"]).isoformat(timespec="seconds"),
                     "end": datetime.fromtimestamp(s["end"]).isoformat(timespec="seconds")}
                    for s in finished]
    return {
        "head_count": head_count,
        "occupied_count": head_count,   # 空位 = 总座位数 - 人数（后端计算）
        "camera_id": camera_id,
        "sessions": sessions,
    }


def draw_overlay(frame, tracked, fps=None):
    """绘制: 跟踪框(带ID) + 顶部统计"""
    for tid, box in tracked:
        x1, y1, x2, y2 = [int(v) for v in box]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 255), 2)
        cv2.putText(frame, f"ID{tid}", (x1, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
    info = f"people:{len(tracked)}"
    if fps:
        info = f"FPS:{fps:.0f} " + info
    cv2.putText(frame, info, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    return frame


def run_image(detector, tracker, img_path, camera_id, show=True):
    """单张图片模式: 检测 -> 上报一次（无跟踪意义, 仅保底链路可用性验证）"""
    frame = cv2.imread(img_path)
    if frame is None:
        print(f"无法读取图片: {img_path}")
        return

    detections = detector.detect(frame)
    tracked = tracker.update(detections)
    frame = draw_overlay(frame, tracked)

    h, w = frame.shape[:2]
    if w > 1280:
        r = 1280 / w
        frame = cv2.resize(frame, (1280, int(h * r)))

    report(build_payload(camera_id, len(tracked), tracker))

    if show:
        cv2.imshow("Edge Agent (image)", frame)
        print("按任意键关闭窗口")
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    else:
        out = os.path.splitext(img_path)[0] + "_result.jpg"
        cv2.imwrite(out, frame)
        print(f"结果已保存: {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="0",
                        help="图片路径 / 视频路径 / 0(摄像头, 默认)")
    parser.add_argument("--interval", type=int, default=30, help="上报间隔秒数")
    parser.add_argument("--camera-id", default=os.environ.get("CAMERA_ID", "cam_01"),
                        help="教室标识")
    parser.add_argument("--no-show", action="store_true", help="不弹窗, 结果存文件")
    args = parser.parse_args()

    camera_id = args.camera_id
    detector = HeadDetector()
    tracker = IOUTracker()
    guard = CameraGuard()
    print(f"[edge] 教室: {camera_id} | API: {API_BASE}")

    # 图片模式: 单次检测上报
    ext = os.path.splitext(args.source)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".bmp"):
        run_image(detector, tracker, args.source,
                  camera_id, show=not args.no_show)
        return

    # 摄像头 / 视频文件模式
    src = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print(f"无法打开视频源: {args.source}")
        return
    if args.source.isdigit():
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    last_report = 0.0
    frame_count = 0
    fps_start = time.time()
    fps = None
    last_head_count = 0
    _last_save = 0.0

    while True:
        ret, frame = cap.read()
        if not ret:
            if not args.source.isdigit():
                print("视频播放完毕")
            break
        if FLIP_FRAME and args.source.isdigit():  # 仅摄像头镜像
            frame = cv2.flip(frame, 1)

        detections = detector.detect(frame)
        tracked = tracker.update(detections)
        last_head_count = len(tracked)

        # 摄像头异常守卫（遮挡/移动）, 告警即上报
        alert = guard.update(frame, last_head_count)
        if alert:
            report_alert(camera_id, alert)

        frame_count += 1
        elapsed = time.time() - fps_start
        if elapsed >= 1.0:
            fps = frame_count / elapsed
            frame_count = 0
            fps_start = time.time()

        frame = draw_overlay(frame, tracked, fps)

        if args.no_show:
            # 无头模式: 每秒存最新帧, 供模拟页轮询展示
            if time.time() - _last_save >= 1.0:
                _last_save = time.time()
                cv2.imwrite(f"sim_{camera_id}.jpg", frame)
        else:
            cv2.imshow("Edge Agent", frame)
            if (cv2.waitKey(1) & 0xFF) in (27, ord('q')):
                break

        # 周期上报（含增量会话）
        if time.time() - last_report >= args.interval:
            report(build_payload(camera_id, len(tracked), tracker))
            last_report = time.time()

    cap.release()
    cv2.destroyAllWindows()

    # 收尾: 仍在场的人也归档上报（沿用最后人数, 不把教室刷成 0 人）
    tracker.flush_remaining_sessions()
    report(build_payload(camera_id, last_head_count, tracker))


if __name__ == "__main__":
    main()
