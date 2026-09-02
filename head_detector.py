# -*- coding: utf-8 -*-
"""
HeadDetector: 人头检测模块（自习室座位监测系统核心检测器）

基于 CrowdHuman 训练的 YOLOv8 head 模型（ONNX, DirectML GPU 推理）。
解决了俯拍视角下人脸不可见、人脸检测失效的问题。

用法:
    detector = HeadDetector()                                   # 默认 1280 模型
    detector = HeadDetector("models/head_detect_960.onnx", input_size=960)
    results = detector.detect(frame)                # [(box, score), ...]
    frame = detector.draw(frame, results)
"""

import os
import time

import cv2
import numpy as np
import onnxruntime as ort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(BASE_DIR, "models", "head_detect_1280.onnx")


class HeadDetector:
    """人头检测器: ONNX 推理 + letterbox 预处理 + 分级阈值 + NMS 后处理"""

    def __init__(
        self,
        model_path=DEFAULT_MODEL,
        input_size=1280,
        conf_large=0.35,
        conf_small=0.15,
        small_box_size=80,
        iou_thresh=0.45,
    ):
        """
        Args:
            model_path: ONNX 模型路径
            input_size: 模型输入分辨率（须与导出时一致）
            conf_large: 大框置信度阈值
            conf_small: 小框置信度阈值（远处小头，低阈值防漏检）
            small_box_size: 小框判定像素数（原始画面中框高低于此值为小框）
            iou_thresh: NMS IoU 阈值
        """
        self.input_size = input_size
        self.conf_large = conf_large
        self.conf_small = conf_small
        self.small_box_size = small_box_size
        self.iou_thresh = iou_thresh

        self.session = ort.InferenceSession(
            model_path, providers=["DmlExecutionProvider", "CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    # ---------- 预处理 ----------

    def _letterbox(self, img):
        h, w = img.shape[:2]
        r = min(self.input_size / h, self.input_size / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh))
        top = (self.input_size - nh) // 2
        bottom = self.input_size - nh - top
        left = (self.input_size - nw) // 2
        right = self.input_size - nw - left
        padded = cv2.copyMakeBorder(
            resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114)
        )
        return padded, r, (left, top)

    def _preprocess(self, img):
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        padded, ratio, (pad_w, pad_h) = self._letterbox(rgb)
        blob = padded.astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)
        blob = np.expand_dims(blob, 0)
        return blob, ratio, pad_w, pad_h

    # ---------- 后处理 ----------

    @staticmethod
    def _nms(boxes, scores, iou_thresh):
        if len(boxes) == 0:
            return []
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0, xx2 - xx1)
            h = np.maximum(0, yy2 - yy1)
            inter = w * h
            ovr = inter / (areas[i] + areas[order[1:]] - inter)
            inds = np.where(ovr <= iou_thresh)[0]
            order = order[inds + 1]
        return keep

    def _postprocess(self, output, ratio, pad_w, pad_h, orig_h, orig_w):
        pred = output[0].T
        all_scores = pred[:, 4]

        # 分级阈值: 原始画面中的框高（pred[:,3]*ratio）小于 small_box_size 用低阈值
        thresh = np.where(
            pred[:, 3] * ratio < self.small_box_size, self.conf_small, self.conf_large
        )
        mask = all_scores > thresh
        if not mask.any():
            return []

        candidates = pred[mask]
        scores = all_scores[mask]

        boxes = np.empty((len(candidates), 4), dtype=np.float32)
        boxes[:, 0] = (candidates[:, 0] - candidates[:, 2] / 2 - pad_w) / ratio
        boxes[:, 1] = (candidates[:, 1] - candidates[:, 3] / 2 - pad_h) / ratio
        boxes[:, 2] = (candidates[:, 0] + candidates[:, 2] / 2 - pad_w) / ratio
        boxes[:, 3] = (candidates[:, 1] + candidates[:, 3] / 2 - pad_h) / ratio

        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, orig_w)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, orig_h)

        keep = self._nms(boxes, scores, self.iou_thresh)
        return [(boxes[i].tolist(), float(scores[i])) for i in keep]

    # ---------- 对外接口 ----------

    def detect(self, img):
        """检测一帧 BGR 图像, 返回 [([x1,y1,x2,y2], score), ...]"""
        orig_h, orig_w = img.shape[:2]
        blob, ratio, pad_w, pad_h = self._preprocess(img)
        output = self.session.run(None, {self.input_name: blob})[0]
        return self._postprocess(output, ratio, pad_w, pad_h, orig_h, orig_w)

    @staticmethod
    def draw(img, results, label="head"):
        for box, score in results:
            x1, y1, x2, y2 = [int(v) for v in box]
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
            text = f"{label} {score:.2f}"
            tw, th = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)[0]
            cv2.rectangle(img, (x1, y1 - th - 6), (x1 + tw, y1), (0, 0, 255), -1)
            cv2.putText(img, text, (x1, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        return img


def main():
    detector = HeadDetector()

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    print("摄像头已打开")
    print("按 ESC 或 q 退出 | 按 s 截图 | 按 f 切换镜像")

    frame_count = 0
    fps_start = time.time()
    flip = True

    while True:
        ret, frame = cap.read()
        if not ret:
            print("无法读取画面")
            break

        if flip:
            frame = cv2.flip(frame, 1)

        results = detector.detect(frame)
        frame = HeadDetector.draw(frame, results)

        # 人数统计（后续对接 FastAPI 的核心数字）
        cv2.putText(frame, f"count: {len(results)}", (10, 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        frame_count += 1
        elapsed = time.time() - fps_start
        if elapsed >= 1.0:
            fps = frame_count / elapsed
            cv2.putText(frame, f"FPS: {fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            frame_count = 0
            fps_start = time.time()

        cv2.imshow("Head Detection", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q'), ord('Q')):
            print("退出")
            break
        elif key in (ord('s'), ord('S')):
            filename = os.path.join(BASE_DIR, f"head_snapshot_{int(cv2.getTickCount())}.jpg")
            cv2.imwrite(filename, frame)
            print(f"截图已保存: {filename}")
        elif key in (ord('f'), ord('F')):
            flip = not flip
            print(f"镜像: {flip}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
