#!/usr/bin/env bash
# 重启后约束仍有效: 重启数据库, 再尝试违规写入, 应仍被拒绝
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API_URL:-http://localhost:8000}
SID="ST-RESTART-$(date +%Y%m%d%H%M%S)"

echo "== 重启前: 建站并写入合法数据 =="
curl -sS -X POST "$API/stations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"name\":\"重启校验站\"}" >/dev/null
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_period\",\"station_id\":\"$SID\",\"watershed\":\"WS-A\",
     \"valid_from\":\"2020-01-01T00:00:00Z\",\"valid_to\":\"2021-01-01T00:00:00Z\"},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2020-06-01T00:00:00Z\",\"value\":1.0}
  ]
}" >/dev/null

echo "== 重启数据库与 API =="
docker compose restart db api
docker compose up -d --wait db api

echo "== 重启后: 数据仍在 =="
curl -sS "$API/stations/$SID/observations" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert len(d)==1, d; print("observations:", d)'

echo "== 重启后: 违规撤段仍被 409 拒绝 =="
PID=$(curl -sS "$API/stations/$SID/periods" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["period_id"])')
CODE=$(curl -sS -o /tmp/restart_check_body.json -w '%{http_code}' -X POST "$API/changesets" \
  -H 'Content-Type: application/json' -d "{\"operations\":[{\"op\":\"delete_period\",\"period_id\":$PID}]}")
cat /tmp/restart_check_body.json; echo
[ "$CODE" = "409" ] || { echo "FAIL: expected 409, got $CODE"; exit 1; }

echo "== 重启后: 直连 SQL 撤段仍被拒绝 =="
if docker compose exec -T db psql postgresql://watershed:watershed@localhost:5432/watershed \
    -c "DELETE FROM attribution_periods WHERE station_id='$SID';" 2>/tmp/restart_sql_err.txt; then
  echo "FAIL: direct SQL delete unexpectedly succeeded"; exit 1
else
  grep -q "no covering attribution period" /tmp/restart_sql_err.txt || { cat /tmp/restart_sql_err.txt; exit 1; }
  echo "direct SQL rejected as expected"
fi

echo "== 重启校验通过 =="
