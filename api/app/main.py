"""流域归属与观测服务 API.

所有写操作都在单事务中执行; 数据库在提交时统一校验:
  1. 同站归属时段不得重叠;
  2. 每条观测在采样时刻必须恰好落入本站一个归属时段.
违反即整笔回滚并返回 409.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import psycopg
from fastapi import FastAPI, HTTPException, Query

from .changeset import execute_changeset
from .db import close_pool, get_pool, init_pool
from .models import (
    ChangeSet,
    CorrectRequest,
    MergeRequest,
    ObservationIn,
    SplitRequest,
    StationIn,
    TransferRequest,
    to_utc,
)

DESCRIPTION = """维护站点-流域归属时段与站点观测。

* 时段左闭右开 `[valid_from, valid_to)`，终点可空（至今有效）
* 同站时段不得重叠；每条观测须恰有一段归属
* `/changesets` 可在一笔事务内拆分/合并/更正时段并补改观测，中途允许暂时违规，提交时统一校验
* 冲突（含并发冲突、死锁）整笔拒绝，返回 409
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


app = FastAPI(
    title="流域归属与观测服务",
    version="1.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
)

_INTEGRITY_ERRORS = (
    psycopg.errors.IntegrityError,        # 含提交时延迟约束触发器抛出的 23514
    psycopg.errors.RaiseException,        # P0001
    psycopg.errors.DeadlockDetected,      # 40P01
    psycopg.errors.SerializationFailure,  # 40001
)


def _to_409(exc: psycopg.Error) -> HTTPException:
    msg = exc.diag.message_primary if exc.diag and exc.diag.message_primary else str(exc).strip()
    return HTTPException(status_code=409, detail=msg)


def run_txn(fn):
    """在单事务中执行 fn(conn); 完整性/并发冲突 -> 409, 整笔回滚."""
    pool = get_pool()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                return fn(conn)
        except HTTPException:
            raise
        except _INTEGRITY_ERRORS as exc:
            raise _to_409(exc) from exc


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"service": "watershed-attribution", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 站点
# ---------------------------------------------------------------------------

@app.post("/stations", status_code=201, tags=["stations"])
def create_station(body: StationIn):
    def fn(conn):
        conn.execute(
            "INSERT INTO stations (station_id, name) VALUES (%s, %s)",
            (body.station_id, body.name),
        )
        return {"station_id": body.station_id, "name": body.name}

    return run_txn(fn)


@app.get("/stations", tags=["stations"])
def list_stations():
    pool = get_pool()
    with pool.connection() as conn:
        return conn.execute(
            "SELECT station_id, name, created_at FROM stations ORDER BY station_id"
        ).fetchall()


@app.get("/stations/{station_id}/periods", tags=["stations"])
def list_periods(station_id: str):
    pool = get_pool()
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT period_id, station_id, watershed, valid_from, valid_to
            FROM attribution_periods
            WHERE station_id = %s
            ORDER BY valid_from
            """,
            (station_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# 变更集(核心写入口)
# ---------------------------------------------------------------------------

@app.post("/changesets", status_code=201, tags=["write"])
def submit_changeset(cs: ChangeSet):
    """一笔事务执行全部操作; 提交时校验不通过则整笔回滚并返回 409."""

    def fn(conn):
        return {"committed": True, "results": execute_changeset(conn, cs)}

    return run_txn(fn)


@app.post("/observations", status_code=201, tags=["write"])
def create_observation(body: ObservationIn):
    """单条补录(等价于只含一个 insert_observation 的变更集)."""

    def fn(conn):
        row = conn.execute(
            """
            INSERT INTO observations (station_id, observed_at, value)
            VALUES (%s, %s, %s)
            RETURNING observation_id
            """,
            (body.station_id, to_utc(body.observed_at), body.value),
        ).fetchone()
        return {"observation_id": row["observation_id"]}

    return run_txn(fn)


# ---------------------------------------------------------------------------
# 便捷操作: 转属 / 拆分 / 合并 / 历史更正
# ---------------------------------------------------------------------------

@app.post("/stations/{station_id}/transfer", status_code=201, tags=["write"])
def transfer(station_id: str, body: TransferRequest):
    """转属: at 时刻截断当前归属时段, 同时开启归属 new_watershed 的新时段."""
    at = to_utc(body.at)

    def fn(conn):
        row = conn.execute(
            """
            SELECT period_id, watershed, valid_from, valid_to
            FROM attribution_periods
            WHERE station_id = %s AND tstzrange(valid_from, valid_to, '[)') @> %s
            FOR UPDATE
            """,
            (station_id, at),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"station {station_id}: no attribution period covers {at.isoformat()}",
            )
        if row["watershed"] == body.new_watershed:
            raise HTTPException(status_code=422, detail="new_watershed equals current watershed")
        if not at > row["valid_from"]:
            raise HTTPException(
                status_code=422,
                detail="transfer time must be later than the covering period start",
            )
        conn.execute(
            "UPDATE attribution_periods SET valid_to = %s WHERE period_id = %s",
            (at, row["period_id"]),
        )
        new_id = conn.execute(
            """
            INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
            VALUES (%s, %s, %s, %s)
            RETURNING period_id
            """,
            (station_id, body.new_watershed, at, row["valid_to"]),
        ).fetchone()["period_id"]
        return {
            "station_id": station_id,
            "closed_period_id": row["period_id"],
            "new_period_id": new_id,
            "watershed": body.new_watershed,
            "valid_from": at,
            "valid_to": row["valid_to"],
        }

    return run_txn(fn)


@app.post("/periods/{period_id}/split", status_code=201, tags=["write"])
def split_period(period_id: int, body: SplitRequest):
    """拆分: 把时段在 at 处一分为二; 右半段可指定新流域."""
    at = to_utc(body.at)

    def fn(conn):
        row = conn.execute(
            """
            SELECT period_id, station_id, watershed, valid_from, valid_to
            FROM attribution_periods
            WHERE period_id = %s
            FOR UPDATE
            """,
            (period_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"period {period_id} not found")
        if not at > row["valid_from"]:
            raise HTTPException(status_code=422, detail="split point must be after valid_from")
        if row["valid_to"] is not None and not at < row["valid_to"]:
            raise HTTPException(status_code=422, detail="split point must be before valid_to")
        right_ws = body.right_watershed or row["watershed"]
        conn.execute(
            "UPDATE attribution_periods SET valid_to = %s WHERE period_id = %s",
            (at, period_id),
        )
        new_id = conn.execute(
            """
            INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
            VALUES (%s, %s, %s, %s)
            RETURNING period_id
            """,
            (row["station_id"], right_ws, at, row["valid_to"]),
        ).fetchone()["period_id"]
        return {
            "left_period_id": period_id,
            "right_period_id": new_id,
            "right_watershed": right_ws,
            "split_at": at,
        }

    return run_txn(fn)


@app.post("/stations/{station_id}/merge", status_code=201, tags=["write"])
def merge_periods(station_id: str, body: MergeRequest):
    """合并: 把在 at 处首尾相接(左段 valid_to == at == 右段 valid_from)的两段并为一段."""
    at = to_utc(body.at)

    def fn(conn):
        left = conn.execute(
            """
            SELECT period_id, watershed, valid_from, valid_to
            FROM attribution_periods
            WHERE station_id = %s AND valid_to = %s
            FOR UPDATE
            """,
            (station_id, at),
        ).fetchone()
        right = conn.execute(
            """
            SELECT period_id, watershed, valid_from, valid_to
            FROM attribution_periods
            WHERE station_id = %s AND valid_from = %s
            FOR UPDATE
            """,
            (station_id, at),
        ).fetchone()
        if left is None or right is None:
            raise HTTPException(
                status_code=404,
                detail=f"station {station_id}: no adjacent period pair at {at.isoformat()}",
            )
        merged_ws = body.watershed or left["watershed"]
        conn.execute(
            "DELETE FROM attribution_periods WHERE period_id = %s",
            (right["period_id"],),
        )
        conn.execute(
            "UPDATE attribution_periods SET valid_to = %s, watershed = %s WHERE period_id = %s",
            (right["valid_to"], merged_ws, left["period_id"]),
        )
        return {
            "merged_period_id": left["period_id"],
            "removed_period_id": right["period_id"],
            "watershed": merged_ws,
            "valid_from": left["valid_from"],
            "valid_to": right["valid_to"],
        }

    return run_txn(fn)


@app.post("/stations/{station_id}/correct", status_code=201, tags=["write"])
def correct_period(station_id: str, body: CorrectRequest):
    """历史更正: 把 [valid_from, valid_to) 子区间改属另一流域.

    要求该子区间完整落在某一个现有时段内; 原时段被切分为最多三段,
    观测归属随采样时刻自动重排. 跨多段的更正请用 /changesets.
    """
    cfrom, cto = to_utc(body.valid_from), to_utc(body.valid_to)
    if not cfrom < cto:
        raise HTTPException(status_code=422, detail="valid_from must be before valid_to")

    def fn(conn):
        row = conn.execute(
            """
            SELECT period_id, watershed, valid_from, valid_to
            FROM attribution_periods
            WHERE station_id = %s
              AND valid_from <= %s
              AND (valid_to IS NULL OR %s <= valid_to)
            ORDER BY valid_from
            LIMIT 1
            FOR UPDATE
            """,
            (station_id, cfrom, cto),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"station {station_id}: no single period covers "
                       f"[{cfrom.isoformat()}, {cto.isoformat()}); use /changesets instead",
            )
        conn.execute(
            "DELETE FROM attribution_periods WHERE period_id = %s",
            (row["period_id"],),
        )
        segments = []
        if cfrom > row["valid_from"]:
            segments.append((row["watershed"], row["valid_from"], cfrom))
        segments.append((body.watershed, cfrom, cto))
        if row["valid_to"] is None or cto < row["valid_to"]:
            segments.append((row["watershed"], cto, row["valid_to"]))
        new_ids = []
        for ws, seg_from, seg_to in segments:
            new_ids.append(
                conn.execute(
                    """
                    INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
                    VALUES (%s, %s, %s, %s)
                    RETURNING period_id
                    """,
                    (station_id, ws, seg_from, seg_to),
                ).fetchone()["period_id"]
            )
        return {
            "replaced_period_id": row["period_id"],
            "new_period_ids": new_ids,
            "corrected_watershed": body.watershed,
            "valid_from": cfrom,
            "valid_to": cto,
        }

    return run_txn(fn)


# ---------------------------------------------------------------------------
# 供数: 按采样时刻关联流域(只见已提交状态)
# ---------------------------------------------------------------------------

@app.get("/stations/{station_id}/observations", tags=["feed"])
def list_attributed_observations(
    station_id: str,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
):
    sql = """
        SELECT observation_id, station_id, observed_at, value, watershed, period_id
        FROM observation_attribution
        WHERE station_id = %s
    """
    params: list = [station_id]
    if from_ is not None:
        sql += " AND observed_at >= %s"
        params.append(from_)
    if to is not None:
        sql += " AND observed_at < %s"
        params.append(to)
    sql += " ORDER BY observed_at"
    pool = get_pool()
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/attributed-observations", tags=["feed"])
def attributed_feed(
    station_id: Optional[str] = None,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
):
    sql = """
        SELECT observation_id, station_id, observed_at, value, watershed, period_id
        FROM observation_attribution
        WHERE TRUE
    """
    params: list = []
    if station_id is not None:
        sql += " AND station_id = %s"
        params.append(station_id)
    if from_ is not None:
        sql += " AND observed_at >= %s"
        params.append(from_)
    if to is not None:
        sql += " AND observed_at < %s"
        params.append(to)
    sql += " ORDER BY station_id, observed_at"
    pool = get_pool()
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()
