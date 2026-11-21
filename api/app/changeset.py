"""变更集执行器: 在调用方提供的事务连接上按序执行操作."""
from __future__ import annotations

from fastapi import HTTPException

from .models import (
    ChangeSet,
    DeleteObservationOp,
    DeletePeriodOp,
    InsertObservationOp,
    InsertPeriodOp,
    UpdateObservationOp,
    UpdatePeriodOp,
    to_utc,
)


def _not_found(what: str, key: object) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} {key} not found")


def _insert_period(conn, op: InsertPeriodOp) -> dict:
    row = conn.execute(
        """
        INSERT INTO attribution_periods (station_id, watershed, valid_from, valid_to)
        VALUES (%s, %s, %s, %s)
        RETURNING period_id
        """,
        (op.station_id, op.watershed, to_utc(op.valid_from), to_utc(op.valid_to)),
    ).fetchone()
    return {"period_id": row["period_id"]}


def _update_period(conn, op: UpdatePeriodOp) -> dict:
    sets, params = [], []
    if op.station_id is not None:
        sets.append("station_id = %s")
        params.append(op.station_id)
    if op.watershed is not None:
        sets.append("watershed = %s")
        params.append(op.watershed)
    if op.valid_from is not None:
        sets.append("valid_from = %s")
        params.append(to_utc(op.valid_from))
    if op.valid_to is not None:
        sets.append("valid_to = %s")
        params.append(to_utc(op.valid_to))
    elif op.clear_valid_to:
        sets.append("valid_to = NULL")
    if not sets:
        raise HTTPException(status_code=422, detail="update_period: no fields to update")
    params.append(op.period_id)
    row = conn.execute(
        f"UPDATE attribution_periods SET {', '.join(sets)} WHERE period_id = %s RETURNING period_id",
        params,
    ).fetchone()
    if row is None:
        raise _not_found("period", op.period_id)
    return {"period_id": row["period_id"]}


def _delete_period(conn, op: DeletePeriodOp) -> dict:
    row = conn.execute(
        "DELETE FROM attribution_periods WHERE period_id = %s RETURNING period_id",
        (op.period_id,),
    ).fetchone()
    if row is None:
        raise _not_found("period", op.period_id)
    return {"period_id": row["period_id"]}


def _insert_observation(conn, op: InsertObservationOp) -> dict:
    row = conn.execute(
        """
        INSERT INTO observations (station_id, observed_at, value)
        VALUES (%s, %s, %s)
        RETURNING observation_id
        """,
        (op.station_id, to_utc(op.observed_at), op.value),
    ).fetchone()
    return {"observation_id": row["observation_id"]}


def _update_observation(conn, op: UpdateObservationOp) -> dict:
    sets, params = [], []
    if op.station_id is not None:
        sets.append("station_id = %s")
        params.append(op.station_id)
    if op.observed_at is not None:
        sets.append("observed_at = %s")
        params.append(to_utc(op.observed_at))
    if op.value is not None:
        sets.append("value = %s")
        params.append(op.value)
    if not sets:
        raise HTTPException(status_code=422, detail="update_observation: no fields to update")
    params.append(op.observation_id)
    row = conn.execute(
        f"UPDATE observations SET {', '.join(sets)} WHERE observation_id = %s RETURNING observation_id",
        params,
    ).fetchone()
    if row is None:
        raise _not_found("observation", op.observation_id)
    return {"observation_id": row["observation_id"]}


def _delete_observation(conn, op: DeleteObservationOp) -> dict:
    row = conn.execute(
        "DELETE FROM observations WHERE observation_id = %s RETURNING observation_id",
        (op.observation_id,),
    ).fetchone()
    if row is None:
        raise _not_found("observation", op.observation_id)
    return {"observation_id": row["observation_id"]}


_HANDLERS = {
    "insert_period": _insert_period,
    "update_period": _update_period,
    "delete_period": _delete_period,
    "insert_observation": _insert_observation,
    "update_observation": _update_observation,
    "delete_observation": _delete_observation,
}


def execute_changeset(conn, cs: ChangeSet) -> list[dict]:
    """在当前事务内按序执行; 任何一步失败由调用方回滚整笔."""
    results = []
    for i, op in enumerate(cs.operations):
        out = _HANDLERS[op.op](conn, op)
        results.append({"index": i, "op": op.op, **out})
    return results
