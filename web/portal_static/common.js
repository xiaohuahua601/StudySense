/* ================= StudySense 门户公共脚本（SSR 版）
 * 会话由服务端 Cookie 维护(HttpOnly, JS 不可读), 页面权限由服务端渲染时判定
 * 所有 fetch 同源 + 自动带 Cookie; 写操作统一带 X-Requested-With 头(服务端 CSRF 校验)
 * ================= */
const $ = s => document.querySelector(s);
const favs = new Set(JSON.parse(localStorage.getItem("st_favs") || "[]"));

async function api(path, opts = {}) {
  opts.headers = Object.assign({
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",   // CSRF 校验头: 跨站表单无法携带
  }, opts.headers || {});
  opts.credentials = "same-origin";          // 同源自动带 HttpOnly 会话 Cookie
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/portal/login"; throw new Error("登录已过期"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

/* 数字滚动 */
function tween(el, to, fmt = v => v) {
  to = Number(to) || 0;   // 防止 undefined/NaN 传入
  const from = parseFloat(el.dataset.val) || 0;
  el.dataset.val = to;
  if (from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = 700;
  (function step(t) {
    const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(Math.round(from + (to - from) * e));
    if (p < 1) requestAnimationFrame(step);
  })(t0);
}

/* 涟漪 + 聚光灯 + 3D 倾斜（全局事件委托） */
addEventListener("click", e => {
  const t = e.target.closest(".btn, .rcard, .kpi, .ntab, .chip");
  if (!t) return;
  const r = t.getBoundingClientRect();
  const d = Math.max(r.width, r.height) * .9;
  const s = document.createElement("span");
  s.className = "ripple";
  s.style.cssText = `width:${d}px;height:${d}px;left:${e.clientX - r.left - d / 2}px;top:${e.clientY - r.top - d / 2}px`;
  t.appendChild(s);
  s.addEventListener("animationend", () => s.remove());
});
let tiltT = null;
addEventListener("mousemove", e => {
  mouse.x = e.clientX; mouse.y = e.clientY;
  document.querySelectorAll(".kpi, .panel").forEach(el => {
    if (el.matches(":hover")) {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", (e.clientX - r.left) + "px");
      el.style.setProperty("--my", (e.clientY - r.top) + "px");
    }
  });
  const card = e.target.closest(".rcard");
  if (tiltT && tiltT !== card && !card) { tiltT.classList.remove("tilting"); tiltT.style.transform = ""; tiltT = null; }
  if (card) {
    tiltT = card;
    const r = card.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width - .5, py = (e.clientY - r.top) / r.height - .5;
    card.classList.add("tilting");
    card.style.transform = `perspective(700px) rotateY(${px * 9}deg) rotateX(${-py * 7}deg) translateY(-3px)`;
  }
});

/* ================= 粒子背景（鼠标响应增强版） ================= */
const mouse = { x: -1e4, y: -1e4 };
(function () {
  const cv = $("#bg"); if (!cv) return;
  const ctx = cv.getContext("2d");
  let W, H, pts = [];
  function resize() {
    W = cv.width = innerWidth; H = cv.height = innerHeight;
    pts = Array.from({ length: Math.min(120, Math.floor(W * H / 18000)) }, () => ({
      x: Math.random() * W, y: Math.random() * H,
      vx: (Math.random() - .5) * .35, vy: (Math.random() - .5) * .35,
      r: Math.random() * 1.8 + .8,
    }));
  }
  resize(); addEventListener("resize", resize);
  (function loop() {
    ctx.clearRect(0, 0, W, H);
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > W) p.vx *= -1;
      if (p.y < 0 || p.y > H) p.vy *= -1;
      const dx = p.x - mouse.x, dy = p.y - mouse.y, d2 = dx * dx + dy * dy;
      if (d2 < 28900) { const d = Math.sqrt(d2) || 1, f = (170 - d) / 170 * 3.4; p.x += dx / d * f; p.y += dy / d * f; }
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 7);
      ctx.fillStyle = "rgba(64,196,255,.55)"; ctx.fill();
    }
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const a = pts[i], b = pts[j];
        const dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy;
        if (d2 < 12000) {
          ctx.strokeStyle = `rgba(0,180,255,${.1 * (1 - d2 / 12000)})`;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
      const a = pts[i], dx = a.x - mouse.x, dy = a.y - mouse.y, d2 = dx * dx + dy * dy;
      if (d2 < 36100) {
        ctx.strokeStyle = `rgba(0,212,255,${.32 * (1 - d2 / 36100)})`;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(mouse.x, mouse.y); ctx.stroke();
      }
    }
    requestAnimationFrame(loop);
  })();
})();

/* ================= 退出登录（服务端删 Cookie） ================= */
const _btnLogout = document.getElementById("btnLogout");
if (_btnLogout) _btnLogout.addEventListener("click", async () => {
  try { await fetch("/portal/logout", { method: "POST", credentials: "same-origin" }); } catch { }
  location.href = "/portal/login";
});

/* ================= 弹窗控制 ================= */
function openModal(html) {
  $("#modalBody").innerHTML = html;
  $("#modalMask").classList.add("on");
}
function closeModal() { $("#modalMask").classList.remove("on"); }
if ($("#modalMask")) {
  $("#modalMask").addEventListener("click", e => { if (e.target === e.currentTarget) closeModal(); });
  addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
}

/* ================= 空座提醒（浏览器通知, 收藏教室空闲变化） ================= */
let seatWatch = { prev: {}, cooldown: {} };

function notifyPush(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  new Notification(title, { body, icon: "/ui/logo.svg", tag: "seat-" + Date.now(), silent: false });
}

(function startSeatWatcher() {
  setInterval(async () => {
    if (!favs.size) return;
    try {
      const d = await api("/api/overview");
      for (const r of d.rooms) {
        if (!favs.has(r.camera_id)) continue;
        const free = r.total_seats - r.occupied_count;
        const prev = seatWatch.prev[r.camera_id];
        // 状态翻转: 空闲不足 → 空座充足(≥3), 且 5 分钟冷却内不重复提醒
        if (prev !== undefined && prev < 3 && free >= 3 &&
            Date.now() - (seatWatch.cooldown[r.camera_id] || 0) > 5 * 60 * 1000) {
          seatWatch.cooldown[r.camera_id] = Date.now();
          notifyPush("🪑 空座提醒", `${r.name} 空出 ${free} 个座位（共 ${r.total_seats} 座, 占用率 ${(r.occupancy_rate * 100).toFixed(0)}%）`);
        }
        seatWatch.prev[r.camera_id] = free;
      }
    } catch { /* 网络异常忽略, 下轮重试 */ }
  }, 15000);   // 15 秒检查一次
})();
