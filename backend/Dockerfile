# 后端 Dockerfile
FROM python:3.13-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY app ./app

# 数据目录
RUN mkdir -p /app/data

EXPOSE 8000

# 启动（生产模式：多 worker）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
