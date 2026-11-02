"""Sandbox-only Plaid adapter. External reads never credit the local ledger."""
from __future__ import annotations

import json
import os
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Header, HTTPException
from pydantic import BaseModel

from backend import banking as bank

BASE = "https://sandbox.plaid.com"
ALLOWED = {"/sandbox/public_token/create", "/item/public_token/exchange", "/accounts/get", "/transactions/sync", "/item/remove"}


def configured():
    return bool(os.getenv("PLAID_CLIENT_ID") and os.getenv("PLAID_SECRET") and os.getenv("PLAID_TOKEN_ENCRYPTION_KEY"))


def cipher():
    try:
        return Fernet(os.environ["PLAID_TOKEN_ENCRYPTION_KEY"].encode())
    except (KeyError, ValueError):
        raise HTTPException(503, "请在服务端配置有效的 Plaid 沙箱密钥与令牌加密密钥") from None


def plaid_call(path: str, body: dict):
    if os.getenv("PLAID_ENV", "sandbox") != "sandbox" or path not in ALLOWED:
        raise HTTPException(503, "此适配器仅允许 Plaid Sandbox 与指定只读测试接口")
    if not configured():
        raise HTTPException(503, "Plaid 尚未配置：请在本机 .env 填写沙箱凭据与加密密钥")
    cipher()  # Reject invalid encryption config before creating any remote Item.
    try:
        with httpx.Client(base_url=BASE, timeout=15, follow_redirects=False) as client:
            response = client.post(path, json={**body, "client_id": os.environ["PLAID_CLIENT_ID"], "secret": os.environ["PLAID_SECRET"]}, headers={"Plaid-Version": "2020-09-14"})
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        if response.is_error:
            if response.status_code==429:
                raise HTTPException(503,'Plaid 沙箱请求过于频繁，请稍后重试；最近同步记录保持不变')
            code = payload.get("error_code", "")
            if code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise PaginationMutation()
            if code == "ITEM_LOGIN_REQUIRED":
                raise HTTPException(409, "测试账户需要重新授权，请断开后重新建立沙箱连接")
            if code in ("INVALID_API_KEYS", "INVALID_ACCESS_TOKEN"):
                raise HTTPException(503, "Plaid 凭据或测试令牌无效，请检查服务端配置")
            raise HTTPException(502, "Plaid 沙箱拒绝请求；本地账本未改变")
        return payload
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Plaid 沙箱暂不可用；最近同步记录保持不变") from None


class PaginationMutation(Exception):
    pass


class Consent(BaseModel):
    confirmed: bool


def amount(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal("1e15"):
            raise ValueError()
        return str(number)
    except (InvalidOperation, ValueError):
        raise HTTPException(502, "外部金额格式异常，未写入同步结果") from None


def accounts_view(rows):
    if not isinstance(rows, list) or len(rows) > 100:
        raise HTTPException(502, "外部账户列表格式异常")
    result = []; identifiers=set()
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('account_id'),str) or not row['account_id'] or len(row['account_id'])>200 or row['account_id'] in identifiers or not isinstance(row.get('balances'),dict):
            raise HTTPException(502,'外部账户标识缺失、重复或余额格式异常，未更新账户快照')
        identifiers.add(row['account_id'])
        balances = row["balances"]
        result.append({"id": str(row["account_id"]), "name": str(row["name"])[:150], "mask": str(row.get("mask") or "")[-4:],
                       "type": row.get("type"), "currency": balances.get("iso_currency_code") or balances.get("unofficial_currency_code") or "UNKNOWN",
                       "current": amount(balances.get("current")), "available": amount(balances.get("available"))})
    return result


def transaction_view(row, account_ids):
    if not isinstance(row,dict) or not isinstance(row.get('pending'),bool):
        raise HTTPException(502,'外部交易待入账状态格式异常，未写入同步结果')
    if row["account_id"] not in account_ids or not isinstance(row["transaction_id"], str) or len(row["transaction_id"]) > 200:
        raise HTTPException(502, "外部交易引用了未授权账户，未写入同步结果")
    if row["amount"] is None:
        raise HTTPException(502, "外部交易缺少金额，未写入同步结果")
    category = row.get("personal_finance_category") or {}
    return {"id": row["transaction_id"], "account_id": row["account_id"], "name": str(row.get("merchant_name") or row["name"])[:200],
            "amount": amount(row["amount"]), "currency": row.get("iso_currency_code") or row.get("unofficial_currency_code") or "UNKNOWN",
            "date": date.fromisoformat(row["date"]).isoformat(), "pending": bool(row["pending"]),
            "pending_transaction_id": row.get("pending_transaction_id"), "category": str(category.get("primary") or "UNCLASSIFIED")[:100],
            "source": "plaid_sandbox", "amount_convention": "positive_outflow"}


def view(db, sid):
    row = db.execute("SELECT * FROM sandbox_plaid_connections WHERE session_id=?", (sid,)).fetchone()
    # The payload is ciphertext on disk; JSON functions must not inspect it in SQL.
    records = db.execute("SELECT id,payload FROM sandbox_plaid_transactions WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    transactions = [json.loads(r['payload']) for r in records]
    transactions.sort(key=lambda value: value['date'], reverse=True)
    total = db.execute("SELECT COUNT(*) FROM sandbox_plaid_transactions WHERE session_id=?", (sid,)).fetchone()[0]
    return {"provider": "Plaid", "environment": "sandbox", "configured": configured(), "status": row["status"] if row else "DISCONNECTED",
            "last_updated_at": row["updated_at"] if row else None, "last_error": row["last_error"] if row else None,
            "accounts": json.loads(row["accounts_json"]) if row else [], "transactions": transactions[:100], "transaction_count": total,
            "read_only": True, "data_kind": "third_party_sandbox", "scope": "第三方模拟数据独立展示，不参与本地可用余额、转账或理财操作。"}


@bank.router.get("/external/plaid")
def status(x_banking_session: str | None = Header(default=None)):
    with bank._connect() as db:
        return view(db, bank._session(db, x_banking_session)["id"])


def begin_operation(token, operation):
    with bank._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        sid = bank._session(db, token)["id"]
        row = db.execute("SELECT * FROM sandbox_plaid_connections WHERE session_id=?", (sid,)).fetchone()
        if not row or not row["token_cipher"]:
            raise HTTPException(409, "请先连接 Plaid 沙箱账户")
        if row["status"] in ("SYNCING", "DISCONNECTING") and row["updated_at"] > (bank._now() - timedelta(minutes=3)).isoformat():
            raise HTTPException(409, "该连接正在处理另一项请求，请稍后重试")
        generation = row["generation"] + 1
        db.execute("UPDATE sandbox_plaid_connections SET status=?,generation=?,updated_at=? WHERE session_id=?", (operation, generation, bank._now().isoformat(), sid))
        return sid, dict(row), generation


def fail_operation(sid, generation, error, connection_id):
    with bank._connect() as db:
        db.execute("UPDATE sandbox_plaid_connections SET status='ERROR',last_error=? WHERE session_id=? AND generation=? AND id=?", (str(error.detail) if isinstance(error, HTTPException) else "外部响应格式异常，最近同步结果保持不变", sid, generation, connection_id))


@bank.router.post("/external/plaid/connect")
def connect(req: Consent, x_banking_session: str | None = Header(default=None)):
    if not req.confirmed:
        raise HTTPException(422, "请明确确认建立第三方测试账户连接")
    if not configured():
        raise HTTPException(503, "Plaid 尚未配置；无需在页面输入密钥，请由开发者配置本机 .env")
    cipher()
    if os.getenv("PLAID_ENV", "sandbox") != "sandbox":
        raise HTTPException(503, "本项目仅开放 Plaid 沙箱连接")
    with bank._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        sid = bank._session(db, x_banking_session)["id"]
        row = db.execute("SELECT * FROM sandbox_plaid_connections WHERE session_id=?", (sid,)).fetchone()
        if row and row["token_cipher"]:
            return view(db, sid)
        if row and row["status"] == "CONNECTING" and row["updated_at"] > (bank._now() - timedelta(minutes=3)).isoformat():
            raise HTTPException(409, "连接正在建立，请稍后刷新")
        operation_id = secrets.token_hex(16)
        db.execute("INSERT INTO sandbox_plaid_connections(session_id,id,status,updated_at,consent_at) VALUES(?,?,'CONNECTING',?,?) ON CONFLICT(session_id) DO UPDATE SET id=excluded.id,status='CONNECTING',updated_at=excluded.updated_at,consent_at=excluded.consent_at", (sid, operation_id, bank._now().isoformat(), bank._now().isoformat()))
    access_token = None
    try:
        public = plaid_call("/sandbox/public_token/create", {"institution_id": "ins_109508", "initial_products": ["transactions"]})
        exchanged = plaid_call("/item/public_token/exchange", {"public_token": public["public_token"]})
        access_token = exchanged["access_token"]
        accounts = accounts_view(plaid_call("/accounts/get", {"access_token": access_token})["accounts"])
        encrypted = cipher().encrypt(access_token.encode()).decode()
        with bank._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE sandbox_plaid_connections SET token_cipher=?,accounts_json=?,status='CONNECTED',last_error=NULL,updated_at=?,cursor='' WHERE session_id=? AND id=?", (encrypted, json.dumps(accounts), bank._now().isoformat(), sid, operation_id)).rowcount
            if not changed:
                raise HTTPException(409, "连接状态已变化，请重新读取")
            bank._audit(db, sid, None, "PLAID_SANDBOX_CONNECTED", {"accounts": len(accounts), "read_only": True})
            return view(db, sid)
    except (HTTPException, KeyError, TypeError, ValueError) as error:
        if access_token:
            try: plaid_call("/item/remove", {"access_token": access_token})
            except HTTPException: pass
        with bank._connect() as db:
            db.execute("UPDATE sandbox_plaid_connections SET status='ERROR',last_error='测试连接未建立，请核对配置或稍后重试' WHERE session_id=? AND id=?", (sid, operation_id))
        if isinstance(error, HTTPException): raise
        raise HTTPException(502, "Plaid 响应格式异常，测试连接未建立") from None


@bank.router.post("/external/plaid/sync")
def sync(x_banking_session: str | None = Header(default=None)):
    sid, row, generation = begin_operation(x_banking_session, "SYNCING")
    try:
        token = cipher().decrypt(row["token_cipher"].encode()).decode()
        account_rows = accounts_view(plaid_call("/accounts/get", {"access_token": token})["accounts"])
        account_ids = {a["id"] for a in account_rows}
        original_cursor = row["cursor"]
        for attempt in range(3):
            cursor, mutations = original_cursor, []
            try:
                for _ in range(50):
                    page = plaid_call("/transactions/sync", {"access_token": token, "cursor": cursor, "count": 500})
                    for kind in ("added", "modified"):
                        mutations.extend(("put", transaction_view(tx, account_ids)) for tx in page[kind])
                    mutations.extend(("remove", {"id": tx["transaction_id"]}) for tx in page["removed"])
                    next_cursor = page["next_cursor"]
                    if not isinstance(next_cursor, str) or not isinstance(page["has_more"], bool) or (page["has_more"] and next_cursor == cursor):
                        raise ValueError("pagination made no progress")
                    cursor = next_cursor
                    if not page["has_more"]:
                        break
                else:
                    raise HTTPException(502, "本轮同步超过页数上限，未保存不完整结果")
                break
            except PaginationMutation:
                if attempt == 2: raise HTTPException(503, "同步期间数据持续变化，请稍后重试；未保存部分结果") from None
        with bank._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT generation,id FROM sandbox_plaid_connections WHERE session_id=?", (sid,)).fetchone()
            if not current or current["id"] != row["id"] or current["generation"] != generation:
                raise HTTPException(409, "连接已变化，本次同步结果已丢弃")
            for kind, tx in mutations:
                if kind == "remove":
                    db.execute("DELETE FROM sandbox_plaid_transactions WHERE session_id=? AND id=?", (sid, tx["id"]))
                else:
                    db.execute("INSERT INTO sandbox_plaid_transactions VALUES(?,?,?) ON CONFLICT(session_id,id) DO UPDATE SET payload=excluded.payload", (sid, tx["id"], json.dumps(tx)))
            db.execute("UPDATE sandbox_plaid_connections SET cursor=?,accounts_json=?,status='CONNECTED',last_error=NULL,updated_at=? WHERE session_id=?", (cursor, json.dumps(account_rows), bank._now().isoformat(), sid))
            bank._audit(db, sid, None, "PLAID_SANDBOX_SYNCED", {"mutations": len(mutations), "accounts": len(account_rows)})
            return view(db, sid)
    except (HTTPException, InvalidToken, KeyError, TypeError, ValueError) as error:
        fail_operation(sid, generation, error, row["id"])
        if isinstance(error, HTTPException): raise
        raise HTTPException(502, "同步响应或加密令牌无效，最近同步结果保持不变") from None


@bank.router.post("/external/plaid/disconnect")
def disconnect(req: Consent, x_banking_session: str | None = Header(default=None)):
    if not req.confirmed:
        raise HTTPException(422, "请确认断开测试连接并清除导入记录")
    sid, row, generation = begin_operation(x_banking_session, "DISCONNECTING")
    try:
        token = cipher().decrypt(row["token_cipher"].encode()).decode()
        plaid_call("/item/remove", {"access_token": token})
        with bank._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("DELETE FROM sandbox_plaid_connections WHERE session_id=? AND id=? AND generation=?", (sid, row["id"], generation)).rowcount
            if not changed: raise HTTPException(409, "连接状态已变化，请刷新")
            db.execute("DELETE FROM sandbox_plaid_transactions WHERE session_id=?", (sid,))
            bank._audit(db, sid, None, "PLAID_SANDBOX_DISCONNECTED", {"remote_item_removed": True})
            return view(db, sid)
    except (HTTPException, InvalidToken) as error:
        fail_operation(sid, generation, error, row["id"])
        if isinstance(error, HTTPException): raise
        raise HTTPException(503, "令牌无法解密，请恢复原服务端加密密钥后重试") from None
