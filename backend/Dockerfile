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

# 启动（单进程：任务登记表与 SQLite 写互斥都在进程内，勿加 --workers，见 DEPLOY.md §4.3）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
