# 食品企业知识库 API 镜像
# 构建: docker build -t factory-ontology-api .
# 运行: docker compose up -d   (见 docker-compose.yml)
FROM python:3.11-slim

WORKDIR /app

# 先复制依赖清单再装依赖, 利用 Docker 层缓存加速重复构建
COPY requirements.txt requirements-optional.txt ./
# 核心运行依赖 (API + 规则引擎 + GraphRAG)；可选依赖不装, 保持镜像精简
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端代码与前端 APP 页面
COPY codes/ ./codes/
COPY web/food_app/ ./web/food_app/

WORKDIR /app/codes

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

EXPOSE 8000
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
