FROM python:3.10-slim

WORKDIR /app

# 先装依赖（利用构建缓存层）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    fastapi uvicorn pymysql requests

# 拷贝代码
COPY server.py .
COPY web/ web/

EXPOSE 8000

# 健康检查: API 存活则容器健康
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
