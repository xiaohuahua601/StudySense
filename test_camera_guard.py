# -*- coding: utf-8 -*-
"""模拟验证 CameraGuard: 正常帧 -> 遮挡(黑帧) -> 恢复 -> 移动(画面剧变)"""
import numpy as np

from camera_guard import CameraGuard

guard = CameraGuard()
rng = np.random.default_rng(42)

# 模拟正常画面: 有纹理有亮度
normal = rng.integers(60, 200, (480, 640, 3), dtype=np.uint8)

# --- 阶段1: 正常帧, 不应告警 ---
for i in range(10):
    a = guard.update(normal, people_count=5)
    assert a is None, f"正常帧误报: {a}"
print("1. 正常帧无告警 ✓")

# --- 阶段2: 遮挡(纯黑帧+无人), 连续 8 帧后应触发 blocked ---
black = np.zeros((480, 640, 3), dtype=np.uint8)
alert = None
for i in range(20):
    alert = guard.update(black, people_count=0)
    if alert:
        break
assert alert and alert["type"] == "blocked", f"遮挡未触发: {alert}"
print(f"2. 遮挡触发 ✓  -> {alert['message']}")

# --- 阶段3: 恢复正常, 应触发 recovered ---
alert = None
for i in range(5):
    alert = guard.update(normal, people_count=5)
    if alert:
        break
assert alert and alert["type"] == "recovered", f"恢复未触发: {alert}"
print(f"3. 恢复通知 ✓  -> {alert['message']}")

# --- 阶段4: 移动(画面内容整体平移), 连续 4 帧后应触发 moved ---
alert = None
# 构造结构化场景（块状纹理, 模拟真实画面内容）
yy, xx = np.mgrid[0:480, 0:640]
scene_a = ((xx // 40 + yy // 40) % 2 * 100 + 60).astype(np.uint8)
scene_a = np.stack([scene_a] * 3, axis=-1)
# 先稳定在场景A
for i in range(10):
    guard.update(scene_a, people_count=3)
# 模拟摄像头被挪动: 场景整体连续大幅平移（每帧偏移 120px）
for i in range(10):
    shifted = np.roll(scene_a, shift=(i + 1) * 120, axis=1)
    alert = guard.update(shifted, people_count=3)
    if alert:
        break
assert alert and alert["type"] == "moved", f"移动未触发: {alert}"
print(f"4. 移动触发 ✓  -> {alert['message']}")

print("\n全部通过: 遮挡/恢复/移动 三类告警判定正确")
