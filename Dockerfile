# 食品企业知识库 API 镜像
# 构建: docker build -t factory-ontology-api .
# 运行: docker compose up -d   (见 docker-compose.yml)
FROM python:3.11-slim

WORKDIR /app
COPY codes/ ./codes/
WORKDIR /app/codes

# 运行时依赖(API + 规则引擎 + GraphRAG, 不含语音STT以保持镜像精简)
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

EXPOSE 8000
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
