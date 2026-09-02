#!/bin/sh
# 由 nginx 镜像启动时自动执行(/docker-entrypoint.d/ 机制)
# 按环境变量 API_BASE 生成前端配置 config.js:
#   - API_BASE 为空  => 大屏与 API 同源(不跨域)
#   - API_BASE 有值  => 跨域指向独立部署的后端, 如 http://192.168.1.50:8000
# 尾部斜杠会被去掉, 避免 //api/ 双斜杠
set -e

TARGET=/usr/share/nginx/html/config.js

if [ -z "$API_BASE" ]; then
  echo 'window.__API_BASE__ = "";' > "$TARGET"
else
  echo "window.__API_BASE__ = \"$(echo "$API_BASE" | sed 's:/*$::')\";" > "$TARGET"
fi

echo "[gen-config] config.js => $(cat "$TARGET")"
