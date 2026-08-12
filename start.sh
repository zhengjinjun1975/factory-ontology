#!/usr/bin/env bash
# =============================================================================
# factory-ontology-kit 一键启动脚本（Linux / macOS / Git-Bash）
#
# 面向无专职工程师的现场用户：三步做完，浏览器即用。
#   流程：装依赖 → 构建 Web 前端 → 启动 api_server（REST API + 移动端 APP）
#
# 用法：
#   ./start.sh              # 完整链路：装依赖 + 构建前端 + 启动服务
#   只需重建前端（依赖已装好时）： ./start.sh --build-only
#   只启动服务（依赖与前端已就绪）： ./start.sh --serve-only
#
# 启动后：
#   - REST API + 移动端 APP  : http://localhost:8000
#   - 现代 Web UI（可选）    : cd web && npm start  → http://localhost:3001
#
# 提示：生产对外开放前请设置鉴权环境变量 FOOD_ADMIN_KEY / FOOD_READ_KEY，
#       否则 /api/* 一律返回 401（见 docs/新机器部署验收.md「配置」）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ 未找到 python/python3，请先安装 Python 3.9+ 并加入 PATH。" >&2
  exit 1
fi

MODE="${1:-all}"

# ---------- [1/3] 装依赖 ----------
if [ "$MODE" = "all" ] || [ "$MODE" = "build-only" ] || [ "$MODE" = "install" ]; then
  echo "==> [1/3] 安装 Python 依赖"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$ROOT/requirements.txt"
fi

# ---------- [2/3] 构建 Web 前端 ----------
if [ "$MODE" = "all" ] || [ "$MODE" = "build-only" ] || [ "$MODE" = "build" ]; then
  echo "==> [2/3] 构建 Web 前端（Svelte5）"
  if ! command -v npm >/dev/null 2>&1; then
    echo "✗ 未找到 npm，请先安装 Node.js 18+。" >&2
    exit 1
  fi
  cd "$ROOT/web"
  if [ ! -d node_modules ]; then
    npm install
  fi
  npm run build
fi

# ---------- [3/3] 启动 api_server ----------
if [ "$MODE" = "all" ] || [ "$MODE" = "serve-only" ] || [ "$MODE" = "serve" ]; then
  echo "==> [3/3] 启动 api_server"
  echo "    REST API + 移动端 APP: http://localhost:8000"
  echo "    现代 Web UI（可选）: 另开终端 cd web && npm start → http://localhost:3001"
  cd "$ROOT/codes"
  exec "$PY" api_server.py
fi

echo "完成。"
