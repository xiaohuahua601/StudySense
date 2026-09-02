# StudySense 智能自习室监测系统 —— 项目交接文档

> 更新时间：2026-09-01
> 用途：新会话/新人接手项目。包含完整背景、当前架构、已完成工作、运行手册、已知问题与后续方向。
> 配套阅读：README.md（需求与技术文档）、PROJECT_INTRO.md（详细介绍 + 面试要点）

***

## 一、项目概述

**一句话介绍（面试用）：**

> 基于自训 YOLOv8s 人头检测模型的多人自习室占用监测系统——俯拍视角下人脸不可见，用头部检测替代人脸检测；边缘端计数只传数字不传画面；FastAPI + MySQL + ECharts 大屏 + Jinja2 SSR 管理门户 + 本地 Ollama Text2SQL 问答，Docker 三容器一键部署。

**核心链路：** 摄像头画面 → 边缘端 AI 数人头 → 30 秒 HTTP 上报（只传数字）→ MySQL 时序存库 → 大屏/门户可视化 + 自然语言问数

**仓库：** https://github.com/xiaohuahua601/StudySense（main 分支，最新提交 `36d8137`）

***

## 二、用户环境信息

| 项目 | 值 |
| --- | --- |
| 系统 | Windows 11 |
| GPU | RTX 5070 Laptop（8GB 显存）|
| 内存 | 32GB |
| Python | conda 环境 `face_test`（路径 `D:\Software\anaconda3\envs\face_test\`，已装 onnxruntime-directml + opencv + numpy + fastapi + jinja2）|
| Docker | Docker Desktop（Linux 引擎，装在 `D:\Software\Docker\`）|
| 项目目录 | `D:\self_project\face_test\` |
| 用户偏好 | 中文交流；每步操作先征得同意再执行；先本地 IDE 测试再容器化；偏好依赖全打包的容器化部署 |

***

## 三、系统架构（当前，3 容器 + 边缘端）

```
┌─ 边缘端（每间教室一台 Windows 电脑, conda face_test 环境）─────────────────┐
│  edge_agent.py: 摄像头/视频 → head_detector(ONNX+DirectML) → tracker(IOU) │
│  → camera_guard(遮挡/移动告警) → 每 30 秒 POST /api/report（只传数字）      │
└──────────────────────────────────────────────────────────────────────────────┘
┌─ studysense-api (8000, FastAPI 单容器承载三个入口) ────────────────────────┐
│  ① REST API /api/*（数据上报/查询/Text2SQL/认证）                          │
│  ② 可视化大屏 /ui/（web/index.html, 单文件 ECharts）                       │
│  ③ 管理门户 /portal/*（Jinja2 SSR 6 页 + web/portal_static 静态资源）        │
└──────────────┬──────────────────────────────┬───────────────────────────────┘
               ▼                              ▼
   studysense-db (MySQL 8.0, 宿主机 3307)   studysense-ollama (11434, GPU)
```

**访问地址（容器部署后）：**

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| 管理门户 | http://localhost:8000/portal/login | SSR 多页；种子账号 admin/admin123 |
| 可视化大屏 | http://localhost:8000/ui | 挂墙监控，无需登录 |
| API 文档 | http://localhost:8000/docs | Swagger |

**docker-compose 服务：** `api`（含门户 SSR 与大屏）+ `db`（数据卷 db_data，宿主机 3307 避开本机 MySQL）+ `ollama`（挂载宿主机 `~/.ollama`，已有模型免二次下载；`gpus: all`）。

***

## 四、已完成工作（按模块）

### 1. 人头检测模型（已收官）

- 数据：CrowdHuman（odgt → YOLO 单类 head 转换，`convert_crowdhuman.py`）
- 训练：yolov8s + SGD + lr0=0.01 + cos_lr + imgsz=960，**mAP50 = 0.842**
- 导出：`head_detect_1280.onnx`（精度优先）/ `head_detect_960.onnx`（速度折中），DirectML GPU 推理
- 关键技巧：**分级置信度阈值**——小框（高<80px）0.15 / 大框 0.35，解决远处小头漏检与大框误检的矛盾；letterbox 预处理 + 向量化 NMS
- 历史教训：AdamW + lr0=0.01 会 loss 振荡不收敛，SGD 才收敛

### 2. 边缘采集端（edge_agent.py）

- 取流（摄像头索引/视频文件/Streamlit 模拟页控制）→ 检测 → IOU+质心跟踪（轨迹确认/老化）→ 周期上报
- `camera_guard.py`：画面遮挡/移动检测，异常 POST /api/alerts
- 会话归档：一个人从出现到离开记一条 seat_sessions（在座时长统计的数据源）

### 3. 后端（server.py，~1100 行）

- 多教室 API：`/api/rooms`（CRUD）、`/api/overview`（卡片墙聚合）、`/api/report`（占用数封顶到教室座位数）、`/api/stats` `/api/seats` `/api/history` `/api/daily` `/api/trends`（7天×24h 热力图+黄金时段）、`/api/duration`、`/api/alerts`
- 启动时自动建库建表（rooms/records/seat_sessions/camera_alerts/users）+ 种子管理员
- **Text2SQL 问答** `/api/ask`：问题 → Ollama 生成 SQL → `_safe_sql` 白名单校验（仅单条只读 SELECT，禁 DML/DDL）→ 执行（限 50 行，报错回喂 LLM 重生成最多 3 次）→ LLM 组织中文答案。schema 说明内置于 `_SCHEMA_DESC`
- 认证：PBKDF2-SHA256（12 万轮）+ HMAC 签名 token（7 天）；双通道（Bearer 头 / HttpOnly Cookie）

### 4. 可视化大屏（web/index.html）

KPI 卡片墙（数值变化闪烁/相对时间每秒跳动）+ 24h 流式趋势曲线 + 占用排行赛跑动画 + 分时热度图 + 座位点阵 + 告警条 + 粒子背景与鼠标交互。5 秒轮询。

### 5. 管理门户（templates/portal/，**最新改造**）

2026-09-01 由原生 JS 单页（portal.html）迁移为 **Jinja2 SSR 多页应用**，同日已提交推送（`36d8137`）：

- 6 个页面：login / overview（KPI+卡片墙+搜索筛选收藏+详情弹窗）/ trends（热力图+黄金时段+7日柱状）/ ai（聊天气泡+快捷提问）/ rooms、users（管理员专属，服务端权限判定）
- 公共资源：`web/portal_static/common.css`（全部样式）+ `common.js`（api 封装/数字滚动/粒子背景/弹窗/空座提醒）
- 视觉风格与大屏完全一致（深色科技风、动效保留）
- 部署精简：退役 8501 dashboard 容器、serve_portal.py、Dockerfile.dashboard；Dockerfile 增加 templates/ 与 jinja2

**门户安全设计（面试亮点）：**

| 防护 | 实现 |
| --- | --- |
| 会话存储 | HttpOnly + SameSite=Strict Cookie（JS 不可读，XSS 偷不走），替代 localStorage |
| CSRF | 写操作强制 `X-Requested-With: XMLHttpRequest` 头（跨站表单无法伪造）|
| 安全响应头 | CSP（仅 self + ECharts CDN）/ X-Frame-Options: DENY / nosniff / Referrer-Policy；会话页 no-store |
| 防爆破 | 同一用户名连错 5 次锁 15 分钟（内存态）|
| 页面守卫 | 未登录 302 登录页；普通用户访问管理页 302 总览+denied 提示 |
| 兼容 | 编程调用仍走 Authorization: Bearer（大屏/脚本不受影响）|

### 6. 历史规律与空座提醒（用户端功能）

- /api/trends：7 天 × 24 小时平均占用率热力矩阵 + 按星期×小时聚合的黄金/高峰时段 Top3
- 空座提醒：浏览器 Notification，收藏教室空座从 <3 翻转 ≥3 时通知（15 秒检查，5 分钟冷却）

***

## 五、关键架构决策（为什么这样设计）

1. **检测在边缘端做**：只回传数字（几十字节/30 秒），带宽/隐私（画面不出本机）/成本（服务器免 GPU）三赢
2. **不做头到座位一对一映射**：俯拍映射不可靠（遮挡/偏移/抖动），改为"人数 = 占用；空位 = 注册座位数 − 人数"，把不确定问题换成确定问题
3. **Text2SQL 而非向量 RAG**：数据是结构化时序数字，SQL 精确（"15 点平均 29.2 人"），语义检索只能给模糊结果
4. **管理门户三代演进**：Streamlit（无法统一视觉、镜像 900MB）→ 原生 JS SPA（风格统一但 token 存 localStorage 有 XSS 风险、权限判定在浏览器）→ Jinja2 SSR（服务端权限判定 + Cookie 会话 + CSRF，体验不打折还省一个容器）

***

## 六、运行手册

### Docker 部署（主用方式）

```powershell
cd D:\self_project\face_test
# 前提: Docker Desktop 已启动（见"环境坑"第 1 条）
docker compose up -d --build    # 3 容器: api + db + ollama
docker compose logs -f api     # 看后端日志
```

`.env` 必填：`MYSQL_PASSWORD`；可调：`OLLAMA_MODEL`（当前 `qwen2.5:1.5b`）、`TOKEN_SECRET`。

### 本地开发运行（调试 server.py 用）

```powershell
# MySQL 用 Docker 里那个（宿主机 3307）; uvicorn 不在 PATH, 用 conda 环境的 python -m
$env:MYSQL_PORT='3307'; $env:MYSQL_PASSWORD='0112@'; $env:MYSQL_HOST='127.0.0.1'
$env:OLLAMA_MODEL='qwen2.5:1.5b'; $env:TOKEN_SECRET='local-dev-secret'
D:\Software\anaconda3\envs\face_test\python.exe -m uvicorn server:app --port 8000
```

注意：本地起 8000 前先 `docker stop studysense-api`，否则端口冲突。

### 无实景测试动线

`streamlit run dashboard.py`（本地开发工具，非部署件）→ 🎬 模拟测试 → 选教室/上传视频或本机摄像头 → 开始模拟 → 数据入库实时驱动大屏与门户。也可直接 `python edge_agent.py --source test_room.mp4 --camera-id room_201`。

***

## 七、环境坑与新会话注意事项

1. **Docker Desktop 不会开机自启**：MySQL 连不上（WinError 10061）九成是这个原因。先 `"D:\Software\Docker\Docker Desktop.exe"` 启动再 `docker start studysense-db`。docker.exe 在 `D:\Software\Docker\resources\bin\`
2. **Starlette 新版 API**：`templates.TemplateResponse(request, "xxx.html", {...})`——request 是第一个位置参数；旧写法 `TemplateResponse("xxx.html", {"request": ...})` 会报 `TypeError: unhashable type: 'dict'`
3. **PowerShell 无 heredoc**：git commit 多行消息用多个 `-m` 参数拼接
4. **时区**：容器统一 TZ=Asia/Shanghai，MySQL 加 `--default-time-zone=+08:00`；Docker Desktop 里 `docker ps` 显示 UTC 属正常
5. **模型不进 git**：*.onnx/*.pt/*.mp4 在 .gitignore；数据库密码/密钥走 .env（.env 不进 git）
6. **edge_agent 独立于容器**：跑在宿主机 conda 环境，需要 DirectML GPU；它上报的目标地址决定用容器 api 还是本地 uvicorn

***

## 八、已知问题与后续可选方向（未做）

按优先级排序，均为"当前能用、但有提升空间"：

1. **HTTP 明文**：全链路无 TLS，适合内网/演示；公网部署需加反向代理 + HTTPS
2. **TOKEN_SECRET 有默认值**：docker-compose 里 `studysense-default-secret-change-me`，公网部署务必在 .env 换强随机值
3. **无状态 token 的注销盲区**：HMAC token 改密码/删用户后旧 token 在过期前仍有效（7 天）；如需即时失效要引入服务端会话表或 token 版本号
4. **AI 问答无限速**：/api/ask 每次调用两轮 LLM，可被刷爆；可按用户加频控
5. **ECharts 走公网 CDN**：离线环境大屏/门户图表会挂，可把 echarts.min.js 本地化到 portal_static 并放宽 CSP
6. **登录限速是内存态**：多副本/重启清零；单容器部署无影响
7. **无自动化测试**：验证全靠手动动线；可优先给 `_safe_sql`、`_parse_token`、`_trends` 聚合补单测
8. 移动端适配（门户导航/KPI 卡在小屏未专门优化）

***

## 九、新会话第一步行动建议

1. `git log --oneline -6` 确认在 `36d8137`（SSR 改造提交）或之后
2. `docker ps` 看三容器是否健康；Docker 没起就先启动 Docker Desktop（见第七节第 1 条）
3. 打开 http://localhost:8000/portal/login 用 admin/admin123 冒烟：登录 → 总览 KPI 有数 → 历史规律热力图渲染 → AI 问答返回 → 管理页表格正常
4. 若要继续开发：改 server.py 后本地起 uvicorn 验证（记得先停 api 容器），通过后再 `docker compose up -d --build api` 重建
5. 每步操作前征求用户同意（用户的硬性要求）
