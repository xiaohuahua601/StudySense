FROM nginx:alpine

# 大屏仪表盘: 纯静态托管 web/index.html, 镜像约 30MB, 适合部署到班级大屏机器
# 启动时由 nginx 镜像的 /docker-entrypoint.d/ 机制执行 gen-config.sh,
# 按环境变量 API_BASE 生成 config.js 注入后端地址
COPY web/index.html web/logo.svg /usr/share/nginx/html/
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY dashboard/gen-config.sh /docker-entrypoint.d/20-gen-config.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/20-gen-config.sh && chmod +x /docker-entrypoint.d/20-gen-config.sh

EXPOSE 80
