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
-- 单站完整性强检: 无重叠时段 + 每条观测都有归属
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
-- BEFORE 行触发器: 按站点取排他行锁(升序加锁降低死锁概率),
-- 并使该站本事务内的"已检"标记失效(支持 SET CONSTRAINTS ... IMMEDIATE 后继续改)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION before_change_station() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_stations text[];
    s text;
BEGIN
    IF to_regclass('pg_temp.pending_station_checks') IS NULL THEN
        CREATE TEMP TABLE pending_station_checks (station_id text PRIMARY KEY) ON COMMIT DELETE ROWS;
    END IF;

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
        DELETE FROM pg_temp.pending_station_checks WHERE station_id = s;
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$;

-- ----------------------------------------------------------------------------
-- 提交时(DEFERRABLE INITIALLY DEFERRED)对受影响站点跑强检; 同一轮触发内去重
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION deferred_station_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_stations text[];
    s text;
BEGIN
    IF to_regclass('pg_temp.pending_station_checks') IS NULL THEN
        CREATE TEMP TABLE pending_station_checks (station_id text PRIMARY KEY) ON COMMIT DELETE ROWS;
    END IF;

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
        INSERT INTO pg_temp.pending_station_checks (station_id) VALUES (s) ON CONFLICT DO NOTHING;
        IF FOUND THEN
            PERFORM check_station_integrity(s);
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
