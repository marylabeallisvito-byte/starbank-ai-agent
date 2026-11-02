"""Isolated, deterministic banking sandbox for the customer-facing agent.

No endpoint in this module moves real money. Amounts use integer fen and all
state transitions take a SQLite write lock so concurrent requests cannot skip
the daily limit, double debit, or replay a confirmation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend.banking_nlu import classify as classify_semantic_intent
from backend import banking_contracts as contracts


def _sandbox_enabled() -> None:
    if os.getenv("APP_ENV", "development").casefold() == "production" and os.getenv("BANKING_SANDBOX_ENABLED") != "1":
        raise HTTPException(503, "生产环境未显式启用银行仿真沙箱")


router = APIRouter(prefix="/api/banking", tags=["banking-sandbox"], dependencies=[Depends(_sandbox_enabled)])
DB_PATH = Path(os.getenv("BANKING_SANDBOX_DB", Path(__file__).resolve().parent / "data" / "banking.sqlite3"))
TZ = timezone(timedelta(hours=8))
DAY_LIMIT_FEN = contracts.TRANSFER_STEPUP_FEN
CONTACTS = (
    {"id": "mom", "name": "妈妈", "bank": "星河银行", "account_tail": "2388"},
    {"id": "chen", "name": "陈晨", "bank": "星河银行", "account_tail": "6102"},
    {"id": "li", "name": "李老师", "bank": "海湾银行", "account_tail": "9921"},
)
CONTACT_ALIASES = {"mom": ("妈妈", "我妈", "母亲"), "chen": ("陈晨",), "li": ("李老师",)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(fen: int) -> str:
    return f"{Decimal(fen) / 100:.2f}"


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    from backend import banking_storage
    try: banking_storage.configure(db)
    except Exception: db.close();raise
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sandbox_sessions (
          id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
          balance_fen INTEGER NOT NULL, reserved_fen INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, locked_until TEXT);
        CREATE TABLE IF NOT EXISTS sandbox_transfers (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
          amount_fen INTEGER NOT NULL, note TEXT NOT NULL, level TEXT NOT NULL,
          status TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, confirmed_at TEXT, verified_at TEXT,
          committed_at TEXT, receipt TEXT, idempotency_key TEXT,
          FOREIGN KEY(session_id) REFERENCES sandbox_sessions(id));
        CREATE UNIQUE INDEX IF NOT EXISTS sandbox_idempotency
          ON sandbox_transfers(session_id, idempotency_key)
          WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS sandbox_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          transfer_id TEXT, event TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL, prev_hash TEXT, event_hash TEXT);
        CREATE TABLE IF NOT EXISTS sandbox_otp (
          transfer_id TEXT PRIMARY KEY, code_hash TEXT NOT NULL,
          attempts INTEGER NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sandbox_bills (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, merchant TEXT NOT NULL,
          category TEXT NOT NULL, amount_fen INTEGER NOT NULL, occurred_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sandbox_cards (
          session_id TEXT PRIMARY KEY, tail TEXT NOT NULL, status TEXT NOT NULL,
          updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sandbox_card_actions (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, action TEXT NOT NULL,
          status TEXT NOT NULL, level TEXT NOT NULL, digest TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          confirmed_at TEXT, verified_at TEXT, executed_at TEXT,
          idempotency_key TEXT, demo_code TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS sandbox_card_action_idempotency
          ON sandbox_card_actions(session_id,idempotency_key)
          WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS sandbox_subscriptions (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, merchant TEXT NOT NULL,
          amount_fen INTEGER NOT NULL, next_charge_at TEXT NOT NULL,
          status TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sandbox_subscription_actions (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
          status TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, confirmed_at TEXT, executed_at TEXT,
          idempotency_key TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS sandbox_subscription_idempotency
          ON sandbox_subscription_actions(session_id,idempotency_key)
          WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS sandbox_plans (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
          status TEXT NOT NULL, run_at TEXT NOT NULL, amount_fen INTEGER NOT NULL,
          subscription_id TEXT NOT NULL, digest TEXT NOT NULL,
          nodes_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          confirmed_at TEXT, released_at TEXT, failure_reason TEXT);
        CREATE TABLE IF NOT EXISTS sandbox_notifications (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, plan_id TEXT NOT NULL,
          message TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(session_id,plan_id));
        CREATE TABLE IF NOT EXISTS sandbox_dialog_context (
          session_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
          slots_json TEXT NOT NULL, expires_at TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES sandbox_sessions(id));
        CREATE TABLE IF NOT EXISTS sandbox_graph_sources (
          session_id TEXT NOT NULL, recipient_id TEXT NOT NULL, graph_json TEXT NOT NULL,
          graph_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          revision INTEGER NOT NULL, scenario TEXT NOT NULL, PRIMARY KEY(session_id,recipient_id));
        CREATE TABLE IF NOT EXISTS sandbox_consents(session_id TEXT NOT NULL,scope TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(session_id,scope));
        CREATE TABLE IF NOT EXISTS sandbox_wealth_profiles (
          session_id TEXT PRIMARY KEY, risk_level INTEGER NOT NULL,
          answers_json TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES sandbox_sessions(id));
        CREATE TABLE IF NOT EXISTS sandbox_wealth_orders (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, product_id TEXT NOT NULL,
          amount_fen INTEGER NOT NULL, action TEXT NOT NULL DEFAULT 'BUY', status TEXT NOT NULL, digest TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          confirmed_at TEXT, verified_at TEXT, committed_at TEXT,
          otp_hash TEXT, demo_code TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          receipt TEXT, idempotency_key TEXT,
          FOREIGN KEY(session_id) REFERENCES sandbox_sessions(id));
        CREATE UNIQUE INDEX IF NOT EXISTS sandbox_wealth_idempotency
          ON sandbox_wealth_orders(session_id,idempotency_key)
          WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS sandbox_wealth_holdings (
          session_id TEXT NOT NULL, product_id TEXT NOT NULL,
          principal_fen INTEGER NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(session_id,product_id),
          FOREIGN KEY(session_id) REFERENCES sandbox_sessions(id));
    """)
    from backend.banking_external import schema as external_schema
    external_schema(db)
    from backend.banking_statements import schema as statement_schema
    from backend.banking_fx import schema as fx_schema
    statement_schema(db)
    fx_schema(db)
    # Add integrity fields to databases created by an earlier sandbox build.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(sandbox_audit)")}
    if "prev_hash" not in columns:
        db.execute("ALTER TABLE sandbox_audit ADD COLUMN prev_hash TEXT")
    if "event_hash" not in columns:
        db.execute("ALTER TABLE sandbox_audit ADD COLUMN event_hash TEXT")
    transfer_columns = {row["name"] for row in db.execute("PRAGMA table_info(sandbox_transfers)")}
    if "graph_revision" not in transfer_columns:
        db.execute("ALTER TABLE sandbox_transfers ADD COLUMN graph_revision TEXT NOT NULL DEFAULT ''")
    if "risk_snapshot" not in transfer_columns:
        db.execute("ALTER TABLE sandbox_transfers ADD COLUMN risk_snapshot TEXT")
    session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sandbox_sessions)")}
    if "reserved_fen" not in session_columns:
        db.execute("ALTER TABLE sandbox_sessions ADD COLUMN reserved_fen INTEGER NOT NULL DEFAULT 0")
    wealth_columns = {row["name"] for row in db.execute("PRAGMA table_info(sandbox_wealth_orders)")}
    if "action" not in wealth_columns:
        db.execute("ALTER TABLE sandbox_wealth_orders ADD COLUMN action TEXT NOT NULL DEFAULT 'BUY'")
    if 'profile_revision' not in wealth_columns:
        db.execute('ALTER TABLE sandbox_wealth_orders ADD COLUMN profile_revision TEXT')
    profile_columns={row['name'] for row in db.execute('PRAGMA table_info(sandbox_wealth_profiles)')}
    if 'revision_id' not in profile_columns:
        db.execute("ALTER TABLE sandbox_wealth_profiles ADD COLUMN revision_id TEXT NOT NULL DEFAULT ''")
    try: banking_storage.install(db)
    except Exception: db.close();raise
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _session(db: sqlite3.Connection, token: str | None) -> sqlite3.Row:
    if not token or len(token) < 20:
        raise HTTPException(401, "缺少沙箱会话")
    row = db.execute("SELECT * FROM sandbox_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if row is None or row["expires_at"] < _now().isoformat():
        raise HTTPException(401, "沙箱会话无效或已过期")
    return row


def _audit(db: sqlite3.Connection, sid: str, transfer_id: str | None, event: str, detail: dict[str, Any]) -> None:
    timestamp = _now().isoformat()
    payload = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    previous = db.execute("SELECT event_hash FROM sandbox_audit WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    prev_hash = previous["event_hash"] if previous and previous["event_hash"] else "GENESIS"
    event_hash = hashlib.sha256(json.dumps([prev_hash, sid, transfer_id, event, payload, timestamp], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    db.execute("INSERT INTO sandbox_audit(session_id,transfer_id,event,detail,created_at,prev_hash,event_hash) VALUES(?,?,?,?,?,?,?)",
               (sid, transfer_id, event, payload, timestamp, prev_hash, event_hash))


def _amount_fen(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise HTTPException(422, "金额格式无效") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise HTTPException(422, "金额须大于 0 且最多两位小数")
    fen = int(amount * 100)
    if fen > 1_000_000_000:
        raise HTTPException(422, "超出沙箱单笔限额")
    return fen


def _intent_amount(text: str) -> tuple[str | None, bool]:
    """Normalize only amounts explicitly present in user text; never trust a model's guess."""
    values = re.findall(r"(?<![\d.])([-+]?\d+(?:\.\d+)?)\s*(?:元|块)", text)
    chinese_matches = re.findall(r"([零〇一二两三四五六七八九十百千万]+)(?:点([零〇一二三四五六七八九]+))?\s*(?:元|块)", text)
    if not values and not chinese_matches:
        action = re.search(r"(?:转|打|汇)(.+)", text)
        if action:
            values = re.findall(r"[-+]?\d+(?:\.\d+)?", action.group(1))
            chinese_matches = re.findall(r"([零〇一二两三四五六七八九十百千万]+)(?:点([零〇一二三四五六七八九]+))?", action.group(1))
    digits = {char: value for value, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    for integer, fraction in chinese_matches:
        if not any(char in integer for char in "十百千万"):
            whole = int("".join(str(digits[char]) for char in integer))
        else:
            total = section = number = 0
            for char in integer:
                if char in digits:
                    number = digits[char]
                elif char == "万":
                    total += (section + number) * 10000
                    section = number = 0
                else:
                    section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                    number = 0
            whole = total + section + number
        values.append(str(whole) + ("." + "".join(str(digits[char]) for char in fraction) if fraction else ""))
    return (values[0] if len(values) == 1 else None, len(values) > 1)


def _daily_spend(db: sqlite3.Connection, sid: str) -> int:
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    row = db.execute("SELECT COALESCE(SUM(amount_fen),0) AS total FROM sandbox_transfers WHERE session_id=? AND status='COMMITTED' AND committed_at>=?", (sid, start)).fetchone()
    return int(row["total"])


def _available(user: sqlite3.Row) -> int:
    return int(user["balance_fen"]) - int(user["reserved_fen"])


def _transfer_risk(db: sqlite3.Connection, sid: str, recipient_id: str, amount_fen: int, exclude_transfer_id: str | None = None) -> dict[str, Any]:
    """Explainable signals from this session's committed synthetic transfers only."""
    ten_minutes = (_now() - timedelta(minutes=10)).isoformat()
    two_minutes = (_now() - timedelta(minutes=2)).isoformat()
    previous = db.execute("SELECT COUNT(*) AS n FROM sandbox_transfers WHERE session_id=? AND recipient_id=? AND status='COMMITTED' AND id!=?",
                          (sid, recipient_id, exclude_transfer_id or "")).fetchone()["n"]
    recent = db.execute("SELECT id,recipient_id,amount_fen,committed_at FROM sandbox_transfers WHERE session_id=? AND status='COMMITTED' AND committed_at>=? AND id!=? ORDER BY committed_at DESC",
                        (sid, ten_minutes, exclude_transfer_id or "")).fetchall()
    duplicate = next((row for row in recent if row["recipient_id"] == recipient_id and row["amount_fen"] == amount_fen and row["committed_at"] >= two_minutes), None)
    contact = next(item for item in CONTACTS if item["id"] == recipient_id)
    factors: list[dict[str, Any]] = []
    if previous == 0:
        factors.append({"code": "FIRST_TRANSFER", "description": "当前沙箱会话首次向该已验证联系人转账", "transaction_ids": []})
    if contact["bank"] != "星河银行":
        factors.append({"code": "CROSS_BANK", "description": "收款账户位于另一家模拟银行", "transaction_ids": []})
    if len(recent) >= 2:
        factors.append({"code": "RECENT_ACTIVITY", "description": "十分钟内已有多笔模拟转账", "transaction_ids": [row["id"] for row in recent]})
    if duplicate:
        factors.append({"code": "POSSIBLE_DUPLICATE", "description": "两分钟内已有同一收款人、同一金额的成功转账", "transaction_ids": [duplicate["id"]]})
    from backend.banking_evidence import retrieve
    graph = retrieve(db, sid, recipient_id)
    factors.extend({"code": f["code"], "description": f["title"], "transaction_ids": f["source_ids"]} for f in graph["findings"])
    review = graph["decision"] in ("verify", "hold", "unavailable") or len(recent) >= 2 or (previous == 0 and contact["bank"] != "星河银行" and amount_fen >= 50_000)
    return {"tier": "review" if review else "normal", "factors": factors, "duplicate_transfer_id": duplicate["id"] if duplicate else None,
            "source": "synthetic_session_ledger", "graph": graph, "sandbox": True}


def _view(row: sqlite3.Row, db: sqlite3.Connection) -> dict[str, Any]:
    contact = next(c for c in CONTACTS if c["id"] == row["recipient_id"])
    result = {
        "id": row["id"], "recipient": contact, "amount": _money(row["amount_fen"]),
        "note": row["note"], "level": row["level"], "status": row["status"],
        "digest": row["digest"], "expires_at": row["expires_at"],
        "receipt": row["receipt"] if row["status"] == "COMMITTED" else None,
        "risk": json.loads(row["risk_snapshot"]) if row["risk_snapshot"] else _transfer_risk(db, row["session_id"], row["recipient_id"], row["amount_fen"], row["id"] if row["status"] == "COMMITTED" else None),
        "sandbox": True,
    }
    if row["status"] == "AWAITING_VERIFICATION":
        result["verification_hint"] = "演示验证码可在本会话内获取；此机制仅用于沙箱，非真实多因子认证。"
    return result


class PrepareTransfer(BaseModel):
    recipient_id: str
    amount: str
    note: str = Field(default="", max_length=100)


class ConfirmTransfer(BaseModel):
    digest: str


class VerifyTransfer(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class ExecuteTransfer(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


class IntentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@router.post("/session")
def create_session() -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    sid = secrets.token_hex(16)
    now = _now()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO sandbox_sessions(id,token_hash,balance_fen,created_at,expires_at) VALUES(?,?,?,?,?)",
                   (sid, hashlib.sha256(token.encode()).hexdigest(), 250_000, now.isoformat(), (now + timedelta(days=7)).isoformat()))
        db.execute("INSERT INTO sandbox_cards(session_id,tail,status,updated_at) VALUES(?,?,?,?)", (sid, "2388", "ACTIVE", now.isoformat()))
        for merchant, amount, days in (("云上视频", 4200, 3), ("听书计划", 1800, 8), ("云端空间", 1200, 16)):
            db.execute("INSERT INTO sandbox_subscriptions(id,session_id,merchant,amount_fen,next_charge_at,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (secrets.token_hex(8), sid, merchant, amount, (now + timedelta(days=days)).isoformat(), "ACTIVE", now.isoformat()))
        # Fictional sample bills. Opening balance is considered before these
        # historical expenses; the displayed 2500 CNY is the resulting balance.
        sample_bills = [
            ("晨间咖啡", "餐饮", 2850, 2, 9, 10),
            ("城市地铁", "交通", 600, 3, 8, 20),
            ("云上视频", "订阅", 4200, 5, 12, 0),
            ("云上视频", "订阅", 4200, 5, 12, 1),
            ("悦享超市", "购物", 18900, 8, 18, 30),
            ("椰子餐厅", "餐饮", 6850, 11, 19, 20),
            ("城市地铁", "交通", 700, 13, 8, 20),
            ("星空书店", "购物", 8500, 15, 16, 12),
        ]
        for merchant, category, amount, day, hour, minute in sample_bills:
            # Keep all synthetic bills in the current month and never in the future.
            # Distinct metro fares must not turn into a false duplicate when their
            # nominal dates collapse at the beginning of a month. The video pair
            # deliberately retains equal amounts as the single review fixture.
            local_now=now.astimezone(TZ)
            local = min(local_now,local_now.replace(day=min(day,local_now.day),hour=hour,minute=minute,second=0,microsecond=0))
            db.execute("INSERT INTO sandbox_bills(id,session_id,merchant,category,amount_fen,occurred_at) VALUES(?,?,?,?,?,?)",
                       (secrets.token_hex(8), sid, merchant, category, amount, local.astimezone(timezone.utc).isoformat()))
        _audit(db, sid, None, "SESSION_CREATED", {"sandbox": True})
    return {"token": token, "expires_at": (now + timedelta(days=7)).isoformat(), "sandbox": True}


@router.get("/overview")
def overview(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        rows = db.execute("SELECT * FROM sandbox_transfers WHERE session_id=? AND status='COMMITTED' ORDER BY committed_at DESC LIMIT 8", (user["id"],)).fetchall()
        return {"balance": _money(_available(user)), "total_balance": _money(user["balance_fen"]),
                "reserved": _money(user["reserved_fen"]), "daily_spend": _money(_daily_spend(db, user["id"])),
                "daily_limit": "1000.00", "contacts": CONTACTS, "transactions": [_view(r, db) for r in rows],
                "sandbox": True}


@router.post("/intent")
def interpret_intent(req: IntentRequest, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    """Return grounded read results or unexecuted drafts from a strict allowlist."""
    text = req.text.strip()
    semantic = None
    rule_terms = contracts.RULE_TERMS
    if os.getenv("BANKING_LLM_ENABLED") == "1" and not any(term in text for term in rule_terms) and not re.search(r"给.{1,20}转", text):
        # Do not hold a write transaction while waiting for a remote model.
        with _connect() as lookup:
            user = _session(lookup, x_banking_session)
            active = lookup.execute("SELECT id,merchant FROM sandbox_subscriptions WHERE session_id=? AND status='ACTIVE'", (user["id"],)).fetchall()
            semantic = classify_semantic_intent(text, list(CONTACTS), [dict(item) for item in active])
    semantic_kind = semantic.kind if semantic else None
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        _audit(db, user["id"], None, "INTENT_RECEIVED", {"text_sha256": hashlib.sha256(text.encode()).hexdigest()})
        previous = db.execute("SELECT * FROM sandbox_dialog_context WHERE session_id=? AND expires_at>?", (user["id"], _now().isoformat())).fetchone()
        amount, ambiguous_amount = _intent_amount(text)
        contacts = [contact for contact in CONTACTS if any(alias in text for alias in CONTACT_ALIASES.get(contact["id"],(contact["name"],)))]
        transfer_words = bool(re.search(r"(?:转账|转给|汇款|打给|给.{1,20}转)", text))
        plan_words = any(word in text for word in ("预留", "留出", "计划", "安排"))
        if contracts.needs_scope_clarification(text):
            db.execute('DELETE FROM sandbox_dialog_context WHERE session_id=?',(user['id'],))
            result = {'kind':'clarification','reply':'这条需求包含多个业务或资金执行条件。请先明确要优先办理的业务及其条件，我不会只执行其中一部分。','sandbox':True}
        elif transfer_words and any(term in text.casefold() for term in ("美元", "美金", "港币", "港幣", "港元", "欧元", "歐元", "日元", "日圓", "英镑", "usd", "hkd", "eur", "jpy", "gbp", "sgd", "aud", "cad", "chf", "$", "€", "£")):
            result = {"kind": "clarification", "reply": "当前转账只支持人民币仿真账本，不能将外币金额当作人民币执行。可先使用跨境预算试算，换汇与外部支付尚未开放。", "sandbox": True}
        elif any(term in text.upper() for term in ("机构登记", "机构身份", "LEI")) or semantic_kind == 'entity_lookup':
            result = {"kind": "entity_lookup" if not (transfer_words or plan_words) else "clarification", "reply": "请在机构登记面板填写公开 LEI 编号，确认后向 GLEIF 官方查询。登记资料不证明收款账户归属或交易安全，资金操作需要单独授权。", "sandbox": True}
        elif (any(term in text for term in ("导入账单", "上传账单", "跨境预算", "汇率")) or semantic_kind in {'statement_import','fx_budget'}) and not transfer_words:
            kind = semantic_kind if semantic_kind in {'statement_import','fx_budget'} else "statement_import" if "账单" in text else "fx_budget"
            result = {"kind": kind, "reply": "请在账单导入面板选择本机文件，先核对金额方向与解析预览，再明确确认保存。原始文件不发送给模型。" if kind == "statement_import" else "已打开跨境预算面板。请核对币种、花费、费用与预算上限；试算不锁定资金，预留需另行明确确认，不执行换汇。", "sandbox": True}
        elif any(term in text for term in ("防骗通知", "风险通知", "官方提醒", "最新骗案")) or semantic_kind == 'public_notices':
            if transfer_words or plan_words or any(term in text for term in ("申购", "挂失", "取消", "支付")):
                result = {"kind": "clarification", "reply": "请先分开查询官方通知与办理业务。公开通知不是收款账户黑名单，不能代替交易授权。", "sandbox": True}
            else:
                from backend.banking_external import feed_view
                feed = feed_view(db)
                snippets = "\n".join(f"{item['published_at']} · {item['title'][:200]} [{item['id']}]" for item in feed["items"][:3])
                result = {"kind": "public_notices", "reply": ("以下来自已获取的香港金融管理局公开通知，点击数据连接可查看官方原文。\n" + snippets if snippets else "尚未取得官方通知，请在数据连接页面更新来源。") + "\n公开通知不是账户黑名单，未命中不代表安全。", "sandbox": True}
        elif ambiguous_amount and (transfer_words or plan_words or semantic_kind in {"transfer_draft", "plan_draft"}):
            result = {"kind": "clarification", "reply": "你提到了多个金额。请先明确这一笔操作的金额，再继续核对。", "sandbox": True}
        elif re.search(r"(?:不要|别|不必|无需|取消).{0,12}(?:转账|转给|汇款|打给|转|预留|申购|购买)", text):
            db.execute("DELETE FROM sandbox_dialog_context WHERE session_id=?", (user["id"],))
            result = {"kind": "clarification", "reply": "已停止整理该操作草案。没有创建或执行任何资金操作。", "sandbox": True}
        elif (transfer_words or semantic_kind == "transfer_draft") and re.search(r"(?:明天|后天|定时|预约|下周|下月|每天|每月)", text):
            result = {"kind": "clarification", "reply": "这条需求包含执行时间。当前转账只支持即时预览，请明确是否现在办理；定时要求不会被忽略。", "sandbox": True}
        elif (plan_words and ("订阅" in text or "续费" in text)) or semantic_kind == "plan_draft":
            subscriptions = db.execute("SELECT id,merchant FROM sandbox_subscriptions WHERE session_id=? AND status='ACTIVE'", (user["id"],)).fetchall()
            matched = [item for item in subscriptions if item["merchant"] in text]
            if not matched and semantic and semantic.subscription_id:
                matched = [item for item in subscriptions if item["id"] == semantic.subscription_id and item["merchant"][:2] in text]
            days = 2 if "后天" in text else 1 if "明天" in text else 0 if "今天" in text else None
            local_run = None
            if days is not None:
                local_run = (datetime.now(TZ) + timedelta(days=days)).replace(hour=10, minute=0, second=0, microsecond=0)
                if local_run <= datetime.now(TZ):
                    local_run += timedelta(days=1)
            elif semantic and semantic.run_at and re.search(r"(?:今|明|后|周|月|号|日|星期|点|时|\d{4}-\d{1,2}-\d{1,2})", text):
                try:
                    candidate = datetime.fromisoformat(semantic.run_at)
                    if candidate.tzinfo and _now() <= candidate.astimezone(timezone.utc) <= _now() + timedelta(days=7):
                        local_run = candidate
                except ValueError:
                    pass
            if amount and len(matched) == 1 and local_run is not None:
                try:
                    _amount_fen(amount)
                except HTTPException:
                    amount = None
            if amount and len(matched) == 1 and local_run is not None:
                result = {"kind": "plan_draft", "reply": "已整理跨场景计划草案。请核对金额、订阅和时间，再由你决定是否创建预览。",
                          "amount": amount, "subscription_id": matched[0]["id"], "run_at": local_run.astimezone(timezone.utc).isoformat(), "sandbox": True}
            else:
                result = {"kind": "clarification", "reply": "请说明预留金额、具体订阅和执行日期，例如：明天预留 500 元，提醒云上视频续费。", "sandbox": True}
        elif transfer_words or semantic_kind == "transfer_draft" or (previous and previous["kind"] == "transfer_draft" and (amount or contacts)
                                and not any(word in text for word in ("订阅", "续费", "账单", "消费", "余额", "卡片", "挂失", "冻结", "计划", "预留"))):
            if re.search(r"(?:不要|别|不必|无需|取消)\s*(?:给.{0,10})?(?:转账|转给|汇款|打给|转)", text):
                result = {"kind": "clarification", "reply": "已停止整理转账草案。没有创建或执行转账。", "sandbox": True}
                db.execute("DELETE FROM sandbox_dialog_context WHERE session_id=?", (user["id"],))
            else:
                prior_slots = json.loads(previous["slots_json"]) if previous and previous["kind"] == "transfer_draft" else {}
                model_recipient = (semantic.recipient_id if semantic and semantic.recipient_id in CONTACT_ALIASES
                                   and any(alias in text for alias in CONTACT_ALIASES[semantic.recipient_id]) else None)
                recipient_id = contacts[0]["id"] if len(contacts) == 1 else None if contacts else (model_recipient or prior_slots.get("recipient_id"))
                amount = amount or prior_slots.get("amount")
                if amount:
                    try:
                        _amount_fen(amount)
                    except HTTPException:
                        amount = None
                if recipient_id and amount:
                    result = {"kind": "transfer_draft", "reply": "已整理转账草案。请核对收款人和金额，再创建交易预览。",
                              "recipient_id": recipient_id, "amount": amount, "sandbox": True}
                    db.execute("DELETE FROM sandbox_dialog_context WHERE session_id=?", (user["id"],))
                else:
                    slots = {"recipient_id": recipient_id, "amount": amount}
                    db.execute("INSERT OR REPLACE INTO sandbox_dialog_context(session_id,kind,slots_json,expires_at) VALUES(?,?,?,?)",
                               (user["id"], "transfer_draft", json.dumps(slots), (_now() + timedelta(minutes=15)).isoformat()))
                    result = {"kind": "clarification", "reply": "请补充已验证联系人和金额，例如：给妈妈转 800 元。", "sandbox": True}
        elif any(word in text for word in ("余额", "还剩", "账户有多少")) or semantic_kind == "balance":
            result = {"kind": "balance", "reply": f"演示账户可用余额为 ¥{_money(_available(user))}，其中已预留 ¥{_money(user['reserved_fen'])}。", "sandbox": True}
        elif any(word in text for word in ("流水", "交易记录", "最近交易")) or semantic_kind == "transactions":
            rows = db.execute("SELECT * FROM sandbox_transfers WHERE session_id=? AND status='COMMITTED' ORDER BY committed_at DESC LIMIT 5", (user["id"],)).fetchall()
            result = {"kind": "transactions", "reply": f"查到 {len(rows)} 笔演示转账记录。", "transactions": [_view(r, db) for r in rows], "sandbox": True}
        elif any(word in text for word in ("账单", "消费", "重复扣费")) or semantic_kind == "bills":
            result = {"kind": "bills", "reply": "我已打开账单分析。分类金额来自仿真交易记录，疑似重复扣费需要你进一步核对。", "sandbox": True}
        elif any(word in text for word in ("卡片", "银行卡", "挂失", "冻结")) or semantic_kind == "card":
            result = {"kind": "card", "reply": "我已打开卡片管理。冻结和挂失需要查看操作详情并经过服务端权限检查。", "sandbox": True}
        elif any(word in text for word in ("订阅", "续费", "代扣")) or semantic_kind == "subscriptions":
            result = {"kind": "subscriptions", "reply": "我已打开订阅管理。读取订阅明细及取消模拟订阅都需要明确确认，当前没有执行这些操作。", "sandbox": True}
        elif any(word in text for word in ("理财", "申购", "基金", "风险测评", "投资产品")) or semantic_kind == "wealth":
            result = {"kind": "wealth", "reply": "我已打开理财实验室。先完成模拟风险测评并核对产品，申购须经过红色权限验证。", "sandbox": True}
        else:
            result = {"kind": "unsupported", "reply": "当前演示支持余额、交易、账单、转账、卡片、订阅、理财和预留提醒计划。未识别的请求不会执行。", "sandbox": True}
        _audit(db, user["id"], None, "INTENT_RESOLVED", {"kind": result["kind"], "draft_only": result["kind"] in {"transfer_draft", "plan_draft"}, "semantic_classifier_used": semantic is not None})
        return contracts.decorate(result)


@router.get('/capabilities')
def capabilities(x_banking_session: str | None = Header(default=None)):
    with _connect() as db:
        _session(db,x_banking_session)
    return contracts.catalogue()


@router.post("/transfers/prepare")
def prepare_transfer(req: PrepareTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    amount = _amount_fen(req.amount)
    contact = next((c for c in CONTACTS if c["id"] == req.recipient_id), None)
    if contact is None:
        raise HTTPException(422, "收款人不在已验证联系人中")
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户因验证失败暂时锁定")
        if _available(user) < amount:
            raise HTTPException(409, "演示账户余额不足")
        risk = _transfer_risk(db, user["id"], contact["id"], amount)
        if risk["graph"]["decision"] in ("hold", "unavailable"):
            _audit(db, user["id"], None, "GRAPH_GATE_BLOCKED", {"revision": risk["graph"]["revision"], "decision": risk["graph"]["decision"], "sources": risk["graph"]["findings"]})
            db.commit()
            raise HTTPException(409, risk["graph"]["summary"])
        if risk["duplicate_transfer_id"]:
            _audit(db, user["id"], risk["duplicate_transfer_id"], "TRANSFER_PREPARE_BLOCKED", {"reason": "possible_duplicate"})
            db.commit()
            raise HTTPException(409, "两分钟内已有相同收款人与金额的成功转账，请先核对交易记录")
        level = contracts.transfer_level(_daily_spend(db, user['id']),amount,risk['tier'])
        transfer_id = secrets.token_hex(16)
        created = _now()
        expires = created + timedelta(minutes=5)
        canonical = f"{risk['graph']['revision']}|{user['id']}|{transfer_id}|{contact['id']}|{amount}|{req.note}|{level}|{expires.isoformat()}"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        db.execute("INSERT INTO sandbox_transfers(id,session_id,recipient_id,amount_fen,note,level,status,digest,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (transfer_id, user["id"], contact["id"], amount, req.note, level, "PREPARED", digest, created.isoformat(), expires.isoformat()))
        db.execute("UPDATE sandbox_transfers SET graph_revision=?,risk_snapshot=? WHERE id=?", (risk["graph"]["revision"], json.dumps(risk, ensure_ascii=False), transfer_id))
        _audit(db, user["id"], transfer_id, "GRAPH_ASSESSED", risk["graph"])
        _audit(db, user["id"], transfer_id, "PREPARED", {"amount_fen": amount, "recipient_id": contact["id"], "level": level,
                                                          "risk_tier": risk["tier"], "risk_factors": [factor["code"] for factor in risk["factors"]]})
        row = db.execute("SELECT * FROM sandbox_transfers WHERE id=?", (transfer_id,)).fetchone()
        return _view(row, db)


def _owned_transfer(db: sqlite3.Connection, user: sqlite3.Row, transfer_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM sandbox_transfers WHERE id=? AND session_id=?", (transfer_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "转账预览不存在")
    return row


@router.post("/transfers/{transfer_id}/confirm")
def confirm_transfer(transfer_id: str, req: ConfirmTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned_transfer(db, user, transfer_id)
        if row["status"] != "PREPARED" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "预览已失效，请重新发起")
        if not secrets.compare_digest(req.digest, row["digest"]):
            _audit(db, user["id"], transfer_id, "CONFIRM_REJECTED", {"reason": "digest_mismatch"})
            db.commit()
            raise HTTPException(409, "确认内容与预览不一致")
        status = "AWAITING_VERIFICATION" if row["level"] == "red" else "READY"
        db.execute("UPDATE sandbox_transfers SET status=?, confirmed_at=? WHERE id=?", (status, _now().isoformat(), transfer_id))
        if status == "AWAITING_VERIFICATION":
            code = f"{secrets.randbelow(1_000_000):06d}"
            db.execute("INSERT OR REPLACE INTO sandbox_otp(transfer_id,code_hash,attempts,expires_at) VALUES(?,?,0,?)",
                       (transfer_id, hashlib.sha256(code.encode()).hexdigest(), (_now() + timedelta(minutes=3)).isoformat()))
            # Only a sandbox convenience: a production OTP must use a separate channel.
            db.execute("UPDATE sandbox_transfers SET receipt=? WHERE id=?", (f"DEMO_OTP:{code}", transfer_id))
        _audit(db, user["id"], transfer_id, "CONFIRMED", {"next_status": status, "digest": row["digest"]})
        return _view(db.execute("SELECT * FROM sandbox_transfers WHERE id=?", (transfer_id,)).fetchone(), db)


@router.get("/transfers/{transfer_id}/demo-code")
def demo_code(transfer_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, str]:
    from backend.banking_auth import require_demo
    require_demo()
    with _connect() as db:
        user = _session(db, x_banking_session)
        row = _owned_transfer(db, user, transfer_id)
        if row["status"] != "AWAITING_VERIFICATION" or not row["receipt"] or not row["receipt"].startswith("DEMO_OTP:"):
            raise HTTPException(409, "当前无需演示验证")
        return {"code": row["receipt"].split(":", 1)[1], "label": "仅用于仿真沙箱，非真实多因子认证"}


@router.post("/transfers/{transfer_id}/verify")
def verify_transfer(transfer_id: str, req: VerifyTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    from backend.banking_auth import require_demo
    require_demo()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned_transfer(db, user, transfer_id)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户因验证失败暂时锁定")
        if row["status"] != "AWAITING_VERIFICATION":
            raise HTTPException(409, "当前状态不可验证")
        otp = db.execute("SELECT * FROM sandbox_otp WHERE transfer_id=?", (transfer_id,)).fetchone()
        if not otp or otp["expires_at"] < _now().isoformat():
            raise HTTPException(409, "验证码已过期")
        if not secrets.compare_digest(hashlib.sha256(req.code.encode()).hexdigest(), otp["code_hash"]):
            attempts = otp["attempts"] + 1
            db.execute("UPDATE sandbox_otp SET attempts=? WHERE transfer_id=?", (attempts, transfer_id))
            if attempts >= 3:
                db.execute("UPDATE sandbox_sessions SET locked_until=? WHERE id=?", ((_now() + timedelta(minutes=10)).isoformat(), user["id"]))
            _audit(db, user["id"], transfer_id, "VERIFICATION_FAILED", {"attempts": attempts, "locked": attempts >= 3})
            return {"verified": False, "attempts_remaining": max(0, 3 - attempts), "locked": attempts >= 3}
        db.execute("UPDATE sandbox_transfers SET status='READY', verified_at=?, receipt=NULL WHERE id=?", (_now().isoformat(), transfer_id))
        db.execute("DELETE FROM sandbox_otp WHERE transfer_id=?", (transfer_id,))
        _audit(db, user["id"], transfer_id, "VERIFIED", {"method": "demo_otp"})
        return {"verified": True, "transfer": _view(db.execute("SELECT * FROM sandbox_transfers WHERE id=?", (transfer_id,)).fetchone(), db)}


@router.post("/transfers/{transfer_id}/execute")
def execute_transfer(transfer_id: str, req: ExecuteTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned_transfer(db, user, transfer_id)
        if row["status"] == "COMMITTED" and row["idempotency_key"] == req.idempotency_key:
            return _view(row, db)
        if row["status"] != "READY" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "转账尚未完成确认、验证或已过期")
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户因验证失败暂时锁定")
        spend = _daily_spend(db, user["id"])
        if row["level"] == "red":
            from backend.banking_auth import require_grant
            require_grant(db, user["id"], "transfer", row)
        risk = _transfer_risk(db, user["id"], row["recipient_id"], row["amount_fen"])
        if risk["graph"]["decision"] in ("hold", "unavailable"):
            _audit(db, user["id"], None, "GRAPH_GATE_BLOCKED", {"revision": risk["graph"]["revision"], "decision": risk["graph"]["decision"], "sources": risk["graph"]["findings"]})
            db.commit()
            raise HTTPException(409, risk["graph"]["summary"])
        if risk["duplicate_transfer_id"]:
            _audit(db, user["id"], transfer_id, "EXECUTION_REJECTED", {"reason": "possible_duplicate", "source_id": risk["duplicate_transfer_id"]})
            db.commit()
            raise HTTPException(409, "两分钟内已有相同收款人与金额的成功转账，请先核对交易记录")
        if row["graph_revision"] != risk["graph"]["revision"]:
            _audit(db, user["id"], transfer_id, "GRAPH_REVISION_CHANGED", {"previous": row["graph_revision"], "current": risk["graph"]["revision"]})
            db.commit()
            raise HTTPException(409, "关联证据已变化，请重新预览并授权转账")
        if row["level"] == "yellow" and contracts.transfer_level(spend,row['amount_fen'],risk['tier']) == 'red':
            _audit(db, user["id"], transfer_id, "EXECUTION_REJECTED", {"reason": "level_changed", "risk_tier": risk["tier"]})
            db.commit()
            raise HTTPException(409, "风险或日累计金额已变化，请按红色权限重新发起")
        if _available(user) < row["amount_fen"]:
            raise HTTPException(409, "演示账户余额不足")
        collision = db.execute("SELECT id FROM sandbox_transfers WHERE session_id=? AND idempotency_key=?", (user["id"], req.idempotency_key)).fetchone()
        if collision:
            raise HTTPException(409, "幂等键已用于另一笔交易")
        receipt = "SBX-" + secrets.token_hex(8).upper()
        now = _now().isoformat()
        db.execute("UPDATE sandbox_sessions SET balance_fen=balance_fen-? WHERE id=?", (row["amount_fen"], user["id"]))
        db.execute("UPDATE sandbox_transfers SET status='COMMITTED', committed_at=?, receipt=?, idempotency_key=? WHERE id=?",
                   (now, receipt, req.idempotency_key, transfer_id))
        _audit(db, user["id"], transfer_id, "COMMITTED", {"receipt": receipt, "amount_fen": row["amount_fen"], "recipient_id": row["recipient_id"]})
        return _view(db.execute("SELECT * FROM sandbox_transfers WHERE id=?", (transfer_id,)).fetchone(), db)


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        return _view(_owned_transfer(db, user, transfer_id), db)


@router.get("/audit")
def audit(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        all_rows = db.execute("SELECT transfer_id,event,detail,created_at,prev_hash,event_hash FROM sandbox_audit WHERE session_id=? ORDER BY id", (user["id"],)).fetchall()
        previous = "GENESIS"
        integrity = True
        for row in all_rows:
            expected = hashlib.sha256(json.dumps([previous, user["id"], row["transfer_id"], row["event"], row["detail"], row["created_at"]], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if row["prev_hash"] != previous or row["event_hash"] != expected:
                integrity = False
            previous = row["event_hash"] or "GENESIS"
        return {"events": [{"transfer_id": r["transfer_id"], "event": r["event"], "detail": json.loads(r["detail"]), "created_at": r["created_at"], "event_hash": r["event_hash"]} for r in reversed(all_rows[-30:])],
                "integrity": integrity, "sandbox": True}


@router.get("/bills")
def bills(month: str | None = None, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    selected = month or datetime.now(TZ).strftime("%Y-%m")
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", selected):
        raise HTTPException(422, "月份格式须为 YYYY-MM")
    with _connect() as db:
        user = _session(db, x_banking_session)
        rows = db.execute("SELECT * FROM sandbox_bills WHERE session_id=? ORDER BY occurred_at DESC", (user["id"],)).fetchall()
        items = [{"id": r["id"], "merchant": r["merchant"], "category": r["category"],
                  "amount": _money(r["amount_fen"]), "occurred_at": r["occurred_at"]}
                 for r in rows if datetime.fromisoformat(r["occurred_at"]).astimezone(TZ).strftime("%Y-%m") == selected]
        buckets: dict[str, int] = {}
        for item in items:
            buckets[item["category"]] = buckets.get(item["category"], 0) + _amount_fen(item["amount"])
        categories = [{"name": name, "amount": _money(value), "transaction_ids": [i["id"] for i in items if i["category"] == name]}
                      for name, value in sorted(buckets.items(), key=lambda pair: pair[1], reverse=True)]
        anomalies: list[dict[str, Any]] = []
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                delta = abs((datetime.fromisoformat(left["occurred_at"]) - datetime.fromisoformat(right["occurred_at"])).total_seconds())
                if left["merchant"] == right["merchant"] and left["amount"] == right["amount"] and delta <= 120:
                    anomalies.append({"kind": "possible_duplicate", "merchant": left["merchant"],
                                      "amount": left["amount"], "transaction_ids": [left["id"], right["id"]],
                                      "explanation": "同商户同金额在 2 分钟内出现两笔，建议核对；系统未自动认定为误扣。"})
        total = sum(_amount_fen(item["amount"]) for item in items)
        return {"month": selected, "total": _money(total), "categories": categories,
                "anomalies": anomalies, "transactions": items, "sandbox": True,
                "source": "synthetic_transaction_records"}


class CardActionRequest(BaseModel):
    action: str


class CardActionConfirm(BaseModel):
    digest: str


def _card_action_view(row: sqlite3.Row) -> dict[str, Any]:
    from backend.banking_auth import mode
    return {"id": row["id"], "action": row["action"], "status": row["status"],
            "level": row["level"], "digest": row["digest"], "expires_at": row["expires_at"],
            "demo_code": row["demo_code"] if row["status"] == "AWAITING_VERIFICATION" and mode() == "demo" else None,
            "sandbox": True}


def _owned_card_action(db: sqlite3.Connection, user: sqlite3.Row, action_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM sandbox_card_actions WHERE id=? AND session_id=?", (action_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "卡片操作不存在")
    return row


@router.get("/card")
def card(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        row = db.execute("SELECT * FROM sandbox_cards WHERE session_id=?", (user["id"],)).fetchone()
        if row is None:
            raise HTTPException(404, "此会话没有演示卡片，请重新开始沙箱会话")
        return {"tail": row["tail"], "status": row["status"], "updated_at": row["updated_at"], "sandbox": True}


@router.post("/card/actions/prepare")
def prepare_card_action(req: CardActionRequest, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    if req.action not in {"freeze", "unfreeze", "report_lost"}:
        raise HTTPException(422, "不支持的卡片操作")
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        current = db.execute("SELECT status FROM sandbox_cards WHERE session_id=?", (user["id"],)).fetchone()["status"]
        allowed = {"freeze": {"ACTIVE"}, "unfreeze": {"FROZEN"}, "report_lost": {"ACTIVE", "FROZEN"}}
        if current not in allowed[req.action]:
            raise HTTPException(409, "当前卡片状态不允许此操作")
        action_id = secrets.token_hex(16)
        level = "red" if req.action in {"report_lost", "unfreeze"} else "yellow"
        now = _now()
        expires = now + timedelta(minutes=5)
        digest = hashlib.sha256(f"{user['id']}|{action_id}|{req.action}|{current}|{level}|{expires.isoformat()}".encode()).hexdigest()
        db.execute("INSERT INTO sandbox_card_actions(id,session_id,action,status,level,digest,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                   (action_id, user["id"], req.action, "PREPARED", level, digest, now.isoformat(), expires.isoformat()))
        _audit(db, user["id"], action_id, "CARD_ACTION_PREPARED", {"action": req.action, "level": level, "from_status": current})
        return _card_action_view(db.execute("SELECT * FROM sandbox_card_actions WHERE id=?", (action_id,)).fetchone())


@router.post("/card/actions/{action_id}/confirm")
def confirm_card_action(action_id: str, req: CardActionConfirm, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned_card_action(db, user, action_id)
        if row['action'] in {'unfreeze','report_lost'} and row['level']!='red':
            raise HTTPException(409,'此操作现在要求强验证，请重新预览并授权')
        if row["status"] != "PREPARED" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "卡片操作预览已失效")
        if not secrets.compare_digest(req.digest, row["digest"]):
            raise HTTPException(409, "确认内容与预览不一致")
        status = "AWAITING_VERIFICATION" if row["level"] == "red" else "READY"
        code = f"{secrets.randbelow(1_000_000):06d}" if row["level"] == "red" else None
        db.execute("UPDATE sandbox_card_actions SET status=?, confirmed_at=?, demo_code=? WHERE id=?", (status, _now().isoformat(), code, action_id))
        _audit(db, user["id"], action_id, "CARD_ACTION_CONFIRMED", {"action": row["action"], "next_status": status})
        return _card_action_view(db.execute("SELECT * FROM sandbox_card_actions WHERE id=?", (action_id,)).fetchone())


@router.post("/card/actions/{action_id}/verify")
def verify_card_action(action_id: str, req: VerifyTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    from backend.banking_auth import require_demo
    require_demo()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned_card_action(db, user, action_id)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        if row["status"] != "AWAITING_VERIFICATION" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "操作状态无效或已过期")
        if not row["demo_code"] or not secrets.compare_digest(req.code, row["demo_code"]):
            attempts_row = db.execute("SELECT attempts FROM sandbox_otp WHERE transfer_id=?", (action_id,)).fetchone()
            attempts = (attempts_row["attempts"] if attempts_row else 0) + 1
            db.execute("INSERT OR REPLACE INTO sandbox_otp(transfer_id,code_hash,attempts,expires_at) VALUES(?,?,?,?)",
                       (action_id, "", attempts, row["expires_at"]))
            if attempts >= 3:
                db.execute("UPDATE sandbox_sessions SET locked_until=? WHERE id=?", ((_now() + timedelta(minutes=10)).isoformat(), user["id"]))
            _audit(db, user["id"], action_id, "CARD_VERIFICATION_FAILED", {"attempts": attempts})
            return {"verified": False, "attempts_remaining": max(0, 3 - attempts), "locked": attempts >= 3}
        db.execute("UPDATE sandbox_card_actions SET status='READY', verified_at=?, demo_code=NULL WHERE id=?", (_now().isoformat(), action_id))
        db.execute("DELETE FROM sandbox_otp WHERE transfer_id=?", (action_id,))
        _audit(db, user["id"], action_id, "CARD_VERIFIED", {"method": "demo_otp"})
        return {"verified": True, "action": _card_action_view(db.execute("SELECT * FROM sandbox_card_actions WHERE id=?", (action_id,)).fetchone())}


@router.post("/card/actions/{action_id}/execute")
def execute_card_action(action_id: str, req: ExecuteTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        row = _owned_card_action(db, user, action_id)
        if row["status"] == "EXECUTED" and row["idempotency_key"] == req.idempotency_key:
            current_card = db.execute("SELECT * FROM sandbox_cards WHERE session_id=?", (user["id"],)).fetchone()
            return {"action": _card_action_view(row), "card": {"tail": current_card["tail"], "status": current_card["status"], "updated_at": current_card["updated_at"], "sandbox": True}}
        if row['action'] in {'unfreeze','report_lost'} and row['level']!='red':
            raise HTTPException(409,'此操作现在要求强验证，请重新预览并授权')
        if row["status"] != "READY" or row["expires_at"] < _now().isoformat():
            raise HTTPException(409, "卡片操作尚未确认、验证或已过期")
        if row["level"] == "red":
            from backend.banking_auth import require_grant
            require_grant(db, user["id"], "card", row)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        current = db.execute("SELECT status FROM sandbox_cards WHERE session_id=?", (user["id"],)).fetchone()["status"]
        allowed = {"freeze": {"ACTIVE"}, "unfreeze": {"FROZEN"}, "report_lost": {"ACTIVE", "FROZEN"}}
        if current not in allowed[row["action"]]:
            raise HTTPException(409, "卡片状态已变化，请重新发起")
        collision = db.execute("SELECT id FROM sandbox_card_actions WHERE session_id=? AND idempotency_key=?", (user["id"], req.idempotency_key)).fetchone()
        if collision:
            raise HTTPException(409, "幂等键已用于另一操作")
        next_state = {"freeze": "FROZEN", "unfreeze": "ACTIVE", "report_lost": "LOST"}[row["action"]]
        now = _now().isoformat()
        db.execute("UPDATE sandbox_cards SET status=?, updated_at=? WHERE session_id=?", (next_state, now, user["id"]))
        db.execute("UPDATE sandbox_card_actions SET status='EXECUTED', executed_at=?, idempotency_key=? WHERE id=?", (now, req.idempotency_key, action_id))
        _audit(db, user["id"], action_id, "CARD_ACTION_EXECUTED", {"action": row["action"], "from_status": current, "to_status": next_state})
        return {"action": _card_action_view(db.execute("SELECT * FROM sandbox_card_actions WHERE id=?", (action_id,)).fetchone()),
                "card": {"tail": "2388", "status": next_state, "updated_at": now, "sandbox": True}}


def _subscription_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "merchant": row["merchant"], "amount": _money(row["amount_fen"]),
            "next_charge_at": row["next_charge_at"], "status": row["status"], "sandbox": True}


def _subscription_action_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "subscription_id": row["subscription_id"],
            "status": row["status"], "level": "yellow", "digest": row["digest"],
            "expires_at": row["expires_at"], "sandbox": True}


def _owned_subscription_action(db: sqlite3.Connection, user: sqlite3.Row, action_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM sandbox_subscription_actions WHERE id=? AND session_id=?", (action_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "订阅操作不存在")
    return row


class QueryConsent(BaseModel):
    confirmed: bool


@router.post("/consents/subscriptions")
def confirm_subscription_query(req: QueryConsent, x_banking_session: str | None = Header(default=None)):
    if not req.confirmed:
        raise HTTPException(422, "需要明确确认查询订阅与代扣记录")
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        expires = (_now() + timedelta(minutes=10)).isoformat()
        db.execute("INSERT OR REPLACE INTO sandbox_consents VALUES(?,?,?)", (user["id"], "subscriptions_read", expires))
        _audit(db, user["id"], None, "SUBSCRIPTION_QUERY_CONFIRMED", {"expires_at": expires, "level": "yellow"})
        return {"confirmed": True, "expires_at": expires}


@router.get("/subscriptions")
def subscriptions(x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        user = _session(db, x_banking_session)
        consent = db.execute("SELECT 1 FROM sandbox_consents WHERE session_id=? AND scope='subscriptions_read' AND expires_at>?", (user["id"], _now().isoformat())).fetchone()
        if not consent:
            raise HTTPException(403, "订阅查询属于黄色操作，请先明确确认")
        rows = db.execute("SELECT * FROM sandbox_subscriptions WHERE session_id=? ORDER BY status, next_charge_at", (user["id"],)).fetchall()
        return {"items": [_subscription_view(row) for row in rows], "sandbox": True}


@router.post("/subscriptions/{subscription_id}/cancel/prepare")
def prepare_subscription_cancel(subscription_id: str, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        subscription = db.execute("SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=?", (subscription_id, user["id"])).fetchone()
        if subscription is None or subscription["status"] != "ACTIVE":
            raise HTTPException(404, "未找到可取消的模拟订阅")
        action_id = secrets.token_hex(16)
        now = _now()
        expires = now + timedelta(minutes=5)
        digest = hashlib.sha256(f"{user['id']}|{action_id}|{subscription_id}|{subscription['merchant']}|{subscription['amount_fen']}|cancel|{expires.isoformat()}".encode()).hexdigest()
        db.execute("INSERT INTO sandbox_subscription_actions(id,session_id,subscription_id,status,digest,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                   (action_id, user["id"], subscription_id, "PREPARED", digest, now.isoformat(), expires.isoformat()))
        _audit(db, user["id"], action_id, "SUBSCRIPTION_CANCEL_PREPARED", {"subscription_id": subscription_id, "merchant": subscription["merchant"]})
        return {"action": _subscription_action_view(db.execute("SELECT * FROM sandbox_subscription_actions WHERE id=?", (action_id,)).fetchone()),
                "subscription": _subscription_view(subscription)}


@router.post("/subscriptions/actions/{action_id}/confirm")
def confirm_subscription_cancel(action_id: str, req: ConfirmTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        action = _owned_subscription_action(db, user, action_id)
        if action["status"] != "PREPARED" or action["expires_at"] < _now().isoformat():
            raise HTTPException(409, "取消预览已失效")
        if not secrets.compare_digest(req.digest, action["digest"]):
            raise HTTPException(409, "确认内容与预览不一致")
        db.execute("UPDATE sandbox_subscription_actions SET status='READY',confirmed_at=? WHERE id=?", (_now().isoformat(), action_id))
        _audit(db, user["id"], action_id, "SUBSCRIPTION_CANCEL_CONFIRMED", {"subscription_id": action["subscription_id"]})
        return _subscription_action_view(db.execute("SELECT * FROM sandbox_subscription_actions WHERE id=?", (action_id,)).fetchone())


@router.post("/subscriptions/actions/{action_id}/execute")
def execute_subscription_cancel(action_id: str, req: ExecuteTransfer, x_banking_session: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = _session(db, x_banking_session)
        action = _owned_subscription_action(db, user, action_id)
        if action["status"] == "EXECUTED" and action["idempotency_key"] == req.idempotency_key:
            subscription = db.execute("SELECT * FROM sandbox_subscriptions WHERE id=?", (action["subscription_id"],)).fetchone()
            return {"action": _subscription_action_view(action), "subscription": _subscription_view(subscription)}
        if action["status"] != "READY" or action["expires_at"] < _now().isoformat():
            raise HTTPException(409, "取消操作尚未确认或已过期")
        if user["locked_until"] and user["locked_until"] > _now().isoformat():
            raise HTTPException(423, "账户暂时锁定")
        subscription = db.execute("SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=?", (action["subscription_id"], user["id"])).fetchone()
        if subscription is None or subscription["status"] != "ACTIVE":
            raise HTTPException(409, "订阅状态已变化，请重新发起")
        collision = db.execute("SELECT id FROM sandbox_subscription_actions WHERE session_id=? AND idempotency_key=?", (user["id"], req.idempotency_key)).fetchone()
        if collision:
            raise HTTPException(409, "幂等键已用于另一操作")
        now = _now().isoformat()
        db.execute("UPDATE sandbox_subscriptions SET status='CANCELLED',updated_at=? WHERE id=?", (now, subscription["id"]))
        db.execute("UPDATE sandbox_subscription_actions SET status='EXECUTED',executed_at=?,idempotency_key=? WHERE id=?", (now, req.idempotency_key, action_id))
        _audit(db, user["id"], action_id, "SUBSCRIPTION_CANCEL_EXECUTED", {"subscription_id": subscription["id"], "merchant": subscription["merchant"]})
        return {"action": _subscription_action_view(db.execute("SELECT * FROM sandbox_subscription_actions WHERE id=?", (action_id,)).fetchone()),
                "subscription": _subscription_view(db.execute("SELECT * FROM sandbox_subscriptions WHERE id=?", (subscription["id"],)).fetchone())}
