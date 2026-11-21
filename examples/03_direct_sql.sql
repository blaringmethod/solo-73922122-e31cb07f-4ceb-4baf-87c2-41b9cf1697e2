-- 示例3: 独立 READ COMMITTED SQL 会话直连数据库, 与 API 受同一套约束
-- 运行: docker compose exec -T db psql postgresql://watershed:watershed@localhost:5432/watershed -f - < examples/03_direct_sql.sql
-- (或本地: psql "$DATABASE_URL" -f examples/03_direct_sql.sql)
\set ON_ERROR_STOP off
\echo '=================================================='
\echo '0. 准备(可重复执行): 清理旧演示数据'
\echo '=================================================='
DELETE FROM observations        WHERE station_id = 'ST-SQL-DEMO';
DELETE FROM attribution_periods WHERE station_id = 'ST-SQL-DEMO';
DELETE FROM stations            WHERE station_id = 'ST-SQL-DEMO';
INSERT INTO stations (station_id, name) VALUES ('ST-SQL-DEMO', '直连SQL演示站');

\echo '=================================================='
\echo '1. 一笔事务内先违规后修复: 提交时满足即成功'
\echo '=================================================='
BEGIN;
INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
VALUES ('ST-SQL-DEMO', 'WS-A', '2020-01-01 00:00+00', '2022-01-01 00:00+00');
INSERT INTO observations (station_id, observed_at, value)
VALUES ('ST-SQL-DEMO', '2021-01-01 00:00+00', 7.7);
-- 先删时段: 此刻观测失去归属(中途暂时违规, 语句可执行)
DELETE FROM attribution_periods WHERE station_id = 'ST-SQL-DEMO';
-- 同事务内补回覆盖时段(改属 WS-B)
INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
VALUES ('ST-SQL-DEMO', 'WS-B', '2020-01-01 00:00+00', '2022-01-01 00:00+00');
COMMIT;  -- 提交时校验通过
\echo '-- 提交成功, 观测现属 WS-B:'
SELECT observed_at, value, watershed
FROM observation_attribution
WHERE station_id = 'ST-SQL-DEMO';

\echo '=================================================='
\echo '2. 提交时仍违规: 整笔回滚(撤段不补回)'
\echo '=================================================='
BEGIN;
DELETE FROM attribution_periods WHERE station_id = 'ST-SQL-DEMO';
COMMIT;  -- 预期 ERROR: observation ... has no covering attribution period
\echo '-- 回滚后数据仍在:'
SELECT count(*) AS periods FROM attribution_periods WHERE station_id = 'ST-SQL-DEMO';

\echo '=================================================='
\echo '3. 重叠时段: 同样被提交时校验拒绝'
\echo '=================================================='
BEGIN;
INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
VALUES ('ST-SQL-DEMO', 'WS-C', '2021-06-01 00:00+00', '2023-01-01 00:00+00');
COMMIT;  -- 预期 ERROR: attribution periods overlap
SELECT count(*) AS periods FROM attribution_periods WHERE station_id = 'ST-SQL-DEMO';

\echo '=================================================='
\echo '4. 无归属补录: 被拒绝'
\echo '=================================================='
INSERT INTO observations (station_id, observed_at, value)
VALUES ('ST-SQL-DEMO', '2025-01-01 00:00+00', 1.0);  -- 预期 ERROR
SELECT count(*) AS observations FROM observations WHERE station_id = 'ST-SQL-DEMO';

\echo '=================================================='
\echo '5. 清理演示数据(整笔删除: 先删观测再删时段)'
\echo '=================================================='
BEGIN;
DELETE FROM observations        WHERE station_id = 'ST-SQL-DEMO';
DELETE FROM attribution_periods WHERE station_id = 'ST-SQL-DEMO';
DELETE FROM stations            WHERE station_id = 'ST-SQL-DEMO';
COMMIT;
\echo '完成'
