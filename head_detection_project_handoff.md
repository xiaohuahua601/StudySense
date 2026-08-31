# 智能自习室座位监测系统 —— 项目交接报告

> 生成时间：2026-08-28
> 用途：新会话继续推进项目。本文档包含完整背景、已完成工作、下一步计划。

***

## 一、项目概述

**一句话介绍（面试用）：**

> 基于自训 YOLOv8 人头检测模型的密集人群统计系统——解决人脸检测在背面/远距场景的漏检问题，配合 FastAPI + MySQL + Streamlit 构成完整分析链路，Docker 一键部署。

**目标场景：** 自习室/图书馆座位占用监测（顶置摄像头俯拍，学生看到的基本是头顶和后脑勺，人脸检测失效，头部检测是正确方案）

**面试核心话术（30 秒版）：**

> "我做了一个自习室座位监测系统。核心难点是俯拍视角下人脸不可见，常规人脸检测失效，所以我基于 CrowdHuman 数据集训练了头部检测模型。系统包含模型训练、ONNX GPU 推理优化、FastAPI 服务、MySQL 时序统计和 Streamlit 可视化，全部 Docker 化部署。"

***

## 二、用户环境信息

| 项目        | 值                                                                     |
| --------- | --------------------------------------------------------------------- |
| 系统        | Windows 11                                                            |
| GPU       | RTX 5070 Laptop（8GB 显存）                                               |
| 内存        | 32GB                                                                  |
| WSL       | Ubuntu（用户 raina），训练环境已就绪（ultralytics + torch CUDA）                    |
| Python 环境 | conda：`face_test`（Windows 侧，已装 onnxruntime-directml + opencv + numpy） |
| 用户偏好      | 每步操作必须先征得同意再执行；中文交流；喜欢 Docker 一键部署（依赖全打包）；先本地 IDE 测试再容器化              |
| 项目目录      | `D:\self_project\face_test\`（Windows）                                 |

***

## 三、已完成工作（截至 2026-08-28）

### 1. 人脸检测模型（完整闭环，mAP50=0.99）

- 数据集：`/home/raina/face_detection/face_only_dataset/`（WSL 内，7780 训练 + 679 验证，软链接结构，单类 face）
- 训练结果：`/home/raina/face_detection/runs/face_only_v1-2/weights/best.pt`
  - mAP50 = 0.990，mAP50-95 = 0.579，epoch 77 最佳
  - 训练配方：yolov8s + SGD + lr0=0.01 + cos\_lr + patience=30 + batch=16 + imgsz=640
- **历史教训**：AdamW + lr0=0.01 会导致 loss 振荡不收敛（用户遇到过），SGD + 0.01 是正确组合

### 2. 摄像头实时检测（Windows，GPU 加速）

- 主脚本：`D:\self_project\face_test\face_detect.py`
- 模型：`face_detect_1280.onnx`（1280 分辨率版，DirectML GPU 推理，\~22 FPS）
- 已实现的关键特性：
  - letterbox 预处理 + 向量化后处理（NumPy）
  - **分级阈值**：小框（高<80px）用 0.15，大框用 0.35——解决"远处小脸漏检 vs 大框误检"矛盾
  - NMS、镜像翻转开关（FLIP\_FRAME）、FPS 显示、ESC/q 退出
- 备用模型：`face_detect.onnx`（640 版）
- 数据集构建脚本：`create_face_dataset.py`

### 3. 摄像头硬件结论

- 笔记本内置摄像头上限 **1280×720**，不支持 1080p（已探测确认）
- 平视角可验证背面检测（转身背对镜头的演示镜头）
- 未来模拟俯拍：手机 + DroidCam/iVCam 推流，改 VideoCapture 源即可

***

## 四、当前进行中的任务（新会话从这里继续）

### 阶段 0：CrowdHuman 数据集获取（正在下载）

**背景**：官网 crowdhuman.org 的百度盘链接部分失效（train01 和 val 失效）。

**替代方案（已找到）**：HyperAI 种子下载

- 页面：<https://hyper.ai/cn/datasets/17239>
- 下载 CrowdHuman.torrent → 用迅雷/qBittorrent 下载
- 内容：train01(5.8GB) + train02(8.7GB) + train03(10.8GB) + val + annotation\_train.odgt + annotation\_val.odgt
- 用户正在下载，目标目录：`D:self_project\crowdhuman\`

**数据格式说明（转换脚本要用）：**

- odgt 格式：每行一个 JSON，`{"ID": 图片名, "gtboxes": [gtbox...]}`
- gtbox 含 `hbox: [x, y, w, h]`（头框）、`fbox`（全身）、`vbox`（可见区域）
- `tag: "mask"` 的框是忽略区域；`extra.ignore=1` 或 `head_attr.ignore=1` 的头框要跳过
- **转换目标**：提取 hbox → YOLO txt 格式（单类 head，class 0），归一化坐标

### 后续阶段总览（完整流程已在旧会话确认）

| 阶段 | 内容         | 关键点                                                                                                                          |
| -- | ---------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 0  | 数据准备       | 转换脚本 + data.yaml + 抽样可视化验证                                                                                                   |
| 1  | 训练 head 模型 | yolov8s + SGD/lr0=0.01/cos\_lr/patience=30，**imgsz=960**，batch=16（显存不够降到 8），mAP50 目标 ≥0.85，导出 ONNX                           |
| 2  | 模块重构       | face\_detect.py → `HeadDetector` 类；拍演示视频（正脸→转身背对镜头）                                                                          |
| 3  | 后端         | FastAPI（POST /api/report, GET /api/stats, /api/seats）+ MySQL（records 表：时间戳/机位/人数/坐标 JSON）+ 座位区域映射 + edge\_agent.py（30 秒采样上报） |
| 4  | 看板         | Streamlit：实时人数 + 24h 曲线 + 座位网格图；Ollama+qwen2.5 生成自然语言日报（用户已有 qwen2.5、bge-m3、bge-large-zh）                                    |
| 5  | 部署         | Docker Compose（api + mysql + dashboard + ollama）；README + 架构图 + 演示视频                                                         |

**里程碑验收点：**

- 0.3：抽样 10 张可视化，头部框贴合
- 1.2：mAP50 ≥ 0.85（不够就升 yolov8m）
- 2.2：**背对摄像头仍能检出**（核心验收，不过则项目方向有问题）
- 5.3：别人 clone 后 docker compose up 能跑

**风险预案：**

- 显存不够（8GB）→ batch 降到 8 或 imgsz 回 640
- 平视背面效果一般 → 侧 45° 角演示也可证明（头可见脸不可见）

***

## 五、历史环境教训（新会话注意）

1. **沙箱限制**：改系统设置（页面文件、Optimize-VHD）需要用户在管理员终端手动执行；pip install 会报沙箱警告但实际成功
2. **页面文件**：曾从自动管理改为固定 4GB 导致运行卡顿，已指导用户恢复自动管理（`Set-CimInstance` 方式，wmic 在 Win11 已移除）——**新会话若涉及系统设置，直接给用户手动命令**
3. **WSL vhdx**：曾压缩（60GB→43GB），数据完整无损，训练环境正常
4. 用户 32GB 内存 + C 盘已清理到 90GB+ 可用，D 盘 247GB 可用，WSL 916GB 可用——磁盘不是约束
5. 摄像头索引 0 可用，OpenCV 读取正常

***

## 六、新会话第一步行动建议

1. 确认用户 CrowdHuman 下载进度（`D:\self_project\crowdhuman\` 是否就绪）
2. 若数据就绪：写 `convert_crowdhuman.py`（odgt → YOLO 单类 head），先转换 + 可视化抽样验证
3. 若数据未就绪：先做 `HeadDetector` 模块重构（不依赖数据，把现有 face 检测能力封装成类，head 模型训好后换 MODEL\_PATH 一行切换）
4. 每步操作前征求用户同意（用户的硬性要求）

