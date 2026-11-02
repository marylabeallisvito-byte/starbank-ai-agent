"""Daily reference rates and explicit CNY budget reservations; never exchanges money."""
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Literal

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel

from backend import banking as b

URL = "https://api.frankfurter.dev/v2/rates?base=CNY&quotes=USD,HKD,EUR,JPY&expand=providers"
CURRENCIES = {"USD", "HKD", "EUR", "JPY"}


def schema(db):
    db.executescript("""CREATE TABLE IF NOT EXISTS fx_budget_quotes(
    id TEXT PRIMARY KEY,session_id TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,
    rate_hash TEXT NOT NULL,status TEXT NOT NULL,reserved_fen INTEGER NOT NULL,
    created_at TEXT NOT NULL,expires_at TEXT NOT NULL);""")


def clean_rates(raw):
    if not isinstance(raw, list): raise ValueError("invalid rate response")
    rows = []
    for item in raw:
        rate = Decimal(str(item["rate"]))
        observed = date.fromisoformat(item["date"])
        if item["base"] != "CNY" or item["quote"] not in CURRENCIES or not rate.is_finite() or not 0 < rate < 10000 or observed > datetime.now(b.TZ).date(): raise ValueError("invalid reference rate")
        providers = item.get("providers") or []
        rows.append({"base": "CNY", "quote": item["quote"], "rate": str(rate), "date": observed.isoformat(), "provider_count": len(providers)})
    if {r["quote"] for r in rows} != CURRENCIES or len(rows) != 4: raise ValueError("incomplete rate response")
    return sorted(rows, key=lambda r: r["quote"])


def rate_view(db):
    row = db.execute("SELECT * FROM sandbox_public_feeds WHERE source='fx_cny'").fetchone()
    corrupt = False; age=None; observation_age=None
    try:
        rates = json.loads(row["payload"]) if row and row["payload"] else []
        if rates:
            if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['content_hash']:raise ValueError('hash mismatch')
            # A valid digest is not proof of a valid or complete reference quote.
            clean_rates(rates)
            fetched=datetime.fromisoformat(row['fetched_at'])
            if fetched.tzinfo is None:raise ValueError('timestamp must include timezone')
            age=(b._now()-fetched).total_seconds()
            if age<0:raise ValueError('future cache timestamp')
            observation_age=max((datetime.now(b.TZ).date()-date.fromisoformat(r['date'])).days for r in rates)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):corrupt=True;rates=[]
    return {"rates": rates, "status": "unavailable" if not rates else "stale" if age > 21600 else "cached" if row["last_error"] else "fresh",
            "usable": bool(rates) and age < 172800 and observation_age <= 7,
            "fetched_at": row["fetched_at"] if row else None, "last_error": "本地汇率快照校验失败，请重新获取" if corrupt else row["last_error"] if row else None,
            "snapshot_hash": row["content_hash"] if row else None, "source": "Frankfurter blended daily reference rates", "source_url": "https://frankfurter.dev/",
            "scope": "日频混合参考汇率，不是银行实时成交报价；预算预留仅锁定本地仿真人民币资金。"}


def refresh_rates(force=False):
    now = b._now().isoformat()
    with b._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT attempted_at FROM sandbox_public_feeds WHERE source='fx_cny'").fetchone()
        if not force and previous and previous[0] > (b._now() - timedelta(seconds=60)).isoformat(): return rate_view(db)
        db.execute("INSERT INTO sandbox_public_feeds(source,attempted_at) VALUES('fx_cny',?) ON CONFLICT(source) DO UPDATE SET attempted_at=excluded.attempted_at", (now,))
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get(URL); response.raise_for_status()
            if len(response.content) > 1_000_000: raise ValueError("oversize response")
            payload = json.dumps(clean_rates(response.json()), sort_keys=True, separators=(",", ":"))
        with b._connect() as db:
            db.execute("UPDATE sandbox_public_feeds SET payload=?,content_hash=?,fetched_at=?,last_error=NULL WHERE source='fx_cny' AND attempted_at=?", (payload, hashlib.sha256(payload.encode()).hexdigest(), b._now().isoformat(), now))
    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation):
        with b._connect() as db: db.execute("UPDATE sandbox_public_feeds SET last_error='汇率来源暂不可用，保留最近成功快照' WHERE source='fx_cny' AND attempted_at=?", (now,))
    with b._connect() as db: return rate_view(db)


@b.router.get("/fx/rates")
def rates(x_banking_session: str | None = Header(default=None)):
    with b._connect() as db:
        b._session(db, x_banking_session); return rate_view(db)


@b.router.post("/fx/rates/refresh")
def refresh(x_banking_session: str | None = Header(default=None)):
    with b._connect() as db:
        sid = b._session(db, x_banking_session)["id"]; b._audit(db, sid, None, "FX_REFERENCE_REFRESH", {"source": "Frankfurter"})
    return refresh_rates()


class BudgetRequest(BaseModel):
    currency: Literal["HKD", "USD", "EUR", "JPY"]
    foreign_amount: str
    buffer_percent: str = "3"
    fee_cny: str = "0"
    budget_cny: str = "2000"
    snapshot_hash: str


def decimal(value, low, high, places=2):
    try:
        if len(value) > 50: raise ValueError()
        number = Decimal(value)
        if not number.is_finite() or not low <= number <= high or number.quantize(Decimal(1).scaleb(-places)) != number: raise ValueError()
        return number
    except (InvalidOperation, ValueError): raise HTTPException(422, "请输入范围内金额与比例，最多两位小数；日元金额为整数") from None


def quote_view(row):
    return {**json.loads(row["payload"]), "id": row["id"], "digest": row["digest"], "status": row["status"], "expires_at": row["expires_at"], "created_at": row["created_at"]}


@b.router.post("/fx/quotes")
def prepare(req: BudgetRequest, x_banking_session: str | None = Header(default=None)):
    foreign = decimal(req.foreign_amount, Decimal(".01"), Decimal("10000000"), 0 if req.currency == "JPY" else 2)
    buffer = decimal(req.buffer_percent, Decimal(0), Decimal(30))
    fee = decimal(req.fee_cny, Decimal(0), Decimal(1000))
    budget = decimal(req.budget_cny, Decimal(".01"), Decimal("10000000"))
    ceil = lambda v: v.quantize(Decimal(".01"), rounding=ROUND_CEILING)
    with b._connect() as db:
        db.execute("BEGIN IMMEDIATE"); sid = b._session(db, x_banking_session)["id"]
        feed = rate_view(db)
        if not feed["usable"] or feed["snapshot_hash"] != req.snapshot_hash: raise HTTPException(409, "参考汇率不可用、过期或已变化，请刷新并重新试算")
        reference = next(r for r in feed["rates"] if r["quote"] == req.currency)
        rate = Decimal(reference["rate"])
        principal = ceil(foreign / rate); reserve_buffer = ceil(principal * buffer / 100)
        total = principal + reserve_buffer + fee
        affordable = (max(Decimal(0), budget - fee) / (1 + buffer / 100) * rate).quantize(Decimal(1) if req.currency == "JPY" else Decimal(".01"), rounding=ROUND_FLOOR)
        unit = Decimal(1) if req.currency == "JPY" else Decimal(".01")
        while affordable > 0 and ceil(affordable / rate) + ceil(ceil(affordable / rate) * buffer / 100) + fee > budget:
            affordable -= unit
        now = b._now(); identifier = secrets.token_hex(16)
        payload = {"currency": req.currency, "foreign_amount": str(foreign), "reference_rate": str(rate), "rate_date": reference["date"], "snapshot_hash": feed["snapshot_hash"],
                   "principal_cny": str(principal), "buffer_percent": str(buffer), "buffer_cny": str(reserve_buffer), "fee_cny": str(fee), "total_cny": str(total),
                   "budget_cny": str(budget), "remaining_cny": str(budget - total), "affordable_foreign": str(affordable), "within_budget": total <= budget,
                   "scope": "预留人民币预算，不执行换汇或境外付款；费用与缓冲为用户输入的估算。"}
        digest = hashlib.sha256(json.dumps([sid, identifier, payload], sort_keys=True).encode()).hexdigest()
        db.execute("INSERT INTO fx_budget_quotes VALUES(?,?,?,?,?,?,?,?,?)", (identifier, sid, json.dumps(payload), digest, feed["snapshot_hash"], "PREVIEW", int(total * 100), now.isoformat(), (now + timedelta(minutes=15)).isoformat()))
        b._audit(db, sid, None, "FX_BUDGET_PREVIEW", {"id": identifier, "total_fen": int(total * 100), "rate_hash": feed["snapshot_hash"]})
        return quote_view(db.execute("SELECT * FROM fx_budget_quotes WHERE id=?", (identifier,)).fetchone())


class Action(BaseModel):
    digest: str
    confirmed: bool


@b.router.post("/fx/quotes/{identifier}/{action}")
def action(identifier: str, action: Literal["reserve", "release"], req: Action, x_banking_session: str | None = Header(default=None)):
    if not req.confirmed: raise HTTPException(422, "请明确确认当前预算操作")
    with b._connect() as db:
        db.execute("BEGIN IMMEDIATE"); user = b._session(db, x_banking_session)
        row = db.execute("SELECT * FROM fx_budget_quotes WHERE id=? AND session_id=?", (identifier, user["id"])).fetchone()
        if not row: raise HTTPException(404, "预算方案不存在")
        if row["digest"] != req.digest: raise HTTPException(409, "预算摘要不一致，请重新读取")
        if action == "reserve":
            if row["status"] == "RESERVED": return quote_view(row)
            feed = rate_view(db)
            if row["status"] != "PREVIEW" or row["expires_at"] < b._now().isoformat() or not feed["usable"] or feed["snapshot_hash"] != row["rate_hash"]: raise HTTPException(409, "方案或参考汇率已过期、变化，请重新试算")
            if not json.loads(row["payload"])["within_budget"]: raise HTTPException(409, "方案超过你的预算上限，请调整后重新试算")
            if user["locked_until"] and user["locked_until"] > b._now().isoformat(): raise HTTPException(423, "账户暂时锁定")
            if b._available(user) < row["reserved_fen"]: raise HTTPException(409, "本地可用余额不足，未预留资金")
            db.execute("UPDATE sandbox_sessions SET reserved_fen=reserved_fen+? WHERE id=?", (row["reserved_fen"], user["id"]))
            state = "RESERVED"
        else:
            if row["status"] == "RELEASED": return quote_view(row)
            if row["status"] != "RESERVED": raise HTTPException(409, "该方案没有已预留资金")
            db.execute("UPDATE sandbox_sessions SET reserved_fen=reserved_fen-? WHERE id=?", (row["reserved_fen"], user["id"]))
            state = "RELEASED"
        db.execute("UPDATE fx_budget_quotes SET status=? WHERE id=?", (state, identifier))
        b._audit(db, user["id"], None, "FX_BUDGET_" + state, {"id": identifier, "amount_fen": row["reserved_fen"]})
        return quote_view(db.execute("SELECT * FROM fx_budget_quotes WHERE id=?", (identifier,)).fetchone())


@b.router.get("/fx/quotes")
def quotes(x_banking_session: str | None = Header(default=None)):
    with b._connect() as db:
        sid = b._session(db, x_banking_session)["id"]
        return {"items": [quote_view(r) for r in db.execute("SELECT * FROM fx_budget_quotes WHERE session_id=? AND status IN ('RESERVED','RELEASED') ORDER BY created_at DESC LIMIT 30", (sid,))]}
