# -*- coding: utf-8 -*-
"""
CrowdHuman odgt -> YOLO 单类 head 数据集转换脚本

用法:
    python convert_crowdhuman.py                 # 转换 train + val
    python convert_crowdhuman.py --split train   # 只转换 train
    python convert_crowdhuman.py --visualize 10  # 转换后随机抽样 10 张可视化

数据目录结构 (转换后):
    crowdhuman/
        images/train/*.jpg   images/val/*.jpg
        labels/train/*.txt   labels/val/*.txt
        data.yaml
"""

import argparse
import json
import random
from pathlib import Path

import cv2

ROOT = Path(r"d:\self_project\crowdhuman")
ODGT = {
    "train": ROOT / "annotation_train.odgt",
    "val": ROOT / "annotation_val.odgt",
}


def jpeg_size(path: Path):
    """解析 JPEG header 获取 (宽, 高)，不解码整图，速度快。"""
    with open(path, "rb") as f:
        data = f.read(256 * 1024)  # 前 256KB 足够找到 SOF marker
    i = 2  # 跳过 FFD8
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0/1/2/3/9/10/11/14/15 都含尺寸信息
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC9, 0xCA, 0xCB, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        # 其他 marker: 按 length 字段跳过
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg_len
    raise ValueError(f"未找到 SOF marker: {path}")


def convert_split(split: str):
    img_dir = ROOT / "images" / split
    lbl_dir = ROOT / "labels" / split
    lbl_dir.mkdir(parents=True, exist_ok=True)

    stats = {"images": 0, "boxes": 0, "skipped_mask": 0,
             "skipped_ignore": 0, "skipped_invalid": 0, "missing_img": 0}

    with open(ODGT[split], "r", encoding="utf-8") as f:
        lines = f.readlines()

    for idx, line in enumerate(lines):
        item = json.loads(line)
        img_id = item["ID"]
        img_path = img_dir / f"{img_id}.jpg"
        if not img_path.exists():
            stats["missing_img"] += 1
            continue

        w, h = jpeg_size(img_path)
        yolo_lines = []
        for gt in item.get("gtboxes", []):
            if gt.get("tag") == "mask":
                stats["skipped_mask"] += 1
                continue
            extra = gt.get("extra", {})
            head_attr = gt.get("head_attr", {})
            if extra.get("ignore", 0) == 1 or head_attr.get("ignore", 0) == 1:
                stats["skipped_ignore"] += 1
                continue

            x, y, bw, bh = gt["hbox"]
            # 裁剪到图片边界
            x1, y1 = max(0.0, x), max(0.0, y)
            x2, y2 = min(float(w), x + bw), min(float(h), y + bh)
            if x2 - x1 < 2 or y2 - y1 < 2:  # 无效框
                stats["skipped_invalid"] += 1
                continue

            # YOLO 归一化: cx cy w h
            cx = (x1 + x2) / 2 / w
            cy = (y1 + y2) / 2 / h
            nw = (x2 - x1) / w
            nh = (y2 - y1) / h
            yolo_lines.append(f"0 {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

        (lbl_dir / f"{img_id}.txt").write_text("\n".join(yolo_lines), encoding="utf-8")
        stats["images"] += 1
        stats["boxes"] += len(yolo_lines)

        if (idx + 1) % 2000 == 0:
            print(f"  [{split}] {idx + 1}/{len(lines)} 已处理")

    print(f"[{split}] 完成: {stats}")
    return stats


def write_data_yaml():
    content = (
        f"path: {ROOT.as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "nc: 1\n"
        "names:\n"
        "  0: head\n"
    )
    (ROOT / "data.yaml").write_text(content, encoding="utf-8")
    print(f"data.yaml 已写入: {ROOT / 'data.yaml'}")


def visualize(split: str, n: int):
    """随机抽 n 张画框保存到 visual_check/ 目录。"""
    out_dir = ROOT / "visual_check"
    out_dir.mkdir(exist_ok=True)
    lbl_dir = ROOT / "labels" / split
    img_dir = ROOT / "images" / split
    samples = random.sample(list(lbl_dir.glob("*.txt")), min(n, len(list(lbl_dir.glob('*.txt')))))

    for lbl_path in samples:
        img = cv2.imread(str(img_dir / f"{lbl_path.stem}.jpg"))
        H, W = img.shape[:2]
        for line in lbl_path.read_text().splitlines():
            parts = line.split()
            _, cx, cy, bw, bh = map(float, parts)
            x1 = int((cx - bw / 2) * W)
            y1 = int((cy - bh / 2) * H)
            x2 = int((cx + bw / 2) * W)
            y2 = int((cy + bh / 2) * H)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.imwrite(str(out_dir / f"{lbl_path.stem}.jpg"), img)
    print(f"可视化结果已保存到: {out_dir} ({len(samples)} 张)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["train", "val", "both"], default="both")
    parser.add_argument("--visualize", type=int, default=0, help="抽样可视化张数")
    args = parser.parse_args()

    random.seed(42)
    splits = ["train", "val"] if args.split == "both" else [args.split]
    for s in splits:
        convert_split(s)
    write_data_yaml()
    if args.visualize > 0:
        visualize("train", args.visualize)
