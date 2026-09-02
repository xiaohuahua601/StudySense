# StudySense · 智能自习室监测系统

基于边缘计算的多人实时监测系统：在每间教室的摄像头上运行人头检测模型，实时统计**当前人数、空余座位、在座时长**，监测**摄像头异常**，并通过本地大模型提供**自然语言数据问答**（"今天几点人最少？"）。适用于自习室/图书馆的运营管理。

系统包含三类前端：**挂墙可视化大屏**（实时监控）、**双角色管理门户**（普通用户：占用总览/收藏/历史规律/座位预测/空座提醒/AI 问答；管理员：教室与用户管理）、**登录注册页**（访客可看全馆实时统计）。

> 面试/求职读者请配合 [docs/项目详细介绍与面试要点.md](docs/项目详细介绍与面试要点.md)（项目详细介绍 + 面试要点指南）阅读。

> 本文档面向不熟悉本项目的读者，从零开始解释系统的每个部分是如何实现的，包括模型怎么训练出来、数据怎么流动、每个模块内部的算法逻辑。

---

## 目录

1. [系统架构设计](#一系统架构设计)
2. [模型训练全流程](#二模型训练全流程)
3. [核心模块详细设计](#三核心模块详细设计)
4. [数据库设计](#四数据库设计)
5. [API 接口文档](#五api-接口文档)
6. [一次完整的数据旅程](#六一次完整的数据旅程)
7. [快速开始](#七快速开始)
8. [无实景测试方案](#八无实景测试方案)
9. [项目结构](#九项目结构)
10. [核心指标与测试结果](#十核心指标与测试结果)
11. [技术决策记录（ADR）](#十一技术决策记录adr)

---

## 一、系统架构设计

### 1.1 整体架构：一条数据流水线

系统由五个部分组成，数据像工厂流水线一样单向流动：

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            整体数据流                                       │
│                                                                            │
│  [摄像头]                                                                  │
│     │                                                                      │
│     ▼                                                                      │
│  ① 边缘采集端 edge_agent.py          （教室电脑上, 检测+跟踪+异常守卫）      │
│     │  每 30 秒 HTTP POST /api/report（只传数字, 不传画面）                 │
│     ▼                                                                      │
│  ② 后端服务 server.py:8000           （中枢: 收数据/存库/供查询/调AI）       │
│     │                    │                                                 │
│     ▼                    ▼                                                │
│  ③ MySQL 库 study_room   ⑤ Ollama 本地大模型（qwen2.5:1.5b, Text2SQL）      │
│     │                                                                      │
│     ▼                                                                      │
│  ④ 可视化大屏 /ui/ + 管理门户 /portal/ (均为8000端口, 门户为 SSR 多页应用)  │
└──────────────────────────────────────────────────────────────────────────┘
```

用一句话概括：**摄像头画面 → AI 数人头 → 存进数据库 → 网页展示 + AI 问答**。

### 1.2 三个关键架构决策（为什么这样设计）

**决策一：检测在"边缘端"做，不在服务器做。**

最直观的做法是把视频流全部传到服务器，在服务器上跑 AI。我们没有这样做——而是在**每间教室的电脑上直接运行检测**，只把"人数"这个数字（几十字节）传给后端。三个理由：

- **带宽**：一路 720p 视频约 2~4 Mbps，10 间教室全部回传对校园网压力很大；而"人数"每 30 秒传一次，几乎不占带宽
- **隐私**：人脸/人像画面不出本机，服务器只见到数字，天然规避隐私合规问题
- **成本**：服务器不需要 GPU，一台普通机器就能带几十间教室

**决策二：不做人头到座位的一对一映射。**

早期版本实现过"标定每个座位的屏幕区域，看头框中心落在哪个区域"。实测发现俯拍视角下这个映射不可靠：头会偏移、遮挡、两个人靠近时区域互相干扰，导致占用判定抖动。最终改为：

> **人数 = 检测并跟踪确认的人数；空位 = 教室注册的固定座位数 − 人数。**

把不确定的问题（谁坐在哪）换成了确定的问题（有几个人）。座位是教室的固有属性（注册时填总座位数），人是流动的——用固定的分母减流动的分子，比坐标映射稳健得多。

**决策三：AI 问答用 Text2SQL，不用向量检索（RAG）。**

我们的数据是结构化的时序数字（几点几分几人、每段会话多久），SQL 查询是精确的（"15点平均29.2人"），而语义检索只能给出模糊相似的结果。此场景下 Text2SQL 是正确工具。详见 [模块六](#36-模块六ai-问答text2sql--本地-ollama)。

### 1.3 技术栈总览

| 层 | 技术 | 选型理由 |
|---|---|---|
| 检测模型 | YOLOv8s → ONNX | 单类检测够用；s 版在精度/速度间平衡 |
| 推理引擎 | onnxruntime-directml | Windows 上免装 CUDA 全家桶，DirectML 走 GPU |
| 跟踪 | 纯 NumPy（自研 IOU 跟踪器） | 场景简单（坐着为主），引 ByteTrack 属于过度设计 |
| 后端 | FastAPI + PyMySQL | 异步、自动 Swagger 文档、轻量 |
| 数据库 | MySQL 8.0 | 时序快照 + 会话记录，关系型足够 |
| 大屏 | 单文件 HTML + ECharts | 零构建、挂任意屏幕、风格完全可控 |
| 管理门户 | Jinja2 SSR 多页应用（FastAPI 同源渲染） | 页面权限服务端判定；HttpOnly Cookie 会话 + CSRF + 安全响应头 |
| 认证 | PBKDF2 + HMAC 签名 Token | 无 JWT 依赖、无状态验证；Cookie 双通道（编程走 Bearer 头） |
| LLM | Ollama + qwen2.5:1.5b（可切 7b） | 本地运行，数据不出内网；1.5b 热响应 ~1.7s |

---

## 二、模型训练全流程

这一章解释检测模型是怎么从零训练出来的——面试时最常被追问的部分。

### 2.1 数据集：CrowdHuman

- **来源**：CrowdHuman（旷视开源的密集人群检测数据集），15,000 张训练图 + 4,370 张验证图，多为街景/校园的远景人群俯拍
- **为什么选它**：标注里有"全身框 + 头框"两类，且包含**背对镜头**的人——这正是项目核心验收点（人脸检测模型检不出背面，必须用头检测）
- **标注规模**：训练集 331,991 个头框，平均每图 22 个头，天然学会密集场景

### 2.2 数据转换（convert_crowdhuman.py）

CrowdHuman 原始格式是 odgt（每行一个 JSON），YOLO 需要 `类别 cx cy w h`（归一化）格式。转换时**不是简单转坐标**，关键是官方标注规则的过滤：

| 过滤项 | 数量(train) | 原因 |
|---|---|---|
| mask 区域内的框 | 85,809 | 被遮挡超过阈值的人，头框质量差 |
| ignore 标记的框 | 20,178 | 官方明确忽略的框（画面边缘裁切等） |
| 无效框（宽高≤0） | 814 | 坐标越界 |

**质量校验**——转换完成后抽样 500 图、11,420 个头框做几何合理性检查：

- 头框中心落在对应全身框内：**99.9%**
- 头框位于全身框上 40% 高度区域：**96.5%**
- 头框平均宽高比 0.86（接近方形，符合头部形状）
- 头宽 / 全身宽 ≈ 0.38（符合人体比例）

校验通过才说明"头框标得对"，而不是盲目喂给模型。

### 2.3 训练配方（WSL + RTX 5070 Laptop 8GB）

```bash
yolo detect train model=yolov8s.pt data=crowdhuman/data.yaml \
  imgsz=768 batch=8 epochs=100 \
  optimizer=SGD lr0=0.01 cos_lr=True patience=30 workers=4
```

每个参数的为什么：

| 参数 | 值 | 理由 |
|---|---|---|
| imgsz | 768 | 8GB 显存跑 960 会 OOM；768 是显存上限内的最大值 |
| batch | 8 | 同上，显存约束 |
| optimizer | **SGD** | 实测 AdamW 在此任务上振荡不收敛（损失下不去），SGD 稳定 |
| lr0 | 0.01 | YOLO 系标准初始学习率 |
| cos_lr | True | 余弦退火，后期小学习率精修小目标 |
| patience | 30 | 连续 30 epoch 无提升早停，实际第 81 epoch 停止 |

**结果：768 分辨率下 val mAP50 = 0.789**。

### 2.4 关键工程技巧：训练 768、推理 1280

训练分辨率和推理分辨率**可以不一致**。小目标（远景人头）在高分辨率下更清楚，于是导出两版 ONNX：

```bash
yolo export ... imgsz=1280   # 精度优先
yolo export ... imgsz=960    # 速度与精度折中
```

同一权重在三个推理分辨率下的 val 表现：

| 推理分辨率 | mAP50 | mAP50-95 | 单帧耗时 |
|---|---|---|---|
| 768（训练档） | 0.789 | 0.494 | 2.3ms |
| 960 | 0.819 | 0.524 | 11.9ms |
| **1280** | **0.842** | **0.546** | 152.5ms |

零额外训练成本，mAP50 提升 **5.3 个点**。实际部署选 1280 版（自习室场景帧率要求不高）。

### 2.5 分级置信度阈值

统一阈值的问题：远处小头置信度天然低（可能 0.2），近处大头置信度高（0.8）。阈值定 0.35 会漏掉小头，定 0.15 会大框误检泛滥。解法：

```python
# 小框（宽<32px）用低阈值 0.15，大框用高阈值 0.35
threshold = np.where(pred_w * ratio < 32, 0.15, 0.35)
```

这一招来自人脸检测阶段的实践经验：**小目标漏检是小阈值问题，不是模型问题**。

---

## 三、核心模块详细设计

### 3.1 模块一：人头检测器（head_detector.py）

**职责**：输入一帧 BGR 图像，输出 `[([x1,y1,x2,y2], score), ...]`。

**每帧处理流水线：**

```
原始图片 (如 1920×1080)
  → ① letterbox 等比缩放到 1280×1280（短边补灰边, 不变形）
  → ② BGR→RGB, 归一化到 0~1, HWC→CHW, 加 batch 维
  → ③ onnxruntime-directml 推理（输出 8400×5: [cx,cy,w,h,score]）
  → ④ 分级阈值过滤（见 2.5）
  → ⑤ NMS 去重（IoU>0.45 的重叠框只留最高分）
  → ⑥ 坐标除以缩放比、减去补边偏移 → 映射回原图
  → 输出头框列表
```

为什么要 letterbox 而不是直接 resize：直接拉伸会把人头压扁/拉长，与训练数据分布不符；letterbox 保持长宽比，模型见到的是"正常比例的小图"。

### 3.2 模块二：多目标跟踪器（tracker.py）

**职责**：给每个检测到的人分配持续不变的 ID，产出"在座会话"。

**为什么逐帧数人头不够**：检测器每帧独立工作，同一个人这帧检到、下帧漏了，人数就跳动（19→21→19）。跟踪器用**帧间关联**平滑抖动。

**算法（每帧执行 `tracker.update(检测框列表)`）：**

```
1. 预测: 每条现有轨迹按上帧速度预测当前位置（匀速模型）
2. 匹配: 新检测框 vs 所有轨迹
   - 计算 IoU 矩阵（重叠度）
   - IoU > 0.25 的配对中贪心选最优
   - IoU 不够但质心距离近的兜底配对（头小幅度移动时 IoU 可能低于阈值）
3. 更新: 匹配上的轨迹更新位置、连续命中数 +1
4. 确认: 连续命中 ≥ 3 帧（min_hits）→ 轨迹"确认"（计入人数）
   ↳ 为什么: 单帧误检（椅背/海报图案）活不过 3 帧, 天然过滤
5. 老化: 未匹配的轨迹失配计数 +1, 连续失配 > 12 帧（max_age）→ 判定离场
   ↳ 为什么: 低头/被挡导致短暂漏检（几帧）不会误判离开
6. 归档: 离场轨迹 → 会话记录 (track_id, 开始时间, 结束时间, 时长)
```

**参数：** `iou_threshold=0.25, max_age=12, min_hits=3`——均为在真实自习视频上调优的结果。

**产出指标（`/api/duration` 聚合）：** 当前人数 = 确认轨迹数；人均在座时长 = 会话总时长 / 会话数；最长单次在座 = max(duration_sec)。

### 3.3 模块三：摄像头异常守卫（camera_guard.py）

**职责**：零额外模型，判断"镜头被挡了"和"摄像头被挪了"。

| 异常 | 判定信号 | 确认条件 | 为什么有效 |
|---|---|---|---|
| **遮挡** | ① 画面亮度骤降 ② 拉普拉斯方差（纹理复杂度）趋零 ③ 检测人数为 0，三者**同时**满足 | 连续 8 帧 | 被挡的画面又暗又平（纯色）且无人；三个条件与降低误报（夜晚教室本来就暗但纹理仍在） |
| **移动** | 相邻帧差分后变化像素占比 > 阈值 | 连续 4 帧 | 静止机位下正常画面几乎无变化；被挪动时全画面剧变 |

**防抖设计**：异常→正常自动发恢复通知；60 秒告警冷却（同一异常不重复刷屏）。

### 3.4 模块四：边缘采集端（edge_agent.py）

**职责**：教室电脑上的常驻进程，串联全部边缘逻辑。

```
输入源(摄像头0/视频文件/图片)
  → 循环取帧 → HeadDetector.detect() → IOUTracker.update()
  → CameraGuard.check(帧, 人数)
  → 每 interval 秒: POST /api/report {camera_id, head_count, occupied_count}
  → 会话归档时: 会话随下一次 report 一起上报
  → 结束(视频播完/Ctrl+C): flush_remaining_sessions() 把在场的人也归档上报, 数据不丢
```

命令行参数：`--source`（0=摄像头/文件路径）、`--camera-id`（绑定教室）、`--interval`（上报周期，默认 30s）、`--no-show`（无头运行）。

### 3.5 模块五：可视化大屏 + 管理页

**大屏（web/index.html，FastAPI 托管在 `/ui/`）**

布局：顶部 KPI 行（教室数/总人数/占用座位/总占用率）→ 教室卡片墙（点击联动右侧详情）→ 详情区（人数/空位/占用率 + 24h 趋势曲线 + 时长统计 + 座位点阵）+ 底部摄像头告警条。

**实时感设计**（监控页的核心体验，数据 5 秒轮询 + 三层视觉反馈）：

1. **数值变化闪烁**：KPI 数字变化时青色辉光 + 放大动画，涨跌显示 `▲ +3 / ▼ -1` 角标
2. **相对时间跳动**：每张卡片的上报时间显示"刚刚 → 14秒前 → 3分钟前"**每秒更新**，10 秒内绿色、超 1 小时变红——一眼看出哪间教室掉线
3. **趋势曲线流式追加**：曲线独立 2.5 秒刷新（比主刷新快一倍），新数据点带 600ms 动画滑入、60 点滚动窗口旧点滑出、末端呼吸光点随最新值跳动；同一分钟内的多次上报只更新最后一个点（平滑不抖）

**管理门户（templates/portal/，:8000，Jinja2 SSR 多页应用）**

由 FastAPI 同源渲染（`/portal/login` `/portal/overview` 等 8 个页面），与 API 同端口部署、无需独立容器。会话存 **HttpOnly Cookie**（JS 不可读，XSS 偷不走），页面权限在服务端渲染时判定：未登录 302 到登录页、普通用户访问管理页被拦截、管理员访问预测/历史规律页重定向回总览。进入后左侧竖排导航切换页面。

- **登录页**：左右分栏——左侧品牌区（Logo + 特性卡 + **未登录可见的实时统计**：教室数/座位数/当前空座），右侧登录/注册滑块切换
- **占用总览**（所有角色）：教室卡片（进度条流光 + 悬停 3D 倾斜 + 点击涟漪）、搜索 + 占用状态筛选（宽松/适中/紧张）、⭐收藏（localStorage 持久化，可只看收藏）、座位变化提示
- **教室详情弹窗**：座位示意点阵（按占用数从左至右填充，绿=占用/蓝=空闲，悬停放大辉光、无文字提示）+ 24h 趋势双曲线（占用/人数 + 均值线，超 200 点降采样）
- **历史规律页签**（"什么时候去自习"决策）：
  - 黄金时段 Top3 卡片（如"周一 16:00–17:00 空座率 88%"）+ 高峰"避开"时段卡片
  - 7 天 × 24 小时占用率热力图（ECharts heatmap，深蓝→红渐变，悬停数值）
  - 近 7 天日均值柱状图（渐变柱 + 均值虚线）；教室筛选（全馆/单教室）
- **空座提醒**：授权浏览器 Notification 后，每 15 秒检查收藏教室，空座从 <3 翻转为 ≥3 时弹系统通知（5 分钟冷却）
- **座位预测页签**（普通用户专属，管理员无入口）：预估未来 1~12 小时各教室空位数——基于过去 12 周**同星期同小时**的日均人数（数据不足时回退到同小时历史日均），按空位降序推荐教室⭐置顶，附样本量分级置信度；可解释的统计基线模型，预测方法直接展示给用户
- **AI 助手**：聊天气泡 + 打字动画 + 快捷提问芯片（含"一周哪些时段最空""明天几点去最空"）；回答只给结论不展示 SQL 语句（后端调试接口保留 sql 字段）。AI 问答仅存在于管理门户，大屏不做问答
- **管理员专属**：教室管理（新增/编辑/删除，可选级联清历史数据）、用户管理（新增/升降管理员/重置密码/删除，自我保护：不能删自己）

**为什么弃用 Streamlit 与纯前端 SPA**：早期管理页用 Streamlit 开发效率高，但无法与大屏保持统一视觉风格且镜像臃肿（~900MB）；中期改为原生 JS 单页应用（风格统一、零构建），但 token 存 localStorage 有 XSS 风险、权限判定全在浏览器端。最终改为 **Jinja2 SSR 多页应用**：安全（服务端权限判定 + HttpOnly Cookie + CSRF 双保险 + CSP 等安全响应头 + 登录防爆破）、体验不打折（同一套 CSS/动效），还省掉一个独立容器。

**门户安全设计**（相对纯前端 SPA 的加固点）：

- **HttpOnly Cookie 会话**：token 由服务端 `Set-Cookie` 写入，JS 完全不可读；`SameSite=Strict` 拦截跨站携带
- **CSRF 防护**：Cookie 会话下的写操作强制校验 `X-Requested-With` 自定义头（跨站表单/图片无法伪造）
- **安全响应头**：CSP（仅允许自身 + ECharts CDN）、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`；会话相关页面 `Cache-Control: no-store`
- **登录防爆破**：同一用户名连续失败 5 次锁定 15 分钟
- **双通道认证**：浏览器走 Cookie，编程调用走 `Authorization: Bearer`（兼容脚本/大屏）

### 3.6 模块六：AI 问答（Text2SQL + 本地 Ollama）

**职责**：中文问数据问题 → 中文回答。

**完整链路（5 步，全部本地）：**

```
用户问题 "今天几点人最少？"
  + 提示词（表结构 Schema + 规则: 只生成 SELECT）
        ↓ ①
Ollama (qwen2.5:1.5b) 生成 SQL
        ↓ ②
安全校验:
  - 只放行 SELECT / WITH 开头
  - 黑名单拦截 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE
  - 剥离可能的 markdown 围栏（```sql ... ```）
        ↓ ③
执行: 包装为子查询, 强制 LIMIT 50
        ↓ ④
若 SQL 报错 → 把 MySQL 错误信息回喂大模型重新生成（自愈重试, 最多 2 次）
        ↓ ⑤
查询结果 + 原问题 → 大模型组织中文回答
（时长自动换算: 13500秒 → "3小时45分"）
```

**实测效果：**
- "今天哪个教室累计在座时间最长？" → "三教201，3小时45分" ✓
- "现在有空座位吗？" → 正确取每教室**最新**记录算空位 ✓
- **"删掉 records 表"** → 安全校验拦截，数据库无法被写操作触达 ✓

**自愈重试的量化效果**：小模型生成 SQL 一次成功率约 70%，错误信息回喂重试后约 95%——工程手段弥补小模型能力。模型从 7b 降至 1.5b 后，靠提示词强化（schema 明确"rooms 表没有任何人数列" + 内嵌正确 SQL 示例）保持准确率，热响应从几十秒降至 **~1.7 秒**。

### 3.7 模块七：用户认证与权限（server.py）

- **口令安全**：PBKDF2-HMAC-SHA256（12 万迭代 + 16 字节随机盐）哈希入库，明文口令不落库
- **无状态 Token**：`base64(用户名|过期时间)` + HMAC-SHA256 签名（服务器密钥），验证不查库，7 天有效
- **首次启动自动建表 + 种子管理员**（admin/admin123，生产环境应改密）
- **权限分层**：管理接口校验 `role=admin`（普通用户 403）；自我保护——不能删除自己、不能改自己角色

---

## 四、数据库设计

库名 `study_room`，五张表，**启动时自动建库建表**（`CREATE DATABASE IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS`），旧表自动加列迁移（如 rooms 补 location 字段）。

### rooms —— 教室注册表

| 字段 | 类型 | 说明 |
|---|---|---|
| camera_id | VARCHAR(32) PK | 教室唯一标识，与 edge_agent 的 `--camera-id` 对应（如 room_201） |
| name | VARCHAR(64) | 显示名称（"三教201"） |
| total_seats | INT | 固定座位总数（空位计算的"分母"） |
| location | VARCHAR(128) | 地址（"三教 2 楼东侧"） |

### records —— 周期上报快照（时序数据）

| 字段 | 类型 | 说明 |
|---|---|---|
| ts | DATETIME | 上报时间 |
| camera_id | VARCHAR(32) | 归属教室 |
| head_count | INT | 检测人数（原始值） |
| occupied_count | INT | 占用座位数（**入库前已封顶到 total_seats**） |
| seat_status | JSON | 历史兼容字段（现固定空值） |

索引：`(camera_id, ts)` 复合索引——所有"某教室最近 N 条/24h"查询都走它。

### seat_sessions —— 在座会话

| 字段 | 类型 | 说明 |
|---|---|---|
| camera_id | VARCHAR(32) | 归属教室 |
| track_id | INT | 跟踪器分配的人 ID（一次连续在座 = 一行） |
| start_time / end_time | DATETIME | 会话起止 |
| duration_sec | INT | 时长（秒） |

### camera_alerts —— 摄像头异常告警

| 字段 | 类型 | 说明 |
|---|---|---|
| alert_type | VARCHAR(16) | blocked（遮挡）/ moved（移动）/ recovered（恢复） |
| message | VARCHAR(255) | 人话描述 |

### users —— 门户账号（自动建表 + 种子管理员 admin）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK | 用户 ID（头像文件名即 uid） |
| username | VARCHAR(32) UNIQUE | 登录名（注册时设置一次，之后不可改） |
| nickname | VARCHAR(64) | 昵称/展示名（随时可在个人中心改） |
| password_hash / password_salt | VARCHAR | PBKDF2-HMAC-SHA256（12 万迭代 + 随机盐） |
| role | VARCHAR(16) | admin / user |
| avatar | VARCHAR(255) | 头像 URL 路径（存 Docker 卷 avatar_data） |
| created_at / last_login | DATETIME | 注册/最近登录 |

旧表自动幂等迁移：启动时检测缺列（如 rooms 补 location、users 补 nickname/avatar）则 `ALTER TABLE ADD COLUMN`。

### 设计要点

- **占用数封顶**：`occupied = min(检测数, total_seats)` 在 `/api/report` 入库前执行。有人站着/瞬时误检会让检测数超座位数，封顶保证"空位 = 座位 − 占用"永远不为负
- **总览以注册表为准**：`/api/overview` 用 rooms LEFT JOIN 最新记录——新注册教室立刻上大屏（显示"未上报"），不等第一次数据
- **一人一行会话**：不存逐帧位置（数据量爆炸），只存起止时间——分析时长只需扫这张小表

---

## 五、API 接口文档

Base URL: `http://127.0.0.1:8000`，交互式文档 `http://127.0.0.1:8000/docs`（Swagger UI 可直接点击测试）。

### 数据上报

```http
POST /api/report
{
  "camera_id": "room_201",
  "head_count": 8,          # 跟踪确认人数
  "occupied_count": 8,     # min(head_count, total_seats) 服务端再封顶一次
  "sessions": [...]        # 可选: 新归档的会话批量入库
}
→ 200 {"status": "ok"}
```

### 查询类

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `GET /api/overview` | - | 全部教室聚合：每间的最新人数/占用/座位数 + 最后上报时间（summary 含汇总 KPI） |
| `GET /api/stats` | camera_id | 单教室最新快照（大屏详情页数据源） |
| `GET /api/history` | hours, camera_id | 时间序列点（24h 趋势曲线） |
| `GET /api/daily` | date, camera_id | 按小时聚合（AI 日报数据源） |
| `GET /api/trends` | days, camera_id | **历史规律**：7天×24h 占用率热力矩阵 + 黄金/高峰时段 Top3（camera_id 空为全馆） |
| `GET /api/forecast` | hours(1~12) | **座位预测**：未来逐小时各教室预估空位（12 周同星期同小时日均，样本不足回退同小时），普通用户专属 |
| `GET /api/duration` | date, camera_id | 会话聚合：会话数/人均时长/累计/最长 |

### 认证与管理（门户）

```http
POST /api/auth/register {"username":"stu01","password":"***"}   → 自动登录返回 token
POST /api/auth/login    {"username":"admin","password":"***"}  → {"token":"...","user":{...}}
GET  /api/auth/stats                                       → 登录页实时统计(无需 token)
GET  /api/auth/me       [需登录]                            → 当前用户信息
GET  /api/admin/users   [需管理员 token]                     → 用户列表
POST /api/admin/users   [需管理员 token]                     → 新增用户(可选角色)
PUT  /api/admin/users/{uid} [需管理员 token]                  → 升降管理员 / 重置密码
DELETE /api/admin/users/{uid} [需管理员 token]                → 删除用户

# 个人中心
POST /api/profile/nickname {"nickname":"..."}                → 改昵称(展示名)
POST /api/profile/password {"old_password":"...","new_password":"..."} → 改密码
POST /api/profile/avatar  (multipart/form-data, 字段 file)   → 上传头像(类型/2MB/魔数校验)
```

所有需登录接口走 `Authorization: Bearer <token>`（HMAC 签名验证）；管理接口额外校验 role=admin。

### 管理类

| 接口 | 说明 |
|---|---|
| `POST /api/rooms` | 注册教室（重复 camera_id = 更新信息） |
| `GET /api/rooms` | 教室列表 |
| `PUT /api/rooms/{camera_id}` | 修改名称/座位数/地址 |
| `DELETE /api/rooms/{camera_id}?purge=true` | 删除教室（purge=true 级联删历史数据） |

### AI 与告警

```http
POST /api/ask {"question": "今天几点人最少？"}
→ {"answer": "今天人最少的是凌晨5点，平均2人。", "sql": "SELECT ..."}

GET /api/alerts?limit=5
→ [{"camera_id":"room_201","alert_type":"blocked","message":"画面疑似被遮挡","ts":"..."}]
```

---

## 六、一次完整的数据旅程

以"三教201 有个人来自习 2 小时"为例，串起所有模块：

1. 教室电脑的 edge_agent 打开摄像头，每帧：HeadDetector 检出 8 个头框 → IOUTracker 维持 8 条轨迹
2. 一个新人入画，连续 3 帧被检出 → 轨迹确认，ID=7，人数 8→9
3. 30 秒到，edge_agent POST `/api/report`：`{camera_id: "room_201", head_count: 9}`（服务端封顶确保 ≤ 45 座）
4. MySQL 的 records 表多一行；大屏 5 秒内轮询到，卡片人数 8→9 闪橙光 + "▲ +1" 角标；趋势曲线末端光点跳到 9
5. 这人学习 2 小时后离开，跟踪器 12 帧没检到 ID=7 → 会话归档：`{track_id: 7, start: 14:00, end: 16:00}` 随下次 report 入库
6. 期间他起身接水 10 秒——12 帧老化期内回归，ID 保持 7，会话不中断
7. 晚上管理员在大屏问 AI："今天三教201人均在座多久？" → Text2SQL 查 seat_sessions → "人均 2 小时 15 分"
8. 半路有人用围巾挡了摄像头 → 亮度+纹理+人数三信号连续 8 帧 → 红色告警条弹出"镜头疑似被遮挡"；拿开后自动"恢复正常"

---

## 七、快速开始

### 7.1 方式一：Docker 一键部署（推荐）

```powershell
# 1. 准备环境变量
Copy-Item .env.example .env    # 编辑: MYSQL_PASSWORD / OLLAMA_MODEL / TOKEN_SECRET

# 2. 拉起 3 容器（api + mysql + ollama; 门户 SSR 已并入 api 容器）
docker compose up -d --build

# 3. （可选）拉取 AI 模型
docker exec studysense-ollama ollama pull qwen2.5:1.5b
```

打开：

| 页面 | 地址 | 说明 |
|---|---|---|
| **管理门户** | http://localhost:8000/portal/login | SSR 多页应用；种子账号 admin/admin123 |
| **可视化大屏** | http://localhost:8000/ui | 挂墙监控 |
| **API 文档** | http://localhost:8000/docs | Swagger |

MySQL 映射到宿主机 **3307**（避开本机 3306）；Ollama 直接挂载宿主机模型目录，已有模型免二次下载。

### 7.2 方式二：本地开发运行

环境要求：Windows + NVIDIA 显卡（DirectML 推理，无需 CUDA）、Python 3.10+（conda 环境 `face_test`）、本机 MySQL 8.0、Ollama。

```powershell
conda create -n face_test python=3.10 -y
conda activate face_test
pip install fastapi uvicorn pymysql requests jinja2 onnxruntime-directml opencv-python numpy streamlit plotly

# 终端 1: 后端（含大屏 + 管理门户 SSR; 密码走环境变量, 不落盘）
$env:MYSQL_PASSWORD="你的MySQL密码"
uvicorn server:app --port 8000

# 终端 2: 边缘采集（真实摄像头; 测试视频则 --source 换文件路径）
python edge_agent.py --source 0 --camera-id room_201 --interval 30

# 测试视频模拟（无摄像头时）
python edge_agent.py --source media/test_room.mp4 --camera-id room_201 --interval 10
```

打开：大屏 http://127.0.0.1:8000/ui/ ，门户 http://127.0.0.1:8000/portal/login 。

---

## 八、无实景测试方案

没有真实自习室时的测试方式（也是开发期的主要验证方式）：

> 旧版 Streamlit 模拟测试页（dashboard.py）已移除，直接用 **edge_agent 挂视频源** 即可完成同样的模拟——数据入库后实时驱动大屏与门户。

1. 启动后端 `uvicorn server:app --port 8000`
2. 用测试视频驱动边缘采集链路（检测框 + 跟踪 ID 实时显示，数据按间隔入库）：
   ```powershell
   python edge_agent.py --source media/test_room.mp4 --camera-id room_201 --interval 10
   ```
3. **同时开大屏**（http://127.0.0.1:8000/ui/）观察联动：卡片人数跳动、曲线滑入新点、相对时间保持绿色"刚刚"
4. 视频播完自动收尾（在场会话归档）；Ctrl+C 随时终止，即停即止

测试视频来源：任何俯拍教室/图书馆/人群视频均可（CrowdHuman 的 val 图也能用于单帧测试）。

---

## 九、项目结构

> 逐文件说明用途。深入理解每个文件的实现逻辑请看 [docs/项目详细介绍与面试要点.md](docs/项目详细介绍与面试要点.md)。

```
face_test/
├── server.py                    # 【后端中枢·单文件】全部 REST API + 启动自动建库建表/旧表迁移
│                                #   + Text2SQL 链路(安全校验/自愈重试) + 用户认证(PBKDF2/HMAC Token)
│                                #   + Jinja2 SSR 门户路由 + 安全中间件(CSP/CSRF/防爆破/CORS)
├── edge_agent.py                # 【边缘采集端】教室电脑常驻进程: 取流(摄像头/视频/图片)→检测→跟踪
│                                #   →异常守卫→每 interval 秒 POST 上报(只传数字); 退出时归档在场会话
├── head_detector.py             # 【人头检测器】letterbox 预处理 + ONNX(DirectML GPU)推理
│                                #   + 分级置信度阈值 + NMS; 默认加载 models/head_detect_1280.onnx
├── tracker.py                   # 【跟踪器】纯 NumPy IOU+质心跟踪: 轨迹确认(min_hits=3)/老化(max_age=12)/会话归档
├── camera_guard.py              # 【异常守卫】零模型判定镜头遮挡(亮度+纹理+人数三信号)与移动(帧差分)
│
├── web/                         # ── 前端静态资源 ──
│   ├── index.html               # 可视化大屏(单文件 52KB, ECharts CDN): KPI/卡片墙/座位点阵/趋势/告警
│   │                            #   开头加载 config.js 读 window.__API_BASE__ → 大屏可独立部署的关键
│   ├── logo.svg                 # 全站 Logo + favicon
│   └── portal_static/           # 管理门户公共资源(8 个门户页共用; avatars/ 头像存 Docker 卷)
│       ├── common.css           # 统一深色科技风样式(卡片/表单/弹窗/动效/粒子背景)
│       └── common.js            # $ 选择器 / api() 带凭证请求封装 / 粒子+鼠标交互(避让/连线) / toast
│
├── templates/portal/            # ── 管理门户 SSR 模板(Jinja2, FastAPI 同源渲染) ──
│   ├── base.html                # 布局母版: 左侧导航+顶栏, 按角色控制导航可见性, 引入公共资源
│   ├── login.html               # 登录/注册页: 品牌区(未登录可见实时统计) + 表单滑块切换
│   ├── overview.html            # 占用总览(所有角色): 卡片墙/搜索筛选/⭐收藏/教室详情弹窗(座位点阵+24h趋势)
│   ├── trends.html              # 历史规律(普通用户专属): 黄金/高峰时段卡片 + 7天×24h热力图 + 日均值柱
│   ├── forecast.html            # 座位预测(普通用户专属): 未来1~12h逐小时空位预估卡片墙(推荐教室⭐置顶)
│   ├── ai.html                  # AI 助手: 聊天气泡+打字动画+快捷提问芯片
│   ├── rooms.html               # 教室管理(管理员): 增删改, 删除可选级联清历史数据
│   ├── users.html               # 用户管理(管理员): 新增/升降管理员/重置密码/删除(含自我保护)
│   └── profile.html             # 个人中心: 头像上传/昵称修改/密码修改
│
├── dashboard/                   # ── 大屏独立部署容器(nginx:alpine, 宿主机 8080) ──
│   ├── Dockerfile               # 只 COPY 大屏两个文件, 镜像 ~10MB
│   └── gen-config.sh            # nginx 启动钩子: 按环境变量 API_BASE 生成 config.js(运行时配置注入)
│
├── models/                      # ── ONNX 检测模型(不进 git) ──
│   ├── head_detect_1280.onnx    # 人头检测·精度优先(部署默认, mAP50=0.842)
│   ├── head_detect_960.onnx     # 人头检测·速度折中(单帧 11.9ms)
│   └── face_detect*.onnx ×2    # 早期人脸检测模型(留档: 立项时验证俯拍人脸失效的依据)
│
├── media/                       # ── 测试素材(不进 git) ──
│   └── test_room.mp4            # 40 人真实自习室俯拍视频(全链路验证)
│
├── scripts/                     # ── 训练辅助 ──
│   └── convert_crowdhuman.py    # CrowdHuman odgt→YOLO 转换(含脏标注过滤+质量校验)
│
├── docs/                        # ── 文档 ──
│   ├── 项目详细介绍与面试要点.md          # 项目详细介绍+面试要点(深入理解实现用)
│   └── 人头检测模型训练交接文档.md        # 检测模型训练阶段交接文档
│
├── docker-compose.yml           # 编排 4 服务: api(8000,含门户SSR) / mysql(3307) / ollama / dashboard(8080)
├── Dockerfile                   # api 镜像: python-slim + FastAPI, 只 COPY server/web/templates
├── .env.example                 # 环境变量模板: MYSQL_PASSWORD/OLLAMA_MODEL/TOKEN_SECRET/CORS_ORIGINS/DASHBOARD_API_BASE
├── .gitignore                   # 忽略模型权重/视频/日志/.env/运行产物
└── README.md                    # 需求与技术文档(本文档)
```

---

## 十、核心指标与测试结果

| 指标 | 数值 | 测试条件 |
|---|---|---|
| 检测 mAP50 | **0.842** | CrowdHuman val（4370 图 / 97,285 头框），1280 推理 |
| 检测 mAP50-95 | 0.546 | 同上 |
| 单帧推理耗时 | 11.9ms @960 / 152ms @1280 | RTX 5070 Laptop, DirectML |
| 端到端数据延迟 | ≤ 5 秒 | 上报周期 30s + 大屏轮询 5s |
| AI 问答 SQL 成功率 | ~70% → ~95%（自愈重试后） | 30+ 个真实问题 |
| AI 热响应 | ~1.7 秒 | qwen2.5:1.5b（提示词强化后保持准确率） |
| 跟踪器依赖 | 纯 NumPy | 无第三方跟踪库 |
| 40 人真实自习室视频 | 39~44 人（跟踪平滑无抖动） | test_room.mp4 全链路实测 |
| 门户容器镜像 | ~150MB | 原生 JS SPA（原 Streamlit 版 ~900MB） |

---

## 十一、技术决策记录（ADR）

| # | 决策 | 备选方案 | 选择理由 |
|---|---|---|---|
| 1 | 边缘端检测 | 服务端集中检测 | 带宽/隐私/成本三胜 |
| 2 | 人数=跟踪数, 空位=座位−人数 | 头→座位坐标映射 | 俯拍映射不可靠；把不确定问题换成确定问题 |
| 3 | Text2SQL | RAG 向量检索 | 结构化时序数据 SQL 精确 |
| 4 | 训练 768 / 推理 1280 | 直接 960 训练（显存不够） | 零训练成本 +5.3 mAP |
| 5 | 自研 IOU 跟踪器 | ByteTrack | 场景静态（坐着为主），杀鸡不用牛刀 |
| 6 | 零模型异常守卫 | 训练异常分类模型 | 亮度+纹理+差分三信号已足够，省一个模型 |
| 7 | 单文件大屏 | Vue/React 工程 | 内部工具零构建维护成本最低 |
| 8 | 占用数入库封顶 | 查询时封顶 | 脏数据不落库，AI 裸查 SQL 也拿到干净数据（真实踩坑：AI 问答与统计口径不一致的 bug 由此修复） |
| 9 | 管理门户用原生 JS SPA | 继续 Streamlit | 与大屏统一深色科技风与动效；镜像 900MB→150MB；代价是放弃了 Streamlit 的表单效率 |
| 10 | AI 模型 1.5b + 提示词强化 | 7b（更准但慢） | 7b 热响应几十秒体验差；schema 明确约束 + 内嵌正确 SQL 示例后 1.5b 准确率达标，响应 1.7s |
| 11 | 空座提醒纯前端实现 | 后端 WebSocket 推送 | Notification API + 15s 轮询已够用，零后端改造成本；将来量大再升级 |
| 12 | HMAC 签名 Token | JWT 库 | 无第三方依赖、逻辑 20 行、无状态验证；内部系统不需要 JWT 生态 |
| 13 | 座位预测用统计基线（12 周同星期同小时日均） | 训练 ML 时序模型 | 自习室人流有强周周期（课表驱动），统计基线即可捕捉；数据量不足时 ML 反而过拟合；预测依据可完整向用户解释（可解释性即功能） |
