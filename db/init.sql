-- ============================================================================
-- 流域归属与观测完整性约束
--
--   * 归属时段: [valid_from, valid_to) 左闭右开, valid_to 可空(=至今有效)
--   * 规则1: 同一站点任意两个时段不得重叠
--   * 规则2: 每条观测在采样时刻必须恰好落入本站的某一个归属时段
--            (不重叠保证"至多一段", 覆盖检查保证"至少一段", 合起来即"恰好一段")
--   * 约束以 DEFERRABLE INITIALLY DEFERRED 触发器在提交时统一校验:
--       事务中途允许暂时违规(便于拆分/合并/更正时段并补改观测),
--       提交时必须全部满足, 否则整笔回滚
--   * 修改时段/观测前按站点取行锁(SELECT ... FOR UPDATE), 使同站并发写
--     串行化; 不同站持不同锁行, 互不阻塞
--   * 约束保存在数据库目录中, 对 API 与任意直连 SQL 会话(READ COMMITTED)
--     同样生效, 重启后仍然有效
--
-- 性能设计(增量提交校验, 不扫整站历史):
--   会话临时表(ON COMMIT DELETE ROWS)记录本事务触及的脏数据:
--     dirty_observations -- 被插入/更新的观测(删除不登记: 删观测不会制造孤儿)
--     dirty_periods      -- 被插入/更新的时段 id(删除不登记: 删时段不会制造重叠)
--     dirty_ranges       -- 被插入/更新/删除时段的旧/新覆盖区间
--   提交时按受影响站点去重后只做三件事:
--     规则1: 仅脏时段 id 与终态做 GiST 重叠探测(O(脏时段数·log 时段数))
--     规则2a: 仅脏观测逐行做归属点探测(O(脏观测数·log 时段数))
--     规则2b: 仅在脏区间内做观测索引范围扫描, 找因时段变更而失去归属的观测
--   因此"大站新增一条合法观测"的提交校验与该站历史行数无关;
--   只有触及开放终点(valid_to IS NULL)时段的罕见操作(转属/更正/合并)
--   才会退化为该站全量观测扫描。
--
-- 完备性:
--   * 终态中任何重叠时段对, 至少一个成员是本事务插入/更新的(已提交旧状态
--     本无重叠), 故必在 dirty_periods 中;
--   * 任何提交时刻的无归属观测, 要么自身被插/改(在 dirty_observations 中),
--     要么其原覆盖时段被插/改/删, 观测时刻落在该时段旧区间或新区间内
--     (在 dirty_ranges 中)。
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE stations (
    station_id  text PRIMARY KEY,
    name        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE attribution_periods (
    period_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id  text NOT NULL REFERENCES stations (station_id),
    watershed   text NOT NULL,
    valid_from  timestamptz NOT NULL,
    valid_to    timestamptz,                       -- NULL = 至今有效
    CONSTRAINT period_bounds_ok CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE observations (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id  text NOT NULL REFERENCES stations (station_id),
    observed_at timestamptz NOT NULL,
    value       double precision NOT NULL
);

CREATE INDEX periods_station_range_idx
    ON attribution_periods USING gist (station_id, tstzrange(valid_from, valid_to, '[)'));
CREATE INDEX observations_station_time_idx
    ON observations (station_id, observed_at);

-- ----------------------------------------------------------------------------
-- 会话级临时表: 本事务脏数据登记 + 每站"已检"标记(随提交/回滚自动清空)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ensure_check_temp_tables() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    -- 会话内已建过则静默跳过(临时表 ON COMMIT DELETE ROWS, 跨事务复用)
    IF to_regclass('pg_temp.pending_station_checks') IS NOT NULL THEN
        RETURN;
    END IF;
    CREATE TEMP TABLE pending_station_checks (
        station_id text PRIMARY KEY
    ) ON COMMIT DELETE ROWS;
    CREATE TEMP TABLE IF NOT EXISTS dirty_observations (
        observation_id bigint NOT NULL,
        station_id     text NOT NULL,
        observed_at    timestamptz NOT NULL
    ) ON COMMIT DELETE ROWS;
    CREATE TEMP TABLE IF NOT EXISTS dirty_periods (
        station_id text NOT NULL,
        period_id  bigint NOT NULL
    ) ON COMMIT DELETE ROWS;
    CREATE TEMP TABLE IF NOT EXISTS dirty_ranges (
        station_id text NOT NULL,
        range_from timestamptz NOT NULL,
        range_to   timestamptz                       -- NULL = 开放终点(+∞)
    ) ON COMMIT DELETE ROWS;
END;
$$;

-- ----------------------------------------------------------------------------
-- 全量审计(人工运维用): 无重叠时段 + 每条观测都有归属, 扫整站
-- 写路径触发器不调用本函数, 提交校验走 check_station_integrity_incremental
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_station_integrity(p_station_id text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_bad bigint;
BEGIN
    SELECT a.period_id INTO v_bad
    FROM attribution_periods a
    JOIN attribution_periods b
      ON b.station_id = a.station_id
     AND a.period_id < b.period_id
     AND tstzrange(a.valid_from, a.valid_to, '[)') && tstzrange(b.valid_from, b.valid_to, '[)')
    WHERE a.station_id = p_station_id
    LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'station %: attribution periods overlap (period_id=%)', p_station_id, v_bad
            USING ERRCODE = '23514';
    END IF;

    SELECT o.observation_id INTO v_bad
    FROM observations o
    WHERE o.station_id = p_station_id
      AND NOT EXISTS (
            SELECT 1
            FROM attribution_periods p
            WHERE p.station_id = o.station_id
              AND tstzrange(p.valid_from, p.valid_to, '[)') @> o.observed_at
          )
    LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'station %: observation % has no covering attribution period', p_station_id, v_bad
            USING ERRCODE = '23514';
    END IF;
END;
$$;

-- ----------------------------------------------------------------------------
-- 增量提交校验: 只检查本事务脏数据, 成本与站点历史行数无关
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_station_integrity_incremental(p_station_id text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_bad bigint;
BEGIN
    -- 规则1: 终态重叠对必含本事务插/改的时段(删除不会制造重叠)。
    --        只对脏时段 id 做 GiST 重叠探测, 不做时段自连接。
    SELECT a.period_id INTO v_bad
    FROM attribution_periods a
    WHERE a.station_id = p_station_id
      AND a.period_id IN (
            SELECT DISTINCT period_id
            FROM dirty_periods
            WHERE station_id = p_station_id
          )
      AND EXISTS (
            SELECT 1
            FROM attribution_periods b
            WHERE b.station_id = a.station_id
              AND b.period_id <> a.period_id
              AND tstzrange(a.valid_from, a.valid_to, '[)')
                  && tstzrange(b.valid_from, b.valid_to, '[)')
          )
    LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'station %: attribution periods overlap (period_id=%)', p_station_id, v_bad
            USING ERRCODE = '23514';
    END IF;

    -- 规则2a: 本事务插/改且仍存在的观测, 逐行做归属点探测(索引)。
    --         已在本事务删除的观测 JOIN 不到, 自然跳过。空表同样先短路,
    --         避免无统计临时表在大站上被规划成嵌套循环全扫。
    PERFORM 1 FROM dirty_observations WHERE station_id = p_station_id LIMIT 1;
    IF FOUND THEN
        SELECT o.observation_id INTO v_bad
        FROM (
            SELECT DISTINCT observation_id, station_id
            FROM dirty_observations
            WHERE station_id = p_station_id
        ) d
        JOIN observations o ON o.observation_id = d.observation_id
        WHERE d.station_id = p_station_id
          AND NOT EXISTS (
                SELECT 1
                FROM attribution_periods p
                WHERE p.station_id = o.station_id
                  AND tstzrange(p.valid_from, p.valid_to, '[)') @> o.observed_at
              )
        LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'station %: observation % has no covering attribution period', p_station_id, v_bad
                USING ERRCODE = '23514';
        END IF;
    END IF;

    -- 规则2b: 时段插/改/删只可能让其旧区间或新区间内的观测失去归属;
    --         按脏区间走观测 (station_id, observed_at) 索引范围扫描。
    --         range_to 为 NULL(开放终点)时覆盖到 +∞, 此时退化为该站全扫,
    --         仅发生在转属/更正/合并触及开放时段的罕见路径。
    -- 守卫: 临时表无统计信息, 优化器对空表也会按估算行数生成嵌套循环,
    --       退化为整站观测扫描; 先做空表短路(纯观测插/改时 dirty_ranges 恒空)。
    PERFORM 1 FROM dirty_ranges WHERE station_id = p_station_id LIMIT 1;
    IF FOUND THEN
        SELECT o.observation_id INTO v_bad
        FROM (
            SELECT DISTINCT station_id, range_from, range_to
            FROM dirty_ranges
            WHERE station_id = p_station_id
        ) r
        JOIN observations o
          ON o.station_id = r.station_id
         AND o.observed_at >= r.range_from
         AND (r.range_to IS NULL OR o.observed_at < r.range_to)
        WHERE r.station_id = p_station_id
          AND NOT EXISTS (
                SELECT 1
                FROM attribution_periods p
                WHERE p.station_id = o.station_id
                  AND tstzrange(p.valid_from, p.valid_to, '[)') @> o.observed_at
              )
        LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'station %: observation % has no covering attribution period', p_station_id, v_bad
                USING ERRCODE = '23514';
        END IF;
    END IF;
END;
$$;

-- ----------------------------------------------------------------------------
-- BEFORE 行触发器:
--   1. 按站点取排他行锁(升序加锁降低死锁概率), 同站并发写串行化
--   2. 使该站本事务内的"已检"标记失效(支持 SET CONSTRAINTS ... IMMEDIATE
--      后继续修改, 提交时重新强检)
--   3. 登记脏观测/脏时段/脏区间, 供提交时增量校验
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION before_change_station() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_stations text[];
    s text;
BEGIN
    PERFORM ensure_check_temp_tables();

    IF TG_OP = 'DELETE' THEN
        v_stations := ARRAY[OLD.station_id];
    ELSIF TG_OP = 'INSERT' THEN
        v_stations := ARRAY[NEW.station_id];
    ELSIF NEW.station_id IS DISTINCT FROM OLD.station_id THEN
        v_stations := ARRAY[OLD.station_id, NEW.station_id];
    ELSE
        v_stations := ARRAY[NEW.station_id];
    END IF;

    SELECT array_agg(x ORDER BY x) INTO v_stations FROM unnest(v_stations) AS x;

    FOREACH s IN ARRAY v_stations LOOP
        PERFORM 1 FROM stations WHERE station_id = s FOR UPDATE;
        DELETE FROM pending_station_checks WHERE station_id = s;
    END LOOP;

    IF TG_TABLE_NAME = 'observations' THEN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO dirty_observations VALUES (NEW.observation_id, NEW.station_id, NEW.observed_at);
        ELSIF TG_OP = 'UPDATE' THEN
            -- 站间转移/时刻变更: 旧值与新值都登记, 两个站都会被校验
            INSERT INTO dirty_observations VALUES
                (OLD.observation_id, OLD.station_id, OLD.observed_at),
                (NEW.observation_id, NEW.station_id, NEW.observed_at);
        END IF;
        -- DELETE 不可能制造无归属观测, 不登记
    ELSIF TG_TABLE_NAME = 'attribution_periods' THEN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO dirty_periods VALUES (NEW.station_id, NEW.period_id);
            INSERT INTO dirty_ranges VALUES (NEW.station_id, NEW.valid_from, NEW.valid_to);
        ELSIF TG_OP = 'UPDATE' THEN
            INSERT INTO dirty_periods VALUES
                (OLD.station_id, OLD.period_id),
                (NEW.station_id, NEW.period_id);
            INSERT INTO dirty_ranges VALUES
                (OLD.station_id, OLD.valid_from, OLD.valid_to),
                (NEW.station_id, NEW.valid_from, NEW.valid_to);
        ELSE -- DELETE
            INSERT INTO dirty_ranges VALUES (OLD.station_id, OLD.valid_from, OLD.valid_to);
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$;

-- ----------------------------------------------------------------------------
-- 提交时(DEFERRABLE INITIALLY DEFERRED)对受影响站点跑增量强检;
-- 同一轮触发内按站点去重, 一笔事务改 N 行每站只检一次
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION deferred_station_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_stations text[];
    s text;
BEGIN
    PERFORM ensure_check_temp_tables();

    IF TG_OP = 'DELETE' THEN
        v_stations := ARRAY[OLD.station_id];
    ELSIF TG_OP = 'INSERT' THEN
        v_stations := ARRAY[NEW.station_id];
    ELSIF NEW.station_id IS DISTINCT FROM OLD.station_id THEN
        v_stations := ARRAY[OLD.station_id, NEW.station_id];
    ELSE
        v_stations := ARRAY[NEW.station_id];
    END IF;

    SELECT array_agg(x ORDER BY x) INTO v_stations FROM unnest(v_stations) AS x;

    FOREACH s IN ARRAY v_stations LOOP
        INSERT INTO pending_station_checks (station_id) VALUES (s) ON CONFLICT DO NOTHING;
        IF FOUND THEN
            PERFORM check_station_integrity_incremental(s);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE TRIGGER periods_lock_station
    BEFORE INSERT OR UPDATE OR DELETE ON attribution_periods
    FOR EACH ROW EXECUTE FUNCTION before_change_station();
CREATE TRIGGER observations_lock_station
    BEFORE INSERT OR UPDATE OR DELETE ON observations
    FOR EACH ROW EXECUTE FUNCTION before_change_station();

CREATE CONSTRAINT TRIGGER periods_check_at_commit
    AFTER INSERT OR UPDATE OR DELETE ON attribution_periods
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION deferred_station_check();
CREATE CONSTRAINT TRIGGER observations_check_at_commit
    AFTER INSERT OR UPDATE OR DELETE ON observations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION deferred_station_check();

-- ----------------------------------------------------------------------------
-- 禁止 TRUNCATE (它绕过行触发器, 会留下无归属观测)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE is not allowed on %: integrity triggers must run per row', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER periods_no_truncate
    BEFORE TRUNCATE ON attribution_periods
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();
CREATE TRIGGER observations_no_truncate
    BEFORE TRUNCATE ON observations
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();

-- ----------------------------------------------------------------------------
-- 供数视图: 按采样时刻关联流域 (READ COMMITTED 下只见完整已提交状态)
-- ----------------------------------------------------------------------------
CREATE VIEW observation_attribution AS
SELECT o.observation_id,
       o.station_id,
       o.observed_at,
       o.value,
       p.period_id,
       p.watershed,
       p.valid_from AS period_valid_from,
       p.valid_to   AS period_valid_to
FROM observations o
JOIN attribution_periods p
  ON p.station_id = o.station_id
 AND tstzrange(p.valid_from, p.valid_to, '[)') @> o.observed_at;

COMMIT;
