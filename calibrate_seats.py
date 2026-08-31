# -*- coding: utf-8 -*-
"""
座位区域标定工具

用法:
    python calibrate_seats.py                    # 摄像头截帧标定
    python calibrate_seats.py --image 图片路径   # 直接在图片上标定（测试用）

摄像头模式: 打开摄像头 → 按 c 截一帧 → 框座位 → s 保存 seats.json
图片模式:   直接加载图片 → 框座位 → s 保存

操作:
    c  截取当前帧进入标定（摄像头模式）
    鼠标拖拽  画座位矩形（每个座位框一次，自动编号）
    u  撤销上一个座位
    s  保存 seats.json
    r  清空重画
    ESC/q  退出
"""

import argparse
import json
import os

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEATS_FILE = os.path.join(BASE_DIR, "seats.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="用图片标定（测试用），不传则用摄像头")
    parser.add_argument("--camera-id", default="cam_01",
                        help="教室标识, 保存为 seats_<camera_id>.json")
    args = parser.parse_args()

    from seat_map import seats_file_for
    seats_file = seats_file_for(args.camera_id)

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f"无法读取图片: {args.image}")
            return
    else:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("无法打开摄像头")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        print("摄像头已打开，调整好画面后按 c 截帧标定")

        frame = None
        # 实时预览直到按 c
        while True:
            ret, f = cap.read()
            if not ret:
                continue
            cv2.imshow("Head Detection", f)
            if (cv2.waitKey(1) & 0xFF) in (ord('c'), ord('C')):
                frame = f.copy()
                break
        cap.release()
        cv2.destroyWindow("Head Detection")

    seats = []          # [{"id": 1, "x1":.., "y1":.., "x2":.., "y2":..}, ...]
    drawing = False
    start = (0, 0)
    cur_rect = None     # (x1, y1, x2, y2)

    def on_mouse(event, x, y, flags, param):
        nonlocal drawing, start, cur_rect
        if event == cv2.EVENT_LBUTTONDOWN:
            drawing = True
            start = (x, y)
            cur_rect = None
        elif event == cv2.EVENT_MOUSEMOVE and drawing:
            cur_rect = (start[0], start[1], x, y)
        elif event == cv2.EVENT_LBUTTONUP and drawing:
            drawing = False
            x1, y1 = min(start[0], x), min(start[1], y)
            x2, y2 = max(start[0], x), max(start[1], y)
            if x2 - x1 > 10 and y2 - y1 > 10:
                seats.append({"id": len(seats) + 1, "x1": x1, "y1": y1, "x2": x2, "y2": y2})
                print(f"座位 {len(seats)}: ({x1},{y1})-({x2},{y2})")
            cur_rect = None

    cv2.namedWindow("Calibrate", cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback("Calibrate", on_mouse)

    print("拖拽画座位 | u 撤销 | r 重画 | s 保存 | q 退出")

    while True:
        canvas = frame.copy()
        # 已保存的座位
        for s in seats:
            cv2.rectangle(canvas, (s["x1"], s["y1"]), (s["x2"], s["y2"]), (0, 255, 0), 2)
            cv2.putText(canvas, f"seat{s['id']}", (s["x1"] + 4, s["y1"] + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        # 正在画的
        if cur_rect:
            cv2.rectangle(canvas, (cur_rect[0], cur_rect[1]), (cur_rect[2], cur_rect[3]), (0, 255, 255), 2)
        cv2.putText(canvas, f"seats: {len(seats)}  | u undo  r reset  s save  q quit", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        cv2.imshow("Calibrate", canvas)

        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q'), ord('Q')):
            print("未保存退出" if os.path.exists(seats_file) else "未保存退出（无历史文件）")
            break
        elif key in (ord('u'), ord('U')) and seats:
            removed = seats.pop()
            print(f"撤销座位 {removed['id']}")
        elif key in (ord('r'), ord('R')):
            seats.clear()
            print("已清空")
        elif key in (ord('s'), ord('S')):
            config = {
                "image_size": {"width": frame.shape[1], "height": frame.shape[0]},
                "seats": seats,
            }
            with open(seats_file, "w", encoding="utf-8") as fp:
                json.dump(config, fp, ensure_ascii=False, indent=2)
            print(f"已保存 {len(seats)} 个座位 -> {seats_file}")
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
