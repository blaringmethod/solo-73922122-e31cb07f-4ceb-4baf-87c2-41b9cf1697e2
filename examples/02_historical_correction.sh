#!/usr/bin/env bash
# 示例2: 历史更正 —— 发现某历史子区间归属录错, 一笔事务内切分时段并补录观测
set -euo pipefail

API=${API_URL:-http://localhost:8000}
SID=${SID:-ST-CORR-$(date +%Y%m%d%H%M%S)}

echo "== 站点: $SID =="

echo "-- 1. 建站 + 一段 [2020,2023) 归属 WS-OLD 的时段 + 三条历史观测"
curl -sS -X POST "$API/stations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"name\":\"历史更正示例站\"}" | jq .
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_period\",\"station_id\":\"$SID\",\"watershed\":\"WS-OLD\",
     \"valid_from\":\"2020-01-01T00:00:00Z\",\"valid_to\":\"2023-01-01T00:00:00Z\"},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2020-06-01T00:00:00Z\",\"value\":10.0},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2021-06-01T00:00:00Z\",\"value\":20.0},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2022-06-01T00:00:00Z\",\"value\":30.0}
  ]
}" | jq .

echo "-- 2. 历史更正: 2021-01-01..2022-01-01 实际应属 WS-NEW (原时段自动切成三段)"
curl -sS -X POST "$API/stations/$SID/correct" -H 'Content-Type: application/json' -d "{
  \"valid_from\":\"2021-01-01T00:00:00Z\",
  \"valid_to\":\"2022-01-01T00:00:00Z\",
  \"watershed\":\"WS-NEW\"
}" | jq .

echo "-- 3. 更正同时补录: 一笔变更集内再删错值、补新值(提交时统一校验)"
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2021-03-01T00:00:00Z\",\"value\":21.5}
  ]
}" | jq .

echo "-- 4. 时段列表: 三段, 中间一段属 WS-NEW"
curl -sS "$API/stations/$SID/periods" | jq .

echo "-- 5. 供数: 观测按采样时刻分别归属 WS-OLD / WS-NEW / WS-OLD"
curl -sS "$API/stations/$SID/observations" | jq .
