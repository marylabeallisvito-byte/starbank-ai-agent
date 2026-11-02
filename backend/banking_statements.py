"""Local-only statement parsing, preview and explicit idempotent import."""
from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
import secrets
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from backend import banking as b

MAX_BYTES, MAX_ROWS = 2 * 1024 * 1024, 5000
CURRENCIES = {"CNY", "USD", "HKD", "EUR", "JPY", "GBP", "SGD", "AUD", "CAD", "CHF"}
ALIASES = {"date": {"日期", "交易日期", "交易时间", "交易创建时间", "date", "datetime"}, "merchant": {"商户", "交易对方", "交易对方名称", "摘要", "merchant", "name"}, "amount": {"金额", "金额元", "交易金额", "收支金额", "amount"}, "direction": {"收支", "收支类型", "direction", "收支方向"}, "currency": {"币种", "currency"}, "external_id": {"交易单号", "流水号", "交易流水号", "订单号", "transactionid"}, "status": {"交易状态", "当前状态", "status"}}


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS statement_drafts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
      filename TEXT NOT NULL,source TEXT NOT NULL,file_hash TEXT NOT NULL,digest TEXT NOT NULL,
      rows_json TEXT NOT NULL,issues_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
      status TEXT NOT NULL,imported_count INTEGER NOT NULL DEFAULT 0,duplicate_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS statement_records(session_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
      batch_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(session_id,fingerprint));
    """)
    db.execute("UPDATE statement_drafts SET rows_json='[]',issues_json='[]',filename='预览已过期',status='EXPIRED' WHERE status='PREVIEW' AND expires_at<?", (b._now().isoformat(),))


class PreviewRequest(BaseModel):
    filename: str = Field(max_length=180)
    content_base64: str = Field(max_length=2_800_000)
    confirmed_local_parse: bool
    source: Literal["generic", "alipay", "wechat"] = "generic"
    sign_convention: Literal["positive_expense", "negative_expense"]
    currency: str = "CNY"
    sheet: str | None = Field(default=None, max_length=100)


class Confirm(BaseModel):
    digest: str
    confirmed: bool


def header_key(value): return re.sub(r"[\s/()（）_\-]", "", str(value)).casefold()
def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_rows(data, extension, sheet=None):
    if extension == "csv":
        text = None
        for encoding in ("utf-8-sig", "gb18030"):
            try: text = data.decode(encoding); break
            except UnicodeDecodeError: pass
        if text is None: raise ValueError("CSV 编码无法识别，请导出 UTF-8 或 GB18030")
        try: dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
        except csv.Error: dialect = csv.excel
        rows = []
        for row in csv.reader(io.StringIO(text), dialect):
            rows.append([(value, False) for value in row])
            if len(rows) > MAX_ROWS + 50: raise ValueError("文件超过五千条记录")
        return rows, []
    if extension == "xlsx":
        import openpyxl
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(m.file_size for m in members) > 15_000_000: raise ValueError("Excel 解压体积超过安全上限")
            if any("vbaProject" in m.filename for m in members): raise ValueError("不支持包含宏的工作簿")
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            names = book.sheetnames
            if len(names) > 1 and sheet is None: return None, names
            selected = sheet or names[0]
            if selected not in names: raise ValueError("工作表不存在")
            rows = []
            for row in book[selected].iter_rows():
                if len(row) > 50: raise ValueError("工作表超过五十列")
                rows.append([(c.value, c.data_type == "f") for c in row])
                if len(rows) > MAX_ROWS + 50: raise ValueError("文件超过五千条记录")
            return rows, names
        finally: book.close()
    if extension == "xls":
        import xlrd
        try: book = xlrd.open_workbook(file_contents=data, on_demand=True)
        except xlrd.biffh.XLRDError: raise ValueError("XLS 文件无法解析，请重新导出") from None
        try:
            names = book.sheet_names()
            if len(names) > 1 and sheet is None: return None, names
            selected = sheet or names[0]
            if selected not in names: raise ValueError("工作表不存在")
            ws = book.sheet_by_name(selected)
            if ws.nrows > MAX_ROWS + 50 or ws.ncols > 50: raise ValueError("工作表超过行列上限")
            return [[(xlrd.xldate.xldate_as_datetime(c.value, book.datemode) if c.ctype == xlrd.XL_CELL_DATE else c.value, False) for c in ws.row(i)] for i in range(ws.nrows)], names
        finally: book.release_resources()
    raise ValueError("仅支持 CSV、XLSX、XLS；不解析 PDF 或图片")


def normalized_date(value):
    if isinstance(value, datetime): return value.isoformat(timespec="seconds")
    if isinstance(value, date): return value.isoformat()
    text = str(value).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.date().isoformat() if fmt == "%Y-%m-%d" else dt.isoformat(timespec="seconds")
        except ValueError: pass
    try: return datetime.fromisoformat(text).isoformat(timespec="seconds")
    except ValueError: raise ValueError("日期格式无法识别") from None


def category(merchant, amount):
    if Decimal(amount) < 0: return "收入"
    for name, words in [("餐饮", ("餐", "咖啡", "外卖", "KFC", "麦当劳")), ("交通", ("地铁", "出租", "Uber", "铁路", "公交")), ("购物", ("商城", "超市", "淘宝", "京东")), ("生活", ("水费", "电费", "燃气", "房租"))]:
        if any(w.casefold() in merchant.casefold() for w in words): return name
    return "其他"


def normalize(rows, req):
    mapping, header_index = {}, None
    for i, row in enumerate(rows[:50]):
        candidate = {}
        for column, (value, _) in enumerate(row):
            key = header_key(value)
            for field, aliases in ALIASES.items():
                if key in aliases and field not in candidate: candidate[field] = column
        if all(k in candidate for k in ("date", "merchant", "amount")):
            mapping, header_index = candidate, i; break
    if header_index is None: raise ValueError("未找到日期、商户、金额列；请参考标准模板调整列名")
    valid, issues, occurrences = [], [], Counter()
    for index, row in enumerate(rows[header_index + 1:], header_index + 2):
        if not any(v is not None and str(v).strip() for v, _ in row): continue
        try:
            if len(row) > 50 or any(len(str(v)) > 1000 for v, _ in row): raise ValueError("行内容超过长度上限")
            if any(formula for _, formula in row): raise ValueError("包含公式，未执行；请改为数值后重新导入")
            def get(key, fallback=""):
                column = mapping.get(key)
                return row[column][0] if column is not None and column < len(row) and row[column][0] is not None else fallback
            merchant = str(get("merchant")).strip()
            if not merchant or len(merchant) > 150: raise ValueError("商户名称缺失或过长")
            if merchant.startswith(("=", "@")): raise ValueError("商户字段疑似公式文本，未导入")
            status = str(get("status")).strip().casefold()
            if status and status not in ("交易成功", "支付成功", "已收钱", "对方已收钱", "已存入零钱", "成功", "已入账", "posted", "completed", "success"):
                raise ValueError("交易状态未确认成功或包含退款，请核对并转换为标准记录后导入")
            currency = str(get("currency", req.currency)).upper().strip()
            currency = {"人民币": "CNY", "美元": "USD", "港币": "HKD", "日元": "JPY"}.get(currency, currency)
            if currency not in CURRENCIES: raise ValueError("币种不在支持范围")
            raw_amount = str(get("amount"))
            if ("€" in raw_amount and currency != "EUR") or ("£" in raw_amount and currency != "GBP") or ("$" in raw_amount and currency not in {"USD", "HKD", "AUD", "CAD", "SGD"}): raise ValueError("金额符号与所选币种不一致")
            text = re.sub(r"[¥￥$€£\s]", "", raw_amount).replace("，", ",")
            if "," in text and not re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d{1,2})?", text): raise ValueError("逗号格式有歧义，请改为小数点金额")
            text = text.replace(",", "")
            if not re.fullmatch(r"[+-]?\d+(\.\d{1,2})?", text): raise ValueError("金额格式无法识别")
            amount = Decimal(text)
            if not amount.is_finite() or abs(amount) > Decimal("1000000000") or amount == 0 or amount.quantize(Decimal(".01")) != amount: raise ValueError("金额须非零、有限且最多两位小数")
            direction = str(get("direction")).strip().casefold()
            if direction:
                if direction in ("支出", "支", "expense", "debit", "out"): amount = abs(amount)
                elif direction in ("收入", "收", "income", "credit", "in"): amount = -abs(amount)
                else: raise ValueError("收支方向不明确或属于不计收支，请核对")
            elif req.sign_convention == "negative_expense": amount = -amount
            when = normalized_date(get("date"))
            external_id = str(get("external_id")).strip()
            signature = {"date": when, "merchant": merchant, "amount": f"{amount:.2f}", "currency": currency, "source": req.source}
            identity = {"source": req.source, "currency": currency, "external_id": external_id} if external_id else signature
            key = fingerprint(identity)
            occurrences[key] += 1
            # Identical no-ID charges within one file remain separate occurrences.
            identifier = key if external_id else fingerprint([key, occurrences[key]])
            valid.append({**signature, "fingerprint": identifier, "content_signature": fingerprint(signature), "category": category(merchant, str(amount)), "row": index})
        except (ValueError, InvalidOperation) as error:
            detail = str(error)
            issues.append({"row": index, "reason": detail if detail and len(detail) < 150 and not detail.startswith("[") else "金额或行数据无法识别"})
    if len(valid) + len(issues) > MAX_ROWS: raise ValueError("数据记录超过五千行")
    return valid, issues, {k: str(rows[header_index][v][0]) for k, v in mapping.items()}


def draft_view(row):
    return {"id": row["id"], "filename": row["filename"], "digest": row["digest"], "expires_at": row["expires_at"], "status": row["status"],
            "rows": json.loads(row["rows_json"]), "issues": json.loads(row["issues_json"]), "imported_count": row["imported_count"], "duplicate_count": row["duplicate_count"]}


@b.router.post("/statements/preview")
def preview(req: PreviewRequest, x_banking_session: str | None = Header(default=None)):
    if not req.confirmed_local_parse: raise HTTPException(422, "请确认仅在本机服务解析此文件")
    with b._connect() as db: sid = b._session(db, x_banking_session)["id"]
    try:
        data = base64.b64decode(req.content_base64, validate=True)
        if not data or len(data) > MAX_BYTES: raise ValueError("文件须非空且不超过 2 MB")
        filename = re.split(r"[/\\]", req.filename)[-1]
        extension = filename.rsplit(".", 1)[-1].lower()
        rows, sheets = read_rows(data, extension, req.sheet)
        if rows is None: return {"needs_sheet": True, "sheet_names": sheets}
        valid, issues, mapping = normalize(rows, req)
    except (ValueError, binascii.Error, zipfile.BadZipFile, KeyError, OSError, IndexError, SyntaxError) as error:
        raise HTTPException(422, str(error)[:180] or "文件无法解析，请使用标准模板") from None
    now = b._now(); identifier = secrets.token_hex(16)
    digest = fingerprint({"session": sid, "id": identifier, "rows": valid, "issues": issues})
    with b._connect() as db:
        db.execute("BEGIN IMMEDIATE"); b._session(db, x_banking_session)
        if db.execute("SELECT COUNT(*) FROM statement_drafts WHERE session_id=? AND status='PREVIEW'", (sid,)).fetchone()[0] >= 5:
            raise HTTPException(429, "当前有五份未确认预览，请先清除或等待过期后重试")
        seen = {}
        for record in valid:
            previous = db.execute("SELECT payload FROM statement_records WHERE session_id=? AND fingerprint=?", (sid, record["fingerprint"])).fetchone()
            prior = json.loads(previous["payload"]) if previous else seen.get(record["fingerprint"])
            record["preview_status"] = "conflict" if prior and prior.get("content_signature") != record["content_signature"] else "duplicate" if prior else "new"
            if record["preview_status"] == "conflict": issues.append({"row": record["row"], "reason": "相同流水号对应不同记录，暂不导入；请核对来源"})
            seen.setdefault(record["fingerprint"], record)
        db.execute("INSERT INTO statement_drafts(id,session_id,filename,source,file_hash,digest,rows_json,issues_json,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identifier, sid, filename, req.source, hashlib.sha256(data).hexdigest(), digest, canonical(valid), canonical(issues), now.isoformat(), (now + timedelta(minutes=20)).isoformat(), "PREVIEW"))
        b._audit(db, sid, None, "STATEMENT_PREVIEW", {"draft_id": identifier, "valid_rows": len(valid), "issue_rows": len(issues)})
        result = draft_view(db.execute("SELECT * FROM statement_drafts WHERE id=?", (identifier,)).fetchone())
        return {**result, "mapping": mapping, "sheet_names": sheets, "needs_sheet": False, "raw_file_retained": False}


@b.router.post("/statements/{draft_id}/confirm")
def confirm(draft_id: str, req: Confirm, x_banking_session: str | None = Header(default=None)):
    with b._connect() as db:
        db.execute("BEGIN IMMEDIATE"); sid = b._session(db, x_banking_session)["id"]
        row = db.execute("SELECT * FROM statement_drafts WHERE id=? AND session_id=?", (draft_id, sid)).fetchone()
        if not row: raise HTTPException(404, "导入预览不存在")
        if not req.confirmed or req.digest != row["digest"]: raise HTTPException(409, "请明确确认当前预览摘要")
        if row["status"] == "IMPORTED": return draft_view(row)
        if row["status"] != "PREVIEW" or row["expires_at"] < b._now().isoformat(): raise HTTPException(409, "预览已过期或已清除，请重新上传")
        records = [r for r in json.loads(row["rows_json"]) if r.get("preview_status") != "conflict"]
        if not records: raise HTTPException(422, "没有可导入记录，请先修正错误行")
        existing = {r[0] for r in db.execute("SELECT fingerprint FROM statement_records WHERE session_id=?", (sid,))}
        if len(existing | {r["fingerprint"] for r in records}) > 20000:
            raise HTTPException(409, "当前会话最多保存两万条导入记录，请先清除旧批次")
        added = 0
        for record in records:
            added += db.execute("INSERT OR IGNORE INTO statement_records VALUES(?,?,?,?)", (sid, record["fingerprint"], draft_id, canonical(record))).rowcount
        db.execute("UPDATE statement_drafts SET status='IMPORTED',imported_count=?,duplicate_count=? WHERE id=?", (added, len(records) - added, draft_id))
        b._audit(db, sid, None, "STATEMENT_IMPORTED", {"draft_id": draft_id, "added": added, "duplicates": len(records) - added})
        return draft_view(db.execute("SELECT * FROM statement_drafts WHERE id=?", (draft_id,)).fetchone())


class Erase(BaseModel): confirmed: bool


@b.router.post("/statements/{draft_id}/erase")
def erase(draft_id: str, req: Erase, x_banking_session: str | None = Header(default=None)):
    if not req.confirmed: raise HTTPException(422, "请确认清除该批导入记录")
    with b._connect() as db:
        db.execute("BEGIN IMMEDIATE"); sid = b._session(db, x_banking_session)["id"]
        row = db.execute("SELECT id FROM statement_drafts WHERE id=? AND session_id=?", (draft_id, sid)).fetchone()
        if not row: raise HTTPException(404, "导入批次不存在")
        db.execute("DELETE FROM statement_records WHERE session_id=? AND batch_id=?", (sid, draft_id))
        db.execute("UPDATE statement_drafts SET rows_json='[]',issues_json='[]',filename='已清除',status='ERASED' WHERE id=?", (draft_id,))
        from backend.banking_mission_evidence import redact_batch
        redact_batch(db,sid,draft_id)
        b._audit(db, sid, None, "STATEMENT_ERASED", {"draft_id": draft_id})
    return {"status": "ERASED"}


@b.router.get("/statements")
def overview(x_banking_session: str | None = Header(default=None)):
    with b._connect() as db:
        sid = b._session(db, x_banking_session)["id"]
        records = [json.loads(row["payload"]) for row in db.execute("SELECT payload FROM statement_records WHERE session_id=?", (sid,))]
        batches = [dict(r) for r in db.execute("SELECT id,filename,status,created_at,imported_count,duplicate_count FROM statement_drafts WHERE session_id=? AND status IN ('IMPORTED','PREVIEW') ORDER BY created_at DESC LIMIT 50", (sid,))]
    totals, categories = {}, defaultdict(lambda: defaultdict(Decimal))
    suspects, grouped = [], defaultdict(list)
    for row in records:
        currency = row["currency"]; amount = Decimal(row["amount"])
        totals.setdefault(currency, {"expense": Decimal(0), "income": Decimal(0), "count": 0})
        totals[currency]["expense" if amount > 0 else "income"] += abs(amount); totals[currency]["count"] += 1
        if amount > 0:
            categories[currency][row["category"]] += amount
            grouped[(row["merchant"], currency, row["amount"], row["date"][:10])].append(row["fingerprint"])
    for (merchant, currency, amount, day), ids in grouped.items():
        if len(ids) > 1: suspects.append({"merchant": merchant, "currency": currency, "amount": amount, "date": day, "count": len(ids), "record_ids": ids})
    return {"records": sorted(records, key=lambda r: r["date"], reverse=True)[:100], "record_count": len(records), "batches": batches,
            "totals": [{"currency": key, "expense": str(v["expense"]), "income": str(v["income"]), "count": v["count"], "categories": [{"name": k, "amount": str(a)} for k, a in categories[key].items()]} for key, v in totals.items()],
            "suspected_duplicates": suspects[:20], "scope": "导入记录只用于独立账单分析，不改变银行余额，不自动认定重复扣款。"}
