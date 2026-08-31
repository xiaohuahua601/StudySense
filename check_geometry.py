# -*- coding: utf-8 -*-
"""几何合理性校验: 头框(hbox) 应位于全身框(fbox)顶部且被包含"""
import json
import random

random.seed(1)
lines = open(r"d:\self_project\crowdhuman\annotation_train.odgt", encoding="utf-8").readlines()
sample = random.sample(lines, 500)

inside = top_half = total = 0
ratio_sum = head_vs_body = 0.0
for line in sample:
    item = json.loads(line)
    for gt in item.get("gtboxes", []):
        if gt.get("tag") == "mask":
            continue
        hx, hy, hw, hh = gt["hbox"]
        fx, fy, fw, fh = gt["fbox"]
        total += 1
        hcx, hcy = hx + hw / 2, hy + hh / 2
        if fx <= hcx <= fx + fw and fy <= hcy <= fy + fh:
            inside += 1
        if hcy <= fy + fh * 0.4:
            top_half += 1
        ratio_sum += hw / max(hh, 1)
        head_vs_body += hw / max(fw, 1)

print(f"总头框数: {total}")
print(f"头框中心在全身框内: {inside/total:.1%}")
print(f"头框中心在全身框上40%区域: {top_half/total:.1%}")
print(f"头框平均宽高比(应接近1): {ratio_sum/total:.2f}")
print(f"头宽/全身宽平均比(正常约0.3-0.4): {head_vs_body/total:.2f}")
