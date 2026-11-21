"""集成测试: 需要运行中的 API 与数据库(默认 localhost:8000 / localhost:5432).

运行: API_URL=... DATABASE_URL=... python -m pytest tests -v
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import httpx
import psycopg
import pytest

API = os.environ.get("API_URL", "http://localhost:8000")
DSN = os.environ.get("DATABASE_URL", "postgresql://watershed:watershed@localhost:5432/watershed")


def new_sid() -> str:
    return "T-" + uuid.uuid4().hex[:12]


@pytest.fixture()
def client():
    with httpx.Client(base_url=API, timeout=30) as c:
        yield c


def mk_station(client: httpx.Client, sid: str) -> None:
    r = client.post("/stations", json={"station_id": sid, "name": "pytest"})
    assert r.status_code == 201, r.text


def changeset(client: httpx.Client, ops: list[dict]) -> httpx.Response:
    return client.post("/changesets", json={"operations": ops})


def periods(client: httpx.Client, sid: str) -> list[dict]:
    r = client.get(f"/stations/{sid}/periods")
    assert r.status_code == 200, r.text
    return r.json()


def observations(client: httpx.Client, sid: str) -> list[dict]:
    r = client.get(f"/stations/{sid}/observations")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 基础可用性
# ---------------------------------------------------------------------------

def test_health_and_docs(client):
    assert client.get("/health").status_code == 200
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger" in r.text.lower()


def test_basic_attribution(client):
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z"},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2020-06-01T00:00:00Z", "value": 1.0},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2021-06-01T00:00:00Z", "value": 2.0},
    ])
    assert r.status_code == 201, r.text
    obs = observations(client, sid)
    assert [o["watershed"] for o in obs] == ["WS-A", "WS-A"]


# ---------------------------------------------------------------------------
# 提交时校验 + 整笔回滚
# ---------------------------------------------------------------------------

def test_overlap_rejected_and_whole_changeset_rolled_back(client):
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z"},
        {"op": "insert_period", "station_id": sid, "watershed": "WS-B",
         "valid_from": "2021-01-01T00:00:00Z", "valid_to": "2023-01-01T00:00:00Z"},
    ])
    assert r.status_code == 409, r.text
    assert "overlap" in r.json()["detail"]
    # 第一笔操作也必须回滚
    assert periods(client, sid) == []


def test_uncovered_observation_rejected(client):
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2022-06-01T00:00:00Z", "value": 1.0},
    ])
    assert r.status_code == 409, r.text
    assert "no covering attribution period" in r.json()["detail"]
    assert periods(client, sid) == []
    assert observations(client, sid) == []


def test_mid_transaction_violation_allowed_commit_satisfies(client):
    """事务中途观测无归属是允许的, 提交时满足即可."""
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z"},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2021-01-01T00:00:00Z", "value": 5.0},
    ])
    assert r.status_code == 201, r.text
    pid = periods(client, sid)[0]["period_id"]
    # 一笔事务: 先删时段(观测暂时无归属)再补回新流域时段
    r = changeset(client, [
        {"op": "delete_period", "period_id": pid},
        {"op": "insert_period", "station_id": sid, "watershed": "WS-B",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z"},
    ])
    assert r.status_code == 201, r.text
    assert observations(client, sid)[0]["watershed"] == "WS-B"


def test_boundary_semantics_left_closed_right_open(client):
    """左闭右开: 终点时刻的观测不属于该时段."""
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2021-01-01T00:00:00Z", "value": 1.0},
    ])
    assert r.status_code == 409, r.text  # 恰在终点, 无归属
    # 相邻两段 [a,b) [b,c) 不算重叠, 边界观测属右段
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
        {"op": "insert_period", "station_id": sid, "watershed": "WS-B",
         "valid_from": "2021-01-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z"},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2021-01-01T00:00:00Z", "value": 1.0},
    ])
    assert r.status_code == 201, r.text
    assert observations(client, sid)[0]["watershed"] == "WS-B"


# ---------------------------------------------------------------------------
# 便捷操作
# ---------------------------------------------------------------------------

def test_transfer(client):
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": None},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2020-06-01T00:00:00Z", "value": 1.0},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2021-06-01T00:00:00Z", "value": 2.0},
    ])
    assert r.status_code == 201, r.text
    r = client.post(f"/stations/{sid}/transfer",
                    json={"at": "2021-01-01T00:00:00Z", "new_watershed": "WS-B"})
    assert r.status_code == 201, r.text
    ps = periods(client, sid)
    assert len(ps) == 2
    assert ps[0]["valid_to"] == "2021-01-01T00:00:00+00:00"
    assert ps[1]["valid_to"] is None
    obs = observations(client, sid)
    assert [o["watershed"] for o in obs] == ["WS-A", "WS-B"]


def test_split_and_merge(client):
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2023-01-01T00:00:00Z"},
    ])
    assert r.status_code == 201, r.text
    pid = periods(client, sid)[0]["period_id"]
    r = client.post(f"/periods/{pid}/split",
                    json={"at": "2021-01-01T00:00:00Z", "right_watershed": "WS-B"})
    assert r.status_code == 201, r.text
    ps = periods(client, sid)
    assert [(p["watershed"]) for p in ps] == ["WS-A", "WS-B"]
    r = client.post(f"/stations/{sid}/merge", json={"at": "2021-01-01T00:00:00Z"})
    assert r.status_code == 201, r.text
    ps = periods(client, sid)
    assert len(ps) == 1 and ps[0]["valid_to"] == "2023-01-01T00:00:00+00:00"


def test_correct_historical(client):
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-OLD",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2023-01-01T00:00:00Z"},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2020-06-01T00:00:00Z", "value": 1.0},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2021-06-01T00:00:00Z", "value": 2.0},
        {"op": "insert_observation", "station_id": sid, "observed_at": "2022-06-01T00:00:00Z", "value": 3.0},
    ])
    assert r.status_code == 201, r.text
    r = client.post(f"/stations/{sid}/correct", json={
        "valid_from": "2021-01-01T00:00:00Z",
        "valid_to": "2022-01-01T00:00:00Z",
        "watershed": "WS-NEW",
    })
    assert r.status_code == 201, r.text
    assert len(periods(client, sid)) == 3
    obs = observations(client, sid)
    assert [o["watershed"] for o in obs] == ["WS-OLD", "WS-NEW", "WS-OLD"]


# ---------------------------------------------------------------------------
# 直连 SQL(独立 READ COMMITTED 连接)同样受约束
# ---------------------------------------------------------------------------

def test_direct_sql_commit_time_enforcement():
    sid = new_sid()
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO stations (station_id) VALUES (%s)", (sid,))

    # 事务中途违规、提交时满足 -> 成功
    with psycopg.connect(DSN) as c:
        c.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s, 'WS-A', '2020-01-01 00:00+00', '2022-01-01 00:00+00')", (sid,))
        c.execute(
            "INSERT INTO observations (station_id, observed_at, value)"
            " VALUES (%s, '2021-01-01 00:00+00', 7.7)", (sid,))
        c.execute("DELETE FROM attribution_periods WHERE station_id = %s", (sid,))
        c.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s, 'WS-B', '2020-01-01 00:00+00', '2022-01-01 00:00+00')", (sid,))
        c.commit()
    with psycopg.connect(DSN) as c:
        ws = c.execute(
            "SELECT watershed FROM observation_attribution WHERE station_id = %s", (sid,)
        ).fetchone()[0]
        assert ws == "WS-B"

    # 提交时仍违规 -> 整笔回滚
    with psycopg.connect(DSN) as c:
        c.execute("DELETE FROM attribution_periods WHERE station_id = %s", (sid,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.commit()
    with psycopg.connect(DSN) as c:
        n = c.execute(
            "SELECT count(*) FROM attribution_periods WHERE station_id = %s", (sid,)
        ).fetchone()[0]
        assert n == 1  # 时段仍在

    # 重叠时段 -> 拒绝
    with psycopg.connect(DSN) as c:
        c.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s, 'WS-C', '2021-06-01 00:00+00', '2023-01-01 00:00+00')", (sid,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.commit()


def test_read_committed_visibility(client):
    """供数只见已提交状态: 未提交事务的观测不可见."""
    sid = new_sid()
    mk_station(client, sid)
    holder = psycopg.connect(DSN)  # 默认 READ COMMITTED, 非 autocommit
    try:
        holder.execute(
            "INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)"
            " VALUES (%s, 'WS-A', '2020-01-01 00:00+00', '2022-01-01 00:00+00')", (sid,))
        holder.execute(
            "INSERT INTO observations (station_id, observed_at, value)"
            " VALUES (%s, '2021-01-01 00:00+00', 1.0)", (sid,))
        assert observations(client, sid) == []  # 未提交不可见
        holder.commit()
        assert len(observations(client, sid)) == 1
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# 并发: 撤段 vs 补录
# ---------------------------------------------------------------------------

def _delete_period_commit(sid: str, out: dict, key: str, hold_seconds: float = 0.0):
    try:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM attribution_periods WHERE station_id = %s", (sid,))
            if hold_seconds:
                c.execute("SELECT pg_sleep(%s)", (hold_seconds,))
            c.commit()
        out[key] = "ok"
    except Exception as exc:  # noqa: BLE001
        out[key] = f"error: {exc}"


def _insert_observation_commit(sid: str, out: dict, key: str):
    try:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(
                "INSERT INTO observations (station_id, observed_at, value)"
                " VALUES (%s, '2020-06-01 00:00+00', 42.0)", (sid,))
        out[key] = "ok"
    except Exception as exc:  # noqa: BLE001
        out[key] = f"error: {exc}"


def _db_counts(sid: str) -> tuple[int, int]:
    with psycopg.connect(DSN) as c:
        p = c.execute("SELECT count(*) FROM attribution_periods WHERE station_id = %s", (sid,)).fetchone()[0]
        o = c.execute("SELECT count(*) FROM observations WHERE station_id = %s", (sid,)).fetchone()[0]
        return p, o


def test_revoke_then_backfill_rejected_sequential(client):
    """撤段先提交 -> 补录在提交时被拒.

    与并发串行化后的终态等价(锁保证后者看到前者已提交状态),
    但不依赖跨进程锁等待唤醒, 任何环境可跑.
    """
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
    ])
    assert r.status_code == 201, r.text

    out: dict = {}
    _delete_period_commit(sid, out, "delete")
    assert out["delete"] == "ok", out          # 无观测, 撤段合法
    _insert_observation_commit(sid, out, "insert")
    assert out["insert"].startswith("error"), out
    assert "no covering attribution period" in out["insert"]
    assert _db_counts(sid) == (0, 0)


@pytest.mark.skipif(
    os.environ.get("PG_LOCK_WAIT_OK") != "1",
    reason="需要正常的锁等待/唤醒语义(设 PG_LOCK_WAIT_OK=1 启用); 某些沙箱阻断进程间信号, 锁等待不返回",
)
def test_concurrent_revoke_then_backfill_threaded(client):
    """真并发: 撤段持锁期间补录被阻塞, 撤段提交后补录在提交时被拒."""
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
    ])
    assert r.status_code == 201, r.text

    out: dict = {}
    t = threading.Thread(target=_delete_period_commit, args=(sid, out, "delete", 2.0))
    t.start()
    time.sleep(0.7)  # 确保删除方先持锁
    _insert_observation_commit(sid, out, "insert")
    t.join(timeout=30)

    assert out["delete"] == "ok", out
    assert out["insert"].startswith("error"), out
    assert "no covering attribution period" in out["insert"]
    assert _db_counts(sid) == (0, 0)


def test_concurrent_backfill_then_revoke_rejected(client):
    """补录先提交 -> 撤段在提交时被拒; 归属保持完好."""
    sid = new_sid()
    mk_station(client, sid)
    r = changeset(client, [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-A",
         "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
    ])
    assert r.status_code == 201, r.text

    out: dict = {}
    _insert_observation_commit(sid, out, "insert")
    assert out["insert"] == "ok", out
    _delete_period_commit(sid, out, "delete")
    assert out["delete"].startswith("error"), out
    assert "no covering attribution period" in out["delete"]
    assert _db_counts(sid) == (1, 1)


def test_unrelated_stations_both_commit(client):
    """无关站点互不阻塞: 两个并发变更集都能提交."""
    sid1, sid2 = new_sid(), new_sid()
    mk_station(client, sid1)
    mk_station(client, sid2)
    results: dict = {}

    def work(sid: str, key: str):
        c = httpx.Client(base_url=API, timeout=30)
        try:
            r = changeset(c, [
                {"op": "insert_period", "station_id": sid, "watershed": "WS-X",
                 "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2021-01-01T00:00:00Z"},
                {"op": "insert_observation", "station_id": sid,
                 "observed_at": "2020-06-01T00:00:00Z", "value": 1.0},
            ])
            results[key] = r.status_code
        finally:
            c.close()

    t1 = threading.Thread(target=work, args=(sid1, "s1"))
    t2 = threading.Thread(target=work, args=(sid2, "s2"))
    t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
    assert results == {"s1": 201, "s2": 201}
