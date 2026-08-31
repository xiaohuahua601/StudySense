# -*- coding: utf-8 -*-
"""
自习室座位监测 - Streamlit 多教室监控中心

运行: streamlit run dashboard.py
依赖: 后端 server.py 运行中

页面:
    总览页    /           多教室卡片墙 + 全局汇总
    教室详情  ?cam=xxx     单教室看板（座位网格 + 24h 曲线 + AI 日报）
"""

import os

import plotly.graph_objects as go
import requests
import streamlit as st

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
REFRESH_SECONDS = 5
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")

st.set_page_config(page_title="自习室监控中心", page_icon="🏫", layout="wide")


# ---------- API 调用 ----------

def _get(path, params=None):
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=5)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_overview():
    return _get("/api/overview")


@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_stats(camera_id):
    return _get("/api/stats", {"camera_id": camera_id})


@st.cache_data(ttl=30)
def fetch_history(camera_id, hours=24):
    data = _get("/api/history", {"hours": hours, "camera_id": camera_id})
    return data.get("points", []) if data else []


@st.cache_data(ttl=300)
def fetch_daily(camera_id, day=None):
    params = {"camera_id": camera_id}
    if day:
        params["day"] = day
    data = _get("/api/daily", params)
    return data or {"day": "", "hours": []}


@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_rooms():
    return _get("/api/rooms") or []


def _api(method, path, json_body=None, params=None):
    try:
        r = requests.request(method, f"{API_BASE}{path}",
                              json=json_body, params=params, timeout=5)
        if r.status_code < 300:
            return True, r.json()
        return False, r.json().get("detail", r.text)
    except requests.RequestException as e:
        return False, str(e)


# ---------- 占用率颜色 ----------

def rate_color(rate):
    if rate >= 0.7:
        return "🔴"      # 紧张
    if rate >= 0.3:
        return "🟡"      # 一般
    return "🟢"          # 宽松


# ========== 教室管理页 ==========

def render_manage():
    if st.button("⬅ 返回总览"):
        st.query_params.clear()
        st.rerun()
    st.title("⚙️ 教室管理")

    rooms = fetch_rooms()
    tab_add, tab_edit, tab_del = st.tabs(["➕ 添加教室", "✏️ 修改信息", "🗑️ 删除教室"])

    # ---- 添加 ----
    with tab_add:
        with st.form("add_room", border=True):
            st.subheader("注册新教室")
            cid = st.text_input("教室标识 (camera_id)", placeholder="如 room_204, 唯一, 创建后不可改",
                                help="边缘采集端 edge_agent.py --camera-id 需与此一致")
            name = st.text_input("教室名称", placeholder="如 三教204")
            seats = st.number_input("座位总数", min_value=0, value=40, step=1)
            location = st.text_input("教室地址", placeholder="如 三教2楼东侧")
            if st.form_submit_button("注册", type="primary", use_container_width=True):
                if not cid or not name:
                    st.error("教室标识和名称不能为空")
                else:
                    ok, resp = _api("POST", "/api/rooms", json_body={
                        "camera_id": cid.strip(), "name": name.strip(),
                        "total_seats": seats, "location": location.strip()})
                    if ok:
                        st.success(f"教室 {name} 已注册（{cid}）")
                        st.cache_data.clear()
                    else:
                        st.error(f"注册失败: {resp}")

    # ---- 修改 ----
    with tab_edit:
        if not rooms:
            st.info("暂无教室")
        else:
            options = {f"{r['name']}（{r['camera_id']}）": r for r in rooms}
            sel = st.selectbox("选择教室", list(options.keys()))
            room = options[sel]
            with st.form("edit_room", border=True):
                st.subheader(f"修改: {room['name']}")
                st.caption(f"教室标识 `{room['camera_id']}` 不可修改（关联键）")
                new_name = st.text_input("教室名称", value=room["name"])
                new_seats = st.number_input("座位总数", min_value=0,
                                            value=room["total_seats"], step=1)
                new_loc = st.text_input("教室地址", value=room.get("location", ""))
                if st.form_submit_button("保存修改", type="primary", use_container_width=True):
                    if not new_name:
                        st.error("教室名称不能为空")
                    else:
                        ok, resp = _api("PUT", f"/api/rooms/{room['camera_id']}",
                                        json_body={"camera_id": room["camera_id"],
                                                   "name": new_name.strip(),
                                                   "total_seats": new_seats,
                                                   "location": new_loc.strip()})
                        if ok:
                            st.success("修改已保存")
                            st.cache_data.clear()
                        else:
                            st.error(f"修改失败: {resp}")

    # ---- 删除 ----
    with tab_del:
        if not rooms:
            st.info("暂无教室")
        else:
            options = {f"{r['name']}（{r['camera_id']}）": r for r in rooms}
            sel = st.selectbox("选择要删除的教室", list(options.keys()))
            room = options[sel]
            purge = st.checkbox("同时删除该教室全部历史数据（记录/会话/告警）",
                                help="不勾选则保留历史数据, 仅解除注册")
            if st.button(f"确认删除: {room['name']}", type="primary",
                         use_container_width=True):
                ok, resp = _api("DELETE", f"/api/rooms/{room['camera_id']}",
                                params={"purge": purge})
                if ok:
                    st.success(f"教室 {room['name']} 已删除")
                    st.cache_data.clear()
                else:
                    st.error(f"删除失败: {resp}")

    # 教室一览
    st.divider()
    st.subheader("已注册教室")
    if rooms:
        for r in rooms:
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 2, 2])
                c1.markdown(f"**{r['name']}**  `{r['camera_id']}`")
                c2.write(f"座位: {r['total_seats']}")
                c3.write(f"地址: {r.get('location') or '—'}")


# ========== 模拟测试页 ==========

SIM_FRAME = "sim_{cam}.jpg"
SIM_UPLOAD = "sim_upload.mp4"


def render_simulate():
    if st.button("⬅ 返回总览"):
        st.query_params.clear()
        st.rerun()
    st.title("🎬 模拟测试")
    st.caption("用视频或摄像头模拟某间教室的实时人流, 验证检测/跟踪/上报/大屏全链路")

    rooms = fetch_rooms()
    if not rooms:
        st.warning("请先在「教室管理」注册教室")
        return
    room_opts = {f"{r['name']}（{r['camera_id']}）": r for r in rooms}
    room = room_opts[st.selectbox("绑定教室", list(room_opts.keys()))]
    cam = room["camera_id"]

    tab_up, tab_path, tab_cam = st.tabs(["📤 上传视频", "📁 本地视频路径", "📷 本机摄像头"])
    with tab_up:
        up = st.file_uploader("选择视频文件", type=["mp4", "avi", "mov", "mkv"])
        if up is not None:
            with open(SIM_UPLOAD, "wb") as f:
                f.write(up.getbuffer())
            st.success(f"已保存: {SIM_UPLOAD}（{up.size/1024/1024:.1f} MB）")
        src_up = SIM_UPLOAD if up is not None else None
    with tab_path:
        src_path = st.text_input("视频完整路径", value="test_room.mp4",
                                 placeholder="如 D:\\videos\\room.mp4")
        src_path = src_path if src_path and os.path.exists(src_path) else None
        if src_path:
            st.success(f"文件存在: {src_path}")
    with tab_cam:
        st.info("使用本机摄像头（默认 0 号设备）实时模拟")
        src_cam = "0" if st.toggle("启用摄像头（优先于视频路径）", value=False) else None

    # 优先级: 摄像头开关 > 上传的视频 > 本地路径（路径栏有默认值, 不加开关会永远轮不到摄像头）
    src = src_cam or src_up or src_path
    interval = st.slider("上报间隔（秒）", 5, 60, 10, 5,
                         help="每隔多久向服务器上报一次人数")

    proc = st.session_state.get("sim_proc")

    # ---- 停止 ----
    if proc is not None and st.button("⏹ 停止模拟", type="primary"):
        proc.terminate()
        st.session_state.pop("sim_proc")
        st.rerun()

    # ---- 启动 ----
    if proc is None:
        if src is None:
            st.info("请先提供视频源（上传 / 路径 / 摄像头）")
            return
        if st.button("▶ 开始模拟", type="primary"):
            with st.spinner("启动边缘采集进程..."):
                import subprocess
                import sys
                # 直接用当前解释器启动, 不经 conda run 中转:
                # 否则 terminate() 只杀掉 conda 包装进程, edge_agent 子进程会变僵尸继续上报
                cmd = [sys.executable, "edge_agent.py",
                       "--source", src, "--camera-id", cam,
                       "--interval", str(interval), "--no-show"]
                st.session_state.sim_proc = subprocess.Popen(
                    cmd, cwd=os.path.dirname(os.path.abspath(__file__)))
            st.rerun()
        return

    # ---- 运行中 ----
    if proc.poll() is not None:
        st.success(f"模拟已结束（退出码 {proc.returncode}）, 历史数据已入库")
        st.session_state.pop("sim_proc")
        st.button("重新开始模拟")
        return

    st.success(f"🟢 模拟运行中 → 教室: {room['name']} | 源: {src}")

    # 最新帧 + 实时数据
    col1, col2 = st.columns([3, 1])
    with col1:
        frame_file = SIM_FRAME.format(cam=cam)
        if os.path.exists(frame_file):
            st.image(frame_file, caption="边缘端实时画面（每秒刷新）")
        else:
            st.info("等待第一帧（模型加载约 3~5 秒）...")
    with col2:
        st.metric("绑定教室", room["name"])
        stats = _get("/api/stats", {"camera_id": cam})
        if stats:
            st.metric("当前人数", stats["head_count"])
            st.metric("空座位",
                      max(stats["total_seats"] - stats["occupied_count"], 0))
        st.caption("同步打开大屏 http://127.0.0.1:8000/ui/ 观察联动效果")

    # 进程存活的周期刷新
    import time as _t
    _t.sleep(3)
    st.rerun()


# ========== 总览页（默认） ==========

def render_overview():
    st.title("🏫 自习室监控中心")

    # 顶部导航
    c1, c2 = st.columns(2)
    if c1.button("⚙️ 教室管理"):
        st.query_params["page"] = "manage"
        st.rerun()
    if c2.button("🎬 模拟测试"):
        st.query_params["page"] = "simulate"
        st.rerun()

    # 实时区块: 每 5 秒局部自动刷新（不重载整页, 按钮/输入不闪烁）
    @st.fragment(run_every=REFRESH_SECONDS)
    def live_overview():
        data = fetch_overview()
        if data is None:
            st.error(f"后端不可达：{API_BASE}。请先启动 server.py")
            return

        summary = data["summary"]
        rooms = data["rooms"]

        # 全局指标
        st.subheader("全局汇总")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("监控教室", f"{summary['room_count']} 间")
        k2.metric("当前总人数", f"{summary['total_heads']} 人")
        k3.metric("占用座位", f"{summary['total_occupied']} / {summary['total_seats']}")
        k4.metric("总占用率", f"{summary['occupancy_rate'] * 100:.1f}%")

        # 教室卡片墙
        st.subheader("各教室实时状态")
        if not rooms:
            st.info("暂无教室数据。请先在「教室管理」注册教室。")
            return

        cols_per_row = 4
        for row_start in range(0, len(rooms), cols_per_row):
            cols = st.columns(cols_per_row)
            for i, room in enumerate(rooms[row_start:row_start + cols_per_row]):
                with cols[i]:
                    rate = room["occupancy_rate"]
                    with st.container(border=True):
                        st.markdown(f"#### {room['name']}")
                        st.markdown(f"`{room['camera_id']}`")
                        st.metric("占用", f"{room['occupied_count']} / {room['total_seats']}")
                        st.progress(rate if rate <= 1.0 else 1.0,
                                    text=f"{rate * 100:.0f}% {rate_color(rate)}")
                        upd = room["last_update"] or "未上报"
                        st.caption(f"人数 {room['head_count']} | 更新 {upd}")
                        if st.button("查看详情", key=f"btn_{room['camera_id']}",
                                     use_container_width=True):
                            st.query_params["cam"] = room["camera_id"]
                            st.rerun(scope="app")

        st.caption(f"每 {REFRESH_SECONDS} 秒自动刷新 | 数据源: {API_BASE}")

    live_overview()


# ========== 教室详情页 ==========

def render_detail(camera_id):
    # 顶部返回
    if st.button("⬅ 返回总览"):
        st.query_params.clear()
        st.rerun()

    stats = fetch_stats(camera_id)
    if stats is None:
        st.error(f"教室 {camera_id} 暂无数据")
        return

    st.title(f"🪑 {camera_id} 教室详情")

    # 实时区块: 每 5 秒局部自动刷新
    @st.fragment(run_every=REFRESH_SECONDS)
    def live_stats():
        stats = fetch_stats(camera_id)
        if stats is None:
            st.warning("该教室暂无上报数据")
            return
        free = max(stats["total_seats"] - stats["occupied_count"], 0)
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("当前人数", f"{stats['head_count']} 人")
        col2.metric("占用座位", f"{stats['occupied_count']} / {stats['total_seats']}")
        col3.metric("空余座位", f"{free} 个")
        col4.metric("占用率", f"{stats['occupancy_rate'] * 100:.1f}%")
        st.caption(f"最后更新: {stats['last_update']} | 每 {REFRESH_SECONDS} 秒自动刷新")

        # 历史曲线
        st.subheader("24 小时趋势")
        points = fetch_history(camera_id, 24)
        if points:
            ts = [p["ts"] for p in points]
            heads = [p["head_count"] for p in points]
            occ = [p["occupied_count"] for p in points]

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=ts, y=heads, name="检测人数", mode="lines",
                                     line=dict(color="#1f77b4")))
            fig.add_trace(go.Scatter(x=ts, y=occ, name="占用座位", mode="lines",
                                     line=dict(color="#2ca02c")))
            fig.update_layout(
                xaxis_title="时间", yaxis_title="数量",
                height=350, margin=dict(l=20, r=20, t=30, b=20),
                legend=dict(orientation="h", y=1.1),
            )
            st.plotly_chart(fig, use_container_width=True)

    live_stats()

    # AI 日报
    st.subheader("AI 日报")

    def gen_daily_report():
        daily = fetch_daily(camera_id)
        if not daily["hours"]:
            return "今天还没有数据。"
        lines = [f"日期: {daily['day']}", "每小时统计(小时, 平均人数, 平均占用, 峰值人数, 采样数):"]
        for h in daily["hours"]:
            lines.append(f"{int(h['h'])}时: {h['avg_heads']:.1f}, {h['avg_occ']:.1f}, "
                          f"{int(h['max_heads'])}, {int(h['samples'])}")
        prompt = (
            "你是自习室管理员助手。根据以下某教室座位监测数据，用中文写一份简短日报（150字内），"
            "包含：人流量概况、高峰时段、空闲时段、运营建议。数据：\n" + "\n".join(lines)
        )
        try:
            r = requests.post(f"{OLLAMA_URL}/api/generate",
                              json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                              timeout=60)
            if r.status_code == 200:
                return r.json().get("response", "").strip()
            return f"Ollama 返回错误 {r.status_code}"
        except requests.RequestException as e:
            return f"Ollama 服务不可达（{e}）。请确认: ollama serve"

    if st.button("生成今日日报", type="primary"):
        with st.spinner("AI 分析中..."):
            st.markdown(gen_daily_report())
    else:
        st.caption(f"点击按钮调用 Ollama（{OLLAMA_MODEL}）基于当日数据生成日报")

    # 数据问答助手（Text2SQL）
    st.subheader("💬 数据问答助手")
    st.caption("用自然语言问询数据库, 如: 今天几点人最少？哪个教室今天累计在座时间最长？")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    def ask_question(q):
        with st.spinner("AI 查询中..."):
            try:
                r = requests.post(f"{API_BASE}/api/ask",
                                  json={"question": q, "camera_id": camera_id},
                                  timeout=120)
                if r.status_code == 200:
                    d = r.json()
                    st.session_state.chat_history.append(
                        (q, d["answer"], d["sql"]))
                else:
                    detail = r.json().get("detail", r.text)
                    st.session_state.chat_history.append((q, f"查询失败: {detail}", None))
            except requests.RequestException as e:
                st.session_state.chat_history.append((q, f"请求失败: {e}", None))

    # 展示历史对话
    for q, a, sql in st.session_state.chat_history[-8:]:
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            st.markdown(a)
            if sql and st.toggle("查看SQL", key=f"sql_{hash(sql)}{hash(q)}"):
                st.code(sql, language="sql")

    if q := st.chat_input("例如: 今天几点人最少？"):
        ask_question(q)
        st.rerun()

    st.caption(f"每 {REFRESH_SECONDS} 秒自动刷新 | 数据源: {API_BASE}")


# ---------- 路由 ----------
cam = st.query_params.get("cam")
page = st.query_params.get("page")

if page == "manage":
    render_manage()
elif page == "simulate":
    render_simulate()
elif cam:
    render_detail(cam)
else:
    render_overview()
