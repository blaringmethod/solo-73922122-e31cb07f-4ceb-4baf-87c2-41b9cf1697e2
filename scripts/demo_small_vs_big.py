#!/usr/bin/env python3
"""/docs 演示配套: 小站 vs 大站的合法写入与违规回滚(经 HTTP API 实测)。"""
import os
import time
import uuid

import httpx
import psycopg

API = os.environ.get("API_URL", "http://127.0.0.1:8020")
DSN = os.environ.get("DATABASE_URL", "postgresql://watershed@127.0.0.1:5433/wnew2")
BIG_N = int(os.environ.get("BIG_N", "100000"))


def seed(sid: str, rows: int) -> None:
    with psycopg.connect(DSN) as c:
        c.execute("INSERT INTO stations (station_id, name) VALUES (%s,'demo')", (sid,))
        c.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s,'WS-A','2000-01-01 00:00+00',NULL)", (sid,))
        if rows:
            c.execute(
                "INSERT INTO observations (station_id, observed_at, value) "
                "SELECT %s, ts, random() FROM generate_series("
                "'2000-01-01 00:00+00'::timestamptz,"
                "'2000-01-01 00:00+00'::timestamptz + (%s||' minutes')::interval,"
                "interval '1 minute') ts", (sid, rows))
        c.commit()


def timed_insert(client: httpx.Client, sid: str, ts: str, ok: bool) -> float:
    t0 = time.perf_counter()
    r = client.post("/observations", json={"station_id": sid, "observed_at": ts, "value": 1.0})
    ms = (time.perf_counter() - t0) * 1000
    expected = 201 if ok else 409
    assert r.status_code == expected, (r.status_code, r.text)
    return ms


def main() -> None:
    small, big = "SMALL-" + uuid.uuid4().hex[:8], "BIG-" + uuid.uuid4().hex[:8]
    seed(small, 100)
    seed(big, BIG_N)
    with httpx.Client(base_url=API, timeout=60) as cli, psycopg.connect(DSN) as db:
        print(f"小站 {small}: 100 条历史; 大站 {big}: {BIG_N} 条历史\n")

        # 合法写入: 延迟应同量级, 与历史行数无关
        s_ok = [timed_insert(cli, small, f"2025-01-0{i+1}T00:00:00Z", True) for i in range(3)]
        b_ok = [timed_insert(cli, big, f"2025-01-0{i+1}T00:00:00Z", True) for i in range(3)]
        print(f"[合法补录] 小站中位 {sorted(s_ok)[1]:6.1f} ms | 大站中位 {sorted(b_ok)[1]:6.1f} ms")

        # 违规写入(无归属): 409 整笔回滚
        before = db.execute("SELECT count(*) FROM observations WHERE station_id=%s", (big,)).fetchone()[0]
        ms = timed_insert(cli, big, "1999-01-01T00:00:00Z", False)
        after = db.execute("SELECT count(*) FROM observations WHERE station_id=%s", (big,)).fetchone()[0]
        print(f"[违规补录] 大站 409 回滚 {ms:6.1f} ms, 行数 {before} -> {after} (未增加)")

        # 观测时刻改到无归属区间: 409 回滚, 原值不变
        oid = db.execute("SELECT observation_id FROM observations WHERE station_id=%s LIMIT 1", (big,)).fetchone()[0]
        r = cli.post("/changesets", json={"operations": [
            {"op": "update_observation", "observation_id": oid,
             "observed_at": "1999-06-01T00:00:00Z"}]})
        assert r.status_code == 409, r.text
        kept = db.execute("SELECT observed_at FROM observations WHERE observation_id=%s", (oid,)).fetchone()[0]
        print(f"[时刻变更违规] 409 回滚, 观测 {oid} 时刻保持 {kept.isoformat()}")


if __name__ == "__main__":
    main()
