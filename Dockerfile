# 容器镜像：把应用与依赖打成一包，换机器一条命令跑起来。
# 用法（在仓库根目录）：
#   docker build -t case-analyzer .
#   docker run --rm -p 8770:8770 -v case-data:/app/runtime case-analyzer
# 带模型 Key：docker run --rm -p 8770:8770 -e DEEPSEEK_API_KEY=... -v case-data:/app/runtime case-analyzer
FROM python:3.12-slim

ENV PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CASE_ANALYZER_HOST=0.0.0.0

WORKDIR /app

# 先装依赖再拷源码，改动代码不必重装依赖（利用构建缓存）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 运行；runtime/ 放卷里，容器重建不丢数据。
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/runtime && chown -R appuser:appuser /app
USER appuser

EXPOSE 8770
VOLUME ["/app/runtime"]

# --no-open：容器里没有浏览器；访问地址由端口映射决定（宿主机 localhost:8770）。
CMD ["python", "-B", "-m", "app", "--host", "0.0.0.0", "--no-open"]
