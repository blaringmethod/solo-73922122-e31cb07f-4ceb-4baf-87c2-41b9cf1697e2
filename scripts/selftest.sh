#!/usr/bin/env bash
# 一键自测: 启动 compose 栈, 跑示例与集成测试
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 启动数据库与 API =="
docker compose up -d --build --wait

echo "== /health 与 /docs =="
curl -fsS http://localhost:8000/health
echo
curl -fsS http://localhost:8000/docs >/dev/null && echo "/docs OK"

echo "== 示例1: 转属 =="
bash examples/01_transfer.sh

echo "== 示例2: 历史更正 =="
bash examples/02_historical_correction.sh

echo "== 示例3: 直连 SQL =="
docker compose exec -T db psql postgresql://watershed:watershed@localhost:5432/watershed \
  -v ON_ERROR_STOP=0 -f - < examples/03_direct_sql.sql

echo "== 示例4: 并发撤段 vs 补录 =="
bash examples/04_concurrent_conflict.sh

echo "== 集成测试 =="
python3 -m venv .venv
.venv/bin/pip install -q -r tests/requirements.txt
.venv/bin/python -m pytest tests -v

echo "== 自测完成 =="
