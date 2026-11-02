"""Synthetic wealth products with server-side suitability and red-level ordering."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend.banking import _amount_fen, _audit, _available, _connect, _money, _now, _sandbox_enabled, _session


router = APIRouter(prefix="/api/banking/wealth", tags=["banking-wealth"], dependencies=[Depends(_sandbox_enabled)])

PRODUCTS = (
    {"id": "cash", "name": "星河现金管理（模拟）", "risk_level": 1, "type": "现金管理", "description": "模拟低波动产品；不展示预期收益，不代表真实产品。"},
    {"id": "bond", "name": "星河稳健债券（模拟）", "risk_level": 2, "type": "债券组合", "description": "模拟中等风险产品；本金可能波动。"},
    {"id": "index", "name": "星河成长指数（模拟）", "risk_level": 3, "type": "指数投资", "description": "模拟较高风险产品；本金可能损失。"},
)


class ProfileAnswers(BaseModel):
    experience: int = Field(ge=0, le=2)
    loss_tolerance: int = Field(ge=0, le=2)
    horizon: int = Field(ge=0, le=2)


class WealthPrepare(BaseModel):
    product_id: str
    amount: str


class WealthConfirm(BaseModel):
    digest: str = Field(min_length=64, max_length=64)


class WealthVerify(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class WealthExecute(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


def _product(product_id: str) -> dict[str, Any]:
    product = next((item for item in PRODUCTS if item["id"] == product_id), None)
    if product is None:
        raise HTTPException(404, "模拟产品不存在")
    return product


def _profile(db, sid: str) -> int | None:
    row = db.execute("SELECT risk_level FROM sandbox_wealth_profiles WHERE session_id=?", (sid,)).fetchone()
    return int(row["risk_level"]) if row else None


def _profile_revision(db,sid):
    row=db.execute('SELECT risk_level,answers_json,updated_at,revision_id FROM sandbox_wealth_profiles WHERE session_id=?',(sid,)).fetchone()
    return hashlib.sha256('|'.join(str(v) for v in row).encode()).hexdigest() if row else None


def _owned(db, sid: str, order_id: str):
    row = db.execute("SELECT * FROM sandbox_wealth_orders WHERE id=? AND session_id=?", (order_id, sid)).fetchone()
    if row is None:
        raise HTTPException(404, "申购预览不存在")
    return row


def _view(row) -> dict[str, Any]:
    from backend.banking_auth import mode
    return {"id": row["id"], "product": _product(row["product_id"]), "amount": _money(row["amount_fen"]),
            "action": row["action"], "status": row["status"], "level": "red", "digest": row["digest"],
            "expires_at": row["expires_at"], "demo_code": row["demo_code"] if row["status"] == "AWAITING_VERIFICATION" and mode() == "demo" else None,
            "receipt": row["receipt"] if row["status"] == "COMMITTED" else None, "sandbox": True}


@router.get("/products")
def products(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        level = _profile(db, user["id"])
        return {"items": [{**item, "suitable": level is not None and item["risk_level"] <= level} for item in PRODUCTS],
                "risk_level": level, "sandbox": True}


@router.post("/profile")
def assess_profile(answers: ProfileAnswers, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    level = 1 + min(answers.experience, answers.loss_tolerance, answers.horizon)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        db.execute("INSERT OR REPLACE INTO sandbox_wealth_profiles(session_id,risk_level,answers_json,updated_at,revision_id) VALUES(?,?,?,?,?)",
                   (user["id"], level, answers.model_dump_json(), _now().isoformat(),secrets.token_hex(16)))
        _audit(db, user["id"], None, "SUITABILITY_ASSESSED", {"risk_level": level})
        return {"risk_level": level, "label": {1: "谨慎型", 2: "稳健型", 3: "进取型"}[level], "sandbox": True}


@router.get("/holdings")
def holdings(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        rows = db.execute("SELECT product_id,principal_fen,updated_at FROM sandbox_wealth_holdings WHERE session_id=? AND principal_fen>0 ORDER BY updated_at DESC", (user["id"],)).fetchall()
        return {"items": [{"product": _product(row["product_id"]), "principal": _money(row["principal_fen"]),
                           "updated_at": row["updated_at"]} for row in rows], "sandbox": True}


@router.post("/orders/prepare")
def prepare_order(req: WealthPrepare, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    product = _product(req.product_id)
    amount = _amount_fen(req.amount)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        level = _profile(db, user["id"])
        if level is None:
            raise HTTPException(409, "请先完成模拟风险测评")
        if product["risk_level"] > level:
            raise HTTPException(403, "该产品高于你的模拟风险承受能力")
        if _available(user) < amount:
            raise HTTPException(409, "演示账户可用余额不足")
        order_id = secrets.token_hex(16)
        expires = _now() + timedelta(minutes=5)
        revision=_profile_revision(db,user['id'])
        digest = hashlib.sha256(f"{user['id']}|{order_id}|BUY|{product['id']}|{amount}|{level}|{revision}|{expires.isoformat()}".encode()).hexdigest()
        db.execute("INSERT INTO sandbox_wealth_orders(id,session_id,product_id,amount_fen,action,status,digest,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (order_id, user["id"], product["id"], amount, "BUY", "PREPARED", digest, _now().isoformat(), expires.isoformat()))
        _audit(db, user["id"], order_id, "WEALTH_PREPARED", {"product_id": product["id"], "amount_fen": amount, "risk_level": level})
        db.execute('UPDATE sandbox_wealth_orders SET profile_revision=? WHERE id=?',(revision,order_id))
        return _view(_owned(db, user["id"], order_id))


@router.post("/redemptions/prepare")
def prepare_redemption(req: WealthPrepare, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    product = _product(req.product_id)
    amount = _amount_fen(req.amount)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        holding = db.execute("SELECT principal_fen FROM sandbox_wealth_holdings WHERE session_id=? AND product_id=?", (user["id"], product["id"])).fetchone()
        if not holding or holding["principal_fen"] < amount:
            raise HTTPException(409, "模拟持仓本金不足")
        order_id = secrets.token_hex(16)
        expires = _now() + timedelta(minutes=5)
        digest = hashlib.sha256(f"{user['id']}|{order_id}|REDEEM|{product['id']}|{amount}|{expires.isoformat()}".encode()).hexdigest()
        db.execute("INSERT INTO sandbox_wealth_orders(id,session_id,product_id,amount_fen,action,status,digest,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (order_id, user["id"], product["id"], amount, "REDEEM", "PREPARED", digest, _now().isoformat(), expires.isoformat()))
        _audit(db, user["id"], order_id, "WEALTH_REDEMPTION_PREPARED", {"product_id": product["id"], "amount_fen": amount})
        return _view(_owned(db, user["id"], order_id))


@router.post("/orders/{order_id}/confirm")
def confirm_order(order_id: str, req: WealthConfirm, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user["id"], order_id)
        if row["status"] != "PREPARED" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "申购预览已失效")
        if not secrets.compare_digest(req.digest, row["digest"]):
            _audit(db, user["id"], order_id, "WEALTH_CONFIRM_REJECTED", {"reason": "digest_mismatch"})
            db.commit()
            raise HTTPException(409, "确认内容与预览不一致")
        code = f"{secrets.randbelow(1_000_000):06d}"
        db.execute("UPDATE sandbox_wealth_orders SET status='AWAITING_VERIFICATION',confirmed_at=?,otp_hash=?,demo_code=? WHERE id=?",
                   (_now().isoformat(), hashlib.sha256(code.encode()).hexdigest(), code, order_id))
        _audit(db, user["id"], order_id, "WEALTH_CONFIRMED", {"next_status": "AWAITING_VERIFICATION"})
        return _view(_owned(db, user["id"], order_id))


@router.post("/orders/{order_id}/verify")
def verify_order(order_id: str, req: WealthVerify, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    from backend.banking_auth import require_demo
    require_demo()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user["id"], order_id)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        if row["status"] != "AWAITING_VERIFICATION" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "当前申购不可验证")
        if not secrets.compare_digest(hashlib.sha256(req.code.encode()).hexdigest(), row["otp_hash"]):
            attempts = row["attempts"] + 1
            db.execute("UPDATE sandbox_wealth_orders SET attempts=? WHERE id=?", (attempts, order_id))
            if attempts >= 3:
                db.execute("UPDATE sandbox_sessions SET locked_until=? WHERE id=?", ((_now() + timedelta(minutes=10)).isoformat(), user["id"]))
            _audit(db, user["id"], order_id, "WEALTH_VERIFICATION_FAILED", {"attempts": attempts, "locked": attempts >= 3})
            return {"verified": False, "attempts_remaining": max(0, 3 - attempts), "locked": attempts >= 3}
        db.execute("UPDATE sandbox_wealth_orders SET status='READY',verified_at=?,otp_hash=NULL,demo_code=NULL WHERE id=?", (_now().isoformat(), order_id))
        _audit(db, user["id"], order_id, "WEALTH_VERIFIED", {"method": "demo_otp"})
        return {"verified": True, "order": _view(_owned(db, user["id"], order_id))}


@router.post("/orders/{order_id}/execute")
def execute_order(order_id: str, req: WealthExecute, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned(db, user["id"], order_id)
        if row["status"] == "COMMITTED" and row["idempotency_key"] == req.idempotency_key:
            return _view(row)
        if row["status"] != "READY" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "申购尚未完成确认、验证或已过期")
        from backend.banking_auth import require_grant
        require_grant(db, user["id"], "wealth", row)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        if row["action"] == "BUY":
            level = _profile(db, user["id"])
            if level is None or _product(row["product_id"])["risk_level"] > level:
                raise HTTPException(403, "风险测评已变化，请重新评估")
            if not row['profile_revision'] or row['profile_revision']!=_profile_revision(db,user['id']):
                raise HTTPException(409,'风险测评版本已变化，请重新预览并授权申购')
            if _available(user) < row["amount_fen"]:
                raise HTTPException(409, "演示账户可用余额不足")
        else:
            holding = db.execute("SELECT principal_fen FROM sandbox_wealth_holdings WHERE session_id=? AND product_id=?", (user["id"], row["product_id"])).fetchone()
            if not holding or holding["principal_fen"] < row["amount_fen"]:
                raise HTTPException(409, "模拟持仓本金不足，请重新预览赎回")
        collision = db.execute("SELECT id FROM sandbox_wealth_orders WHERE session_id=? AND idempotency_key=?", (user["id"], req.idempotency_key)).fetchone()
        if collision:
            raise HTTPException(409, "幂等键已用于另一笔申购")
        receipt = ("SBX-W-" if row["action"] == "BUY" else "SBX-WR-") + secrets.token_hex(8).upper()
        now = _now().isoformat()
        if row["action"] == "BUY":
            db.execute("UPDATE sandbox_sessions SET balance_fen=balance_fen-? WHERE id=?", (row["amount_fen"], user["id"]))
            db.execute("INSERT INTO sandbox_wealth_holdings(session_id,product_id,principal_fen,updated_at) VALUES(?,?,?,?) ON CONFLICT(session_id,product_id) DO UPDATE SET principal_fen=principal_fen+excluded.principal_fen,updated_at=excluded.updated_at",
                       (user["id"], row["product_id"], row["amount_fen"], now))
        else:
            db.execute("UPDATE sandbox_wealth_holdings SET principal_fen=principal_fen-?,updated_at=? WHERE session_id=? AND product_id=? AND principal_fen>=?",
                       (row["amount_fen"], now, user["id"], row["product_id"], row["amount_fen"]))
            if db.execute("SELECT changes() AS n").fetchone()["n"] != 1:
                raise HTTPException(409, "模拟持仓状态已变化")
            db.execute("UPDATE sandbox_sessions SET balance_fen=balance_fen+? WHERE id=?", (row["amount_fen"], user["id"]))
        db.execute("UPDATE sandbox_wealth_orders SET status='COMMITTED',committed_at=?,receipt=?,idempotency_key=? WHERE id=?", (now, receipt, req.idempotency_key, order_id))
        _audit(db, user["id"], order_id, "WEALTH_COMMITTED" if row["action"] == "BUY" else "WEALTH_REDEEMED", {"product_id": row["product_id"], "amount_fen": row["amount_fen"], "receipt": receipt})
        return _view(_owned(db, user["id"], order_id))
