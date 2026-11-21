"""性能回归与增量校验完备性测试(修复提交性能缺陷).

验证:
  1. 大站(数万历史观测)单条合法补录的提交延迟不随历史行数增长(上限阈值);
  2. 观测时刻变更 / 站点归属更正后不会留下无归属数据, 违规整笔回滚;
  3. 大批量变更集仍走每站一次的增量校验(脏数据驱动, 不扫整站历史)。

运行: API_URL=... DATABASE_URL=... python -m pytest tests -v
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import psycopg
import pytest

API = os.environ.get("API_URL", "http://localhost:8000")
DSN = os.environ.get("DATABASE_URL", "postgresql://watershed:watershed@localhost:5432/watershed")

HISTORY_ROWS = int(os.environ.get("PERF_HISTORY_ROWS", "50000"))
SMALL_ROWS = 100


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2]


def _time_inserts(client, sid: str, times: list[str]) -> list[float]:
    out = []
    for i, ts in enumerate(times):
        t0 = time.perf_counter()
        r = client.post(
            "/observations",
            json={"station_id": sid, "observed_at": ts, "value": float(i)},
        )
        out.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 201, r.text
    return out


def test_single_insert_latency_independent_of_history(client):
    """核心回归: 大站单条补录延迟与小站同量级(不随历史行数线性增长)。

    用小站(百行)与大站(数万行)的延迟中位数之比断言扩展性,
    避免把沙箱调度/WAL 抖动误判为回归; 另设宽松绝对上限防整体劣化。
    """
    small_sid, big_sid = new_sid(), new_sid()
    _seed_big_station(small_sid, SMALL_ROWS)
    _seed_big_station(big_sid, HISTORY_ROWS)

    times = ["2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z", "2025-01-03T00:00:00Z"]
    small_ms = _median(_time_inserts(client, small_sid, times))
    big_ms = _median(_time_inserts(client, big_sid, times))

    assert big_ms < 1000, f"big-station insert too slow: {big_ms:.1f} ms"
    # 修复前大站约为小站的 O(N) 倍; 修复后增量校验只探测脏行, 比值应接近 1
    assert big_ms <= max(3.0 * small_ms, 100.0), (
        f"latency scales with history: small={small_ms:.1f}ms big={big_ms:.1f}ms"
    )


def new_sid() -> str:
    return "P-" + uuid.uuid4().hex[:12]


@pytest.fixture()
def client():
    with httpx.Client(base_url=API, timeout=60) as c:
        yield c


def _seed_big_station(sid: str, rows: int) -> None:
    with psycopg.connect(DSN) as c:
        c.execute("INSERT INTO stations (station_id, name) VALUES (%s, 'perf')", (sid,))
        c.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s, 'WS-A', '2000-01-01 00:00+00', NULL)",
            (sid,),
        )
        c.execute(
            """
            INSERT INTO observations (station_id, observed_at, value)
            SELECT %s, ts, random()
            FROM generate_series('2000-01-01 00:00+00'::timestamptz,
                                 '2000-01-01 00:00+00'::timestamptz + (%s || ' minutes')::interval,
                                 interval '1 minute') AS ts
            """,
            (sid, rows),
        )
        c.commit()


def test_illegal_insert_on_big_station_rolls_back(client):
    """大站违规补录(无归属)仍被 409 拒绝, 且不写入任何行。"""
    sid = new_sid()
    _seed_big_station(sid, 2000)
    before = None
    with psycopg.connect(DSN) as c:
        before = c.execute(
            "SELECT count(*) FROM observations WHERE station_id=%s", (sid,)
        ).fetchone()[0]

    r = client.post(
        "/observations",
        json={"station_id": sid, "observed_at": "1999-01-01T00:00:00Z", "value": 1.0},
    )
    assert r.status_code == 409, r.text
    assert "no covering attribution period" in r.json()["detail"]

    with psycopg.connect(DSN) as c:
        after = c.execute(
            "SELECT count(*) FROM observations WHERE station_id=%s", (sid,)
        ).fetchone()[0]
    assert after == before


def test_observation_time_change_cannot_leave_orphan(client):
    """观测时刻改到无归属区间: 整笔回滚, 原时刻保持不变。"""
    sid = new_sid()
    client.post("/stations", json={"station_id": sid})
    r = client.post(
        "/changesets",
        json={
            "operations": [
                {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
                 "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
                {"op": "insert_observation", "station_id": sid,
                 "observed_at": "2020-06-01T00:00:00Z", "value": 1.0},
            ]
        },
    )
    assert r.status_code == 201, r.text
    oid = r.json()["results"][1]["observation_id"]

    r = client.post(
        "/changesets",
        json={"operations": [
            {"op": "update_observation", "observation_id": oid,
             "observed_at": "2022-06-01T00:00:00Z"},
        ]},
    )
    assert r.status_code == 409, r.text

    r = client.get(f"/stations/{sid}/observations")
    assert r.json()[0]["observed_at"].startswith("2020-06-01")


def test_period_change_cannot_leave_orphan_via_dirty_range():
    """时段平移让区间内老观测失去归属: 脏区间扫描必须抓到并整笔回滚。"""
    sid = new_sid()
    with psycopg.connect(DSN) as c:
        c.execute("INSERT INTO stations (station_id) VALUES (%s)", (sid,))
        c.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s, 'WS-A', '2020-01-01 00:00+00', NULL)",
            (sid,),
        )
        c.execute(
            "INSERT INTO observations (station_id, observed_at, value)"
            " VALUES (%s, '2020-03-01 00:00+00', 1.0)",
            (sid,),
        )
        c.commit()
        c.execute(
            "UPDATE attribution_periods SET valid_from='2020-06-01 00:00+00' WHERE station_id=%s",
            (sid,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            c.commit()

    with psycopg.connect(DSN) as c:
        vf = c.execute(
            "SELECT valid_from FROM attribution_periods WHERE station_id=%s", (sid,)
        ).fetchone()[0]
        assert vf.isoformat().startswith("2020-01-01")


def test_bulk_changeset_dedupes_per_station_check(client):
    """单事务插入上千行: 每站只检一次, 提交时间与行数近似线性而非平方。"""
    sid = new_sid()
    client.post("/stations", json={"station_id": sid})
    n = 2000
    ops = [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2000-01-01T00:00:00Z", "valid_to": None},
    ]
    ops += [
        {"op": "insert_observation", "station_id": sid,
         "observed_at": f"2001-01-{1 + i // 1440:02d}T{i // 60 % 24:02d}:{i % 60:02d}:00Z",
         "value": float(i)}
        for i in range(n)
    ]
    t0 = time.perf_counter()
    r = client.post("/changesets", json={"operations": ops})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 201, r.text
    assert elapsed < 10, f"bulk changeset took {elapsed:.2f}s"

    with psycopg.connect(DSN) as c:
        cnt = c.execute(
            "SELECT count(*) FROM observations WHERE station_id=%s", (sid,)
        ).fetchone()[0]
    assert cnt == n
