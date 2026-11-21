#!/usr/bin/env bash
# 示例1: 站点转属 —— 站点中途划归另一流域, 历史观测归属不变, 新观测归属新流域
set -euo pipefail

API=${API_URL:-http://localhost:8000}
SID=${SID:-ST-TRANS-$(date +%Y%m%d%H%M%S)}

echo "== 站点: $SID =="

echo "-- 1. 建站"
curl -sS -X POST "$API/stations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"name\":\"示例水文站\"}" | jq .

echo "-- 2. 初始归属 WS-YANGTZE-UPPER [2020-01-01, 至今), 并补录两条观测"
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_period\",\"station_id\":\"$SID\",\"watershed\":\"WS-YANGTZE-UPPER\",
     \"valid_from\":\"2020-01-01T00:00:00Z\",\"valid_to\":null},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2020-06-01T00:00:00Z\",\"value\":101.5},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2021-06-01T00:00:00Z\",\"value\":99.2}
  ]
}" | jq .

echo "-- 3. 2021-01-01 起转属 WS-JINSHA"
curl -sS -X POST "$API/stations/$SID/transfer" -H 'Content-Type: application/json' \
  -d '{"at":"2021-01-01T00:00:00Z","new_watershed":"WS-JINSHA"}' | jq .

echo "-- 4. 时段列表: [2020,2021) 属旧流域, [2021,∞) 属新流域"
curl -sS "$API/stations/$SID/periods" | jq .

echo "-- 5. 供数: 按采样时刻关联流域(同一站点两条观测分属不同流域)"
curl -sS "$API/stations/$SID/observations" | jq .
