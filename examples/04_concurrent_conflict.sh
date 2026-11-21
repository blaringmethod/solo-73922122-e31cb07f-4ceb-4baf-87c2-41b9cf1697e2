#!/usr/bin/env bash
# 示例4: 并发撤段与补录 —— 同站并发写被串行化, 冲突整笔拒绝, 不会留下无归属观测
# 注意: 场景1 依赖正常的锁等待/唤醒语义(等待方在锁释放后被唤醒再于提交时被拒)。
#       请在 Docker/常规 Linux 环境运行; 若运行环境阻断进程间信号(某些沙箱),
#       等待方可能无法被唤醒, 请改用 pytest 中的顺序化等价用例验证。
set -uo pipefail

DSN=${DATABASE_URL:-postgresql://watershed:watershed@localhost:5432/watershed}
if [ -n "${PSQL_CMD:-}" ]; then
  :
elif command -v docker >/dev/null 2>&1; then
  PSQL_CMD="docker compose exec -T db psql $DSN -v ON_ERROR_STOP=0"
else
  PSQL_CMD="psql $DSN -v ON_ERROR_STOP=0"
fi

SID=${SID:-ST-CONC-$(date +%Y%m%d%H%M%S)}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "== 站点: $SID =="
$PSQL_CMD -q <<SQL
INSERT INTO stations (station_id, name) VALUES ('$SID', '并发演示站');
INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
VALUES ('$SID', 'WS-OLD', '2020-01-01 00:00+00', '2021-01-01 00:00+00');
SQL

echo
echo "== 场景1: 撤段先持锁并提交, 补录被阻塞后在提交时被拒 =="
( echo "BEGIN; DELETE FROM attribution_periods WHERE station_id='$SID'; SELECT pg_sleep(3); COMMIT;" \
    | $PSQL_CMD -q ) >"$TMP/a.log" 2>&1 &
PID_A=$!
sleep 1
( echo "INSERT INTO observations (station_id, observed_at, value) VALUES ('$SID', '2020-06-01 00:00+00', 42.0);" \
    | $PSQL_CMD -q ) >"$TMP/b.log" 2>&1 &
PID_B=$!
wait $PID_A $PID_B
echo "-- 撤段事务输出:"; cat "$TMP/a.log"
echo "-- 补录事务输出(预期 ERROR: no covering attribution period):"; cat "$TMP/b.log"
echo "-- 最终状态(时段0、观测0, 无孤儿观测):"
$PSQL_CMD -c "SELECT (SELECT count(*) FROM attribution_periods WHERE station_id='$SID') AS periods, (SELECT count(*) FROM observations WHERE station_id='$SID') AS observations;"

echo
echo "== 场景2: 补录先提交, 撤段在提交时被拒 =="
$PSQL_CMD -q <<SQL
INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
VALUES ('$SID', 'WS-OLD', '2020-01-01 00:00+00', '2021-01-01 00:00+00');
SQL
( echo "INSERT INTO observations (station_id, observed_at, value) VALUES ('$SID', '2020-06-01 00:00+00', 42.0);" \
    | $PSQL_CMD -q ) >"$TMP/c.log" 2>&1 &
PID_C=$!
wait $PID_C
( echo "BEGIN; DELETE FROM attribution_periods WHERE station_id='$SID'; COMMIT;" \
    | $PSQL_CMD -q ) >"$TMP/d.log" 2>&1 &
PID_D=$!
wait $PID_D
echo "-- 补录事务输出:"; cat "$TMP/c.log"
echo "-- 撤段事务输出(预期 ERROR: no covering attribution period):"; cat "$TMP/d.log"
echo "-- 最终状态(时段1、观测1, 归属完好):"
$PSQL_CMD -c "SELECT (SELECT count(*) FROM attribution_periods WHERE station_id='$SID') AS periods, (SELECT count(*) FROM observations WHERE station_id='$SID') AS observations;"

echo
echo "== 清理 =="
$PSQL_CMD -q <<SQL
BEGIN;
DELETE FROM observations        WHERE station_id='$SID';
DELETE FROM attribution_periods WHERE station_id='$SID';
DELETE FROM stations            WHERE station_id='$SID';
COMMIT;
SQL
echo "完成"
