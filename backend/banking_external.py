"""Official public notices and read-only external sandbox data boundaries."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Header, HTTPException

from backend import banking as bank

HKMA_URL = "https://api.hkma.gov.hk/public/scam-alert?lang=sc&pagesize=30"


def schema(db):
    # Called by bank._connect before any business transaction begins.
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sandbox_public_feeds(
      source TEXT PRIMARY KEY,payload TEXT,content_hash TEXT,fetched_at TEXT,
      attempted_at TEXT,last_error TEXT);
    CREATE TABLE IF NOT EXISTS sandbox_plaid_connections(
      session_id TEXT PRIMARY KEY,id TEXT NOT NULL,token_cipher TEXT,cursor TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,accounts_json TEXT NOT NULL DEFAULT '[]',updated_at TEXT NOT NULL,
      generation INTEGER NOT NULL DEFAULT 0,last_error TEXT,consent_at TEXT);
    CREATE TABLE IF NOT EXISTS sandbox_plaid_transactions(
      session_id TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
      PRIMARY KEY(session_id,id));
    """)


def parse_notices(raw):
    if raw.get("header", {}).get("success") is not True:
        raise ValueError("source rejected request")
    records = raw.get("result", {}).get("records")
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("invalid record list")
    rows = {}
    for row in records:
        link = row.get("link", "")
        url = urlparse(link)
        if url.scheme != "https" or url.hostname != "www.hkma.gov.hk" or not re.match(r"^/(gb_chi|eng|chi)/news-and-media/(scam-alert|press-releases)/", url.path) or url.username or url.password:
            raise ValueError("untrusted source link")
        published = date.fromisoformat(row["date"]).isoformat()
        title = re.sub(r"[\x00-\x1f\x7f]", "", html.unescape(str(row["title"]))).strip()
        if not title or len(title) > 1000:
            raise ValueError("invalid source title")
        identifier = "HKMA-" + hashlib.sha256(link.encode()).hexdigest()[:16]
        rows[identifier] = {"id": identifier, "title": title, "published_at": published, "url": link, "source": "香港金融管理局"}
    if not rows:
        raise ValueError("empty feed; preserve last known good snapshot")
    return sorted(rows.values(), key=lambda r: (r["published_at"], r["id"]), reverse=True)


def feed_view(db):
    row = db.execute("SELECT * FROM sandbox_public_feeds WHERE source='hkma'").fetchone()
    items = json.loads(row["payload"]) if row and row["payload"] else []
    fetched = row["fetched_at"] if row else None
    age = (bank._now() - datetime.fromisoformat(fetched)).total_seconds() if fetched else None
    return {"source": "香港金融管理局", "source_url": HKMA_URL, "data_kind": "official_public_notices",
            "status": "unavailable" if not items else "stale" if age > 21600 else "cached" if row["last_error"] else "fresh",
            "fetched_at": fetched, "last_attempt_at": row["attempted_at"] if row else None,
            "last_error": row["last_error"] if row else None, "content_hash": row["content_hash"] if row else None,
            "items": items, "scope": "官方公开通知索引，不是收款账户黑名单；未命中不代表安全。"}


def refresh_hkma(force=False):
    now = bank._now().isoformat()
    with bank._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT attempted_at FROM sandbox_public_feeds WHERE source='hkma'").fetchone()
        if not force and previous and previous["attempted_at"] > (bank._now() - timedelta(seconds=60)).isoformat():
            return feed_view(db)
        db.execute("INSERT INTO sandbox_public_feeds(source,attempted_at) VALUES('hkma',?) ON CONFLICT(source) DO UPDATE SET attempted_at=excluded.attempted_at", (now,))
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get(HKMA_URL)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("oversize feed")
            rows = parse_notices(response.json())
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        with bank._connect() as db:
            db.execute("UPDATE sandbox_public_feeds SET payload=?,content_hash=?,fetched_at=?,last_error=NULL WHERE source='hkma' AND attempted_at=?",
                       (payload, hashlib.sha256(payload.encode()).hexdigest(), bank._now().isoformat(), now))
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        with bank._connect() as db:
            db.execute("UPDATE sandbox_public_feeds SET last_error=? WHERE source='hkma' AND attempted_at=?", ("官方来源暂不可用；保留最近成功快照", now))
    with bank._connect() as db:
        return feed_view(db)


@bank.router.get("/external/notices")
def notices(q: str = "", x_banking_session: str | None = Header(default=None)):
    if len(q) > 100:
        raise HTTPException(422, "检索词最多一百字")
    with bank._connect() as db:
        bank._session(db, x_banking_session)
        result = feed_view(db)
    terms = q.strip().casefold().split()
    result["total"] = len(result["items"])
    result["items"] = [r for r in result["items"] if all(term in r["title"].casefold() for term in terms)]
    result["query"] = q.strip()
    return result


@bank.router.post("/external/notices/refresh")
def refresh_notices(x_banking_session: str | None = Header(default=None)):
    with bank._connect() as db:
        user = bank._session(db, x_banking_session)
        bank._audit(db, user["id"], None, "PUBLIC_FEED_REFRESH_REQUESTED", {"source": "HKMA"})
    return refresh_hkma()


@bank.router.get("/external/benchmark")
def benchmark(x_banking_session: str | None = Header(default=None)):
    with bank._connect() as db:
        bank._session(db, x_banking_session)
    path = Path(__file__).resolve().parent / "eval/reports/amlsim_external_v1.json"
    if not path.is_file():
        raise HTTPException(404, "尚未运行 AMLSim 外部样本评测")
    return json.loads(path.read_text(encoding="utf-8"))


@bank.router.get("/external/benchmark/recall")
def recall_benchmark(x_banking_session: str | None = Header(default=None)):
    with bank._connect() as db: bank._session(db, x_banking_session)
    path = Path(__file__).resolve().parent / "eval/reports/amlsim_recall_v2.json"
    if not path.is_file(): raise HTTPException(404, "新版召回评测尚未生成，请先冻结模型后运行验证")
    return json.loads(path.read_text(encoding="utf-8"))


@bank.router.get("/external/benchmark/robustness")
def robustness_benchmark(x_banking_session: str | None = Header(default=None)):
    with bank._connect() as db: bank._session(db, x_banking_session)
    path = Path(__file__).resolve().parent / "eval/reports/amlsim_robustness_v1.json"
    if not path.is_file(): raise HTTPException(404, "尚未生成冻结模型敏感性诊断")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not report.get("scenarios"): raise ValueError("invalid report")
        return report
    except (ValueError, OSError):
        raise HTTPException(503, "敏感性诊断文件无法校验，请重新生成") from None


if __name__ == "__main__":
    result = refresh_hkma(force=True)
    print(json.dumps({"status": result["status"], "records": len(result["items"]), "fetched_at": result["fetched_at"]}))
