import cv2
import numpy as np
import onnxruntime as ort

MODEL_PATH = "D:/self_project/face_test/face_detect_1280.onnx"
CONF_THRESHOLD_LARGE = 0.35
CONF_THRESHOLD_SMALL = 0.15
SMALL_BOX_SIZE = 80
IOU_THRESHOLD = 0.45
INPUT_SIZE = 1280


def letterbox(img, new_shape=640):
    h, w = img.shape[:2]
    r = min(new_shape / h, new_shape / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    resized = cv2.resize(img, (nw, nh))
    top = (new_shape - nh) // 2
    bottom = new_shape - nh - top
    left = (new_shape - nw) // 2
    right = new_shape - nw - left
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return padded, r, (left, top)


def preprocess(img):
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    padded, ratio, (pad_w, pad_h) = letterbox(rgb, INPUT_SIZE)
    blob = padded.astype(np.float32) / 255.0
    blob = blob.transpose(2, 0, 1)
    blob = np.expand_dims(blob, 0)
    return blob, ratio, pad_w, pad_h


def nms(boxes, scores, iou_thresh):
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


def postprocess(output, ratio, pad_w, pad_h, orig_h, orig_w):
    pred = output[0].T
    all_scores = pred[:, 4]

    thresh = np.where(pred[:, 3] * ratio < SMALL_BOX_SIZE, CONF_THRESHOLD_SMALL, CONF_THRESHOLD_LARGE)
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

    keep = nms(boxes, scores, IOU_THRESHOLD)

    return [(boxes[i].tolist(), scores[i]) for i in keep]


def draw_results(img, results):
    for box, score in results:
        x1, y1, x2, y2 = [int(v) for v in box]
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = f"face {score:.2f}"
        tw, th = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)[0]
        cv2.rectangle(img, (x1, y1 - th - 6), (x1 + tw, y1), (0, 255, 0), -1)
        cv2.putText(img, label, (x1, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    return img


def main():
    session = ort.InferenceSession(MODEL_PATH, providers=['DmlExecutionProvider', 'CPUExecutionProvider'])
    input_name = session.get_inputs()[0].name

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    print("摄像头已打开")
    print("按 ESC 或 q 退出 | 按 s 截图保存（需点击窗口使其获得焦点）")

    import time
    frame_count = 0
    fps_start = time.time()

    FLIP_FRAME = True

    while True:
        ret, frame = cap.read()
        if not ret:
            print("无法读取画面")
            break

        if FLIP_FRAME:
            frame = cv2.flip(frame, 1)

        orig_h, orig_w = frame.shape[:2]
        blob, ratio, pad_w, pad_h = preprocess(frame)

        output = session.run(None, {input_name: blob})[0]
        results = postprocess(output, ratio, pad_w, pad_h, orig_h, orig_w)
        frame = draw_results(frame, results)

        frame_count += 1
        elapsed = time.time() - fps_start
        if elapsed >= 1.0:
            fps = frame_count / elapsed
            cv2.putText(frame, f"FPS: {fps:.1f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            frame_count = 0
            fps_start = time.time()

        cv2.imshow("Face Detection", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == 27 or key == ord('q') or key == ord('Q'):
            print("退出")
            break
        elif key == ord('s') or key == ord('S'):
            import os
            snapshot_dir = os.path.dirname(os.path.abspath(__file__))
            filename = os.path.join(snapshot_dir, f"snapshot_{int(cv2.getTickCount())}.jpg")
            cv2.imwrite(filename, frame)
            print(f"截图已保存: {filename}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
