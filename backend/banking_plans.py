"""Persistent, narrowly scoped cross-scene banking plans.

The DAG template is server-owned: check funds -> reserve a budget -> remind
about a sandbox subscription. User text or model output never supplies code,
SQL, dependency edges, or business tool names.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend.banking import _amount_fen, _audit, _available, _connect, _money, _now, _sandbox_enabled, _session


router = APIRouter(prefix="/api/banking", tags=["banking-plans"], dependencies=[Depends(_sandbox_enabled)])


class PlanRequest(BaseModel):
    amount: str
    subscription_id: str
    run_at: datetime


class PlanConfirm(BaseModel):
    digest: str = Field(min_length=64, max_length=64)


def _nodes() -> list[dict[str, Any]]:
    return [
        {"id": "check_balance", "kind": "balance_check", "depends_on": [], "status": "PENDING", "output": None},
        {"id": "reserve_budget", "kind": "budget_reservation", "depends_on": ["check_balance"], "status": "PENDING", "output": None},
        {"id": "remind_subscription", "kind": "subscription_reminder", "depends_on": ["reserve_budget"], "status": "PENDING", "output": None},
    ]


def _view(row) -> dict[str, Any]:
    return {"id": row["id"], "title": row["title"], "status": row["status"],
            "run_at": row["run_at"], "amount": _money(row["amount_fen"]),
            "subscription_id": row["subscription_id"], "digest": row["digest"],
            "nodes": json.loads(row["nodes_json"]), "failure_reason": row["failure_reason"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "sandbox": True}


def _owned(db, user, plan_id: str):
    row = db.execute("SELECT * FROM sandbox_plans WHERE id=? AND session_id=?", (plan_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "计划不存在")
    return row


@router.post("/plans/prepare")
def prepare_plan(req: PlanRequest, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    amount = _amount_fen(req.amount)
    if req.run_at.tzinfo is None:
        raise HTTPException(422, "执行时间必须带时区")
    run_at = req.run_at.astimezone(timezone.utc)
    now = _now()
    if run_at < now - timedelta(minutes=5) or run_at > now + timedelta(days=7):
        raise HTTPException(422, "执行时间须在当前至七天内")
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        if user["locked_until"] and user["locked_until"] > now.isoformat():
            raise HTTPException(423, "账户暂时锁定")
        subscription = db.execute("SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=? AND status='ACTIVE'", (req.subscription_id, user["id"])).fetchone()
        if subscription is None:
            raise HTTPException(404, "未找到生效中的模拟订阅")
        if amount > _available(user):
            raise HTTPException(409, "计划金额高于当前可用余额")
        plan_id = secrets.token_hex(16)
        digest = hashlib.sha256(f"{user['id']}|{plan_id}|{amount}|{subscription['id']}|{run_at.isoformat()}".encode()).hexdigest()
        title = f"预留 ¥{_money(amount)} 并提醒 {subscription['merchant']} 续费"
        db.execute("INSERT INTO sandbox_plans(id,session_id,title,status,run_at,amount_fen,subscription_id,digest,nodes_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (plan_id, user["id"], title, "DRAFT", run_at.isoformat(), amount, subscription["id"], digest,
                    json.dumps(_nodes(), ensure_ascii=False), now.isoformat(), now.isoformat()))
        _audit(db, user["id"], plan_id, "PLAN_PREPARED", {"amount_fen": amount, "subscription_id": subscription["id"], "run_at": run_at.isoformat()})
        return _view(_owned(db, user, plan_id))


@router.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: str, req: PlanConfirm, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user, plan_id)
        if row["status"] != "DRAFT":
            raise HTTPException(409, "计划已经确认或失效")
        if not secrets.compare_digest(row["digest"], req.digest):
            raise HTTPException(409, "确认内容与计划预览不一致")
        now = _now().isoformat()
        db.execute("UPDATE sandbox_plans SET status='SCHEDULED',confirmed_at=?,updated_at=? WHERE id=?", (now, now, plan_id))
        _audit(db, user["id"], plan_id, "PLAN_CONFIRMED", {"digest": row["digest"]})
        return _view(_owned(db, user, plan_id))


@router.get("/plans")
def list_plans(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        rows = db.execute("SELECT * FROM sandbox_plans WHERE session_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        return {"items": [_view(row) for row in rows], "sandbox": True}


@router.post("/plans/{plan_id}/pause")
def pause_plan(plan_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user, plan_id)
        if row["status"] != "SCHEDULED":
            raise HTTPException(409, "只能暂停待执行计划")
        db.execute("UPDATE sandbox_plans SET status='PAUSED',updated_at=? WHERE id=?", (_now().isoformat(), plan_id))
        _audit(db, user["id"], plan_id, "PLAN_PAUSED", {})
        return _view(_owned(db, user, plan_id))


@router.post("/plans/{plan_id}/resume")
def resume_plan(plan_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user, plan_id)
        if row["status"] not in {"PAUSED", "NEEDS_ATTENTION"}:
            raise HTTPException(409, "当前计划不能恢复")
        db.execute("UPDATE sandbox_plans SET status='SCHEDULED',failure_reason=NULL,updated_at=? WHERE id=?", (_now().isoformat(), plan_id))
        _audit(db, user["id"], plan_id, "PLAN_RESUMED", {})
        return _view(_owned(db, user, plan_id))


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user, plan_id)
        if row["status"] not in {"DRAFT", "SCHEDULED", "PAUSED", "NEEDS_ATTENTION"}:
            raise HTTPException(409, "已执行计划不能取消；可释放已预留预算")
        db.execute("UPDATE sandbox_plans SET status='CANCELLED',updated_at=? WHERE id=?", (_now().isoformat(), plan_id))
        _audit(db, user["id"], plan_id, "PLAN_CANCELLED", {})
        return _view(_owned(db, user, plan_id))


@router.post("/plans/{plan_id}/release")
def release_budget(plan_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user, plan_id)
        if row["status"] != "COMPLETED" or row["released_at"]:
            raise HTTPException(409, "该计划没有可释放的预算")
        db.execute("UPDATE sandbox_sessions SET reserved_fen=reserved_fen-? WHERE id=? AND reserved_fen>=?", (row["amount_fen"], user["id"], row["amount_fen"]))
        if db.execute("SELECT changes() AS n").fetchone()["n"] != 1:
            raise HTTPException(409, "预算状态不一致")
        now = _now().isoformat()
        db.execute("UPDATE sandbox_plans SET status='RELEASED',released_at=?,updated_at=? WHERE id=?", (now, now, plan_id))
        _audit(db, user["id"], plan_id, "BUDGET_RELEASED", {"amount_fen": row["amount_fen"]})
        return _view(_owned(db, user, plan_id))


@router.get("/notifications")
def notifications(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        rows = db.execute("SELECT id,plan_id,message,created_at FROM sandbox_notifications WHERE session_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        return {"items": [dict(row) for row in rows], "sandbox": True}


def run_due_plans(session_id: str | None = None, now: datetime | None = None) -> int:
    """Execute each due confirmed plan once; safe across concurrent workers."""
    current = (now or _now()).astimezone(timezone.utc).isoformat()
    with _connect() as db:
        sql = "SELECT id FROM sandbox_plans WHERE status='SCHEDULED' AND run_at<=?"
        args: tuple[Any, ...] = (current,)
        if session_id:
            sql += " AND session_id=?"
            args += (session_id,)
        plan_ids = [row["id"] for row in db.execute(sql, args).fetchall()]
    completed = 0
    for plan_id in plan_ids:
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = db.execute("SELECT * FROM sandbox_plans WHERE id=? AND status='SCHEDULED' AND run_at<=?", (plan_id, current)).fetchone()
            if plan is None:
                continue
            sid = plan["session_id"]
            user = db.execute("SELECT * FROM sandbox_sessions WHERE id=?", (sid,)).fetchone()
            subscription = db.execute("SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=?", (plan["subscription_id"], sid)).fetchone()
            reason = None
            if not user or user["expires_at"] <= current:
                reason = "沙箱会话已过期，计划未执行"
            elif not subscription or subscription["status"] != "ACTIVE":
                reason = "订阅已取消或不可用，计划未执行"
            elif user["locked_until"] and user["locked_until"] > current:
                reason = "账户安全锁定，计划未执行"
            elif _available(user) < plan["amount_fen"]:
                reason = "可用余额不足，计划未执行"
            if reason:
                db.execute("UPDATE sandbox_plans SET status='NEEDS_ATTENTION',failure_reason=?,updated_at=? WHERE id=?", (reason, current, plan_id))
                _audit(db, sid, plan_id, "PLAN_NEEDS_ATTENTION", {"reason": reason})
                continue
            nodes = json.loads(plan["nodes_json"])
            nodes[0].update(status="COMPLETED", output={"available_before": _money(_available(user))})
            db.execute("UPDATE sandbox_sessions SET reserved_fen=reserved_fen+? WHERE id=?", (plan["amount_fen"], sid))
            nodes[1].update(status="COMPLETED", output={"reserved": _money(plan["amount_fen"])})
            message = f"预算 ¥{_money(plan['amount_fen'])} 已预留；{subscription['merchant']} 下次模拟续费为 {subscription['next_charge_at'][:10]}。"
            db.execute("INSERT INTO sandbox_notifications(id,session_id,plan_id,message,created_at) VALUES(?,?,?,?,?)", (secrets.token_hex(8), sid, plan_id, message, current))
            nodes[2].update(status="COMPLETED", output={"message": message})
            db.execute("UPDATE sandbox_plans SET status='COMPLETED',nodes_json=?,updated_at=? WHERE id=?", (json.dumps(nodes, ensure_ascii=False), current, plan_id))
            _audit(db, sid, plan_id, "PLAN_COMPLETED", {"nodes": [node["id"] for node in nodes], "amount_fen": plan["amount_fen"]})
            completed += 1
    return completed


@router.post("/plans/run-due")
def run_due_for_session(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    """Explicit sandbox demo trigger; production worker also calls run_due_plans."""
    with _connect() as db:
        user = _session(db, x_banking_session)
        sid = user["id"]
    return {"completed": run_due_plans(session_id=sid), "sandbox": True}


async def background_plan_worker() -> None:
    import asyncio
    import logging
    from backend.banking_missions import run_tick
    while True:
        await asyncio.sleep(2)
        if os.getenv("APP_ENV", "development").casefold() != "production" or os.getenv("BANKING_SANDBOX_ENABLED") == "1":
            try:
                await asyncio.to_thread(run_due_plans)
                await asyncio.to_thread(run_tick)
            except Exception:
                logging.getLogger(__name__).exception("Banking scheduler tick failed; durable checkpoints retained")
