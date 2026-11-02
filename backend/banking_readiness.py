"""Local, session-scoped diagnostics. Never contacts or mutates an external service."""
import hashlib
import json
from pathlib import Path

from fastapi import Header, HTTPException
from backend import banking as bank

ROOT = Path(__file__).resolve().parents[1]


def source_hash():
    files = sorted((ROOT / "backend").glob("banking*.py")) + sorted((ROOT / "backend/tests").glob("test_banking*.py")) + [ROOT / "backend/eval/banking_release_eval.py", ROOT / "backend/rag/spatiotemporal_aml.py", ROOT / "backend/rag/temporal_patterns.py", ROOT / "backend/rag/graph_features.py"]
    files.append(ROOT / 'backend/eval/banking_crash_worker.py')
    files.extend(sorted((ROOT/'backend/eval').glob('ai_expert*')))
    files.extend(ROOT/path for path in ('backend/eval/monthly_challenge.py','backend/eval/monthly_challenge_v1.json','backend/eval/monthly_challenge_protocol_v1.json','scripts/banking_delivery.py','scripts/init_banking_demo.py','scripts/launch_banking.py'))
    return hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in files)).hexdigest()


LOADED_SOURCE = source_hash()
LOADED_AT = bank._now().isoformat()


@bank.router.get('/evaluation/external-routing')
def external_routing():
    reports = []
    for filename in ('banking77_live_v1.json', 'banking77_rules_v1.json'):
        path = ROOT / 'backend/eval/reports' / filename
        if not path.is_file():
            continue
        value = json.loads(path.read_text(encoding='utf-8'))
        reports.append({key: value[key] for key in ('generated_at', 'live_model', 'passed', 'total', 'by_category', 'no_funds_moved', 'limitations')})
    return {'reports': reports, 'scope': '外部来源英文语料的四类路由压力测试；非完整 BANKING77 分类成绩，非独立第三方认证。'}


@bank.router.get('/security/storage')
def storage_status(x_banking_session: str | None = Header(default=None)):
    from backend.banking_storage import status
    with bank._connect() as db:
        bank._session(db, x_banking_session)
        return status(db)


def report_status(filename, current_hash=None):
    path = ROOT / "backend/eval/reports" / filename
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not report.get("generated_at"):
            raise ValueError("invalid report")
        return {"status": "outdated" if current_hash and report.get("source_sha256") != current_hash else "available", "generated_at": report["generated_at"]}
    except FileNotFoundError:
        return {"status": "missing", "generated_at": None}
    except (ValueError, OSError):
        return {"status": "invalid", "generated_at": None}


@bank.router.get("/readiness")
def readiness(x_banking_session: str | None = Header(default=None)):
    from backend.banking_evidence import retrieve
    from backend.banking_external import feed_view
    from backend.banking_fx import rate_view
    from backend.banking_plaid import configured, cipher

    current = source_hash()
    with bank._connect() as db:
        user = bank._session(db, x_banking_session)
        graphs = []
        for contact in bank.CONTACTS:
            try:
                evidence = retrieve(db, user["id"], contact["id"])
                graphs.append({"recipient_id": contact["id"], "status": evidence["source_status"], "observed_at": evidence["observed_at"]})
            except HTTPException:
                graphs.append({"recipient_id": contact["id"], "status": "invalid", "observed_at": None})
        def cached(view):
            try:
                result = view(db)
                return {k: result[k] for k in ("status", "fetched_at")}
            except (ValueError, TypeError, KeyError):
                return {"status": "invalid", "fetched_at": None}
        notices, fx = cached(feed_view), cached(rate_view)
        connection = db.execute("SELECT status,updated_at FROM sandbox_plaid_connections WHERE session_id=?", (user["id"],)).fetchone()
        plaid = {"status": connection["status"].lower() if connection else "disconnected", "configured": configured(), "updated_at": connection["updated_at"] if connection else None}
        if plaid["configured"]:
            try:
                cipher()
            except HTTPException:
                plaid["status"] = "invalid_configuration"
    return {"checked_at": bank._now().isoformat(), "runtime": {"status": "current" if current == LOADED_SOURCE else "restart_required", "source_sha256": LOADED_SOURCE, "loaded_at": LOADED_AT},
            "graphs": graphs, "notices": notices, "fx": fx, "plaid": plaid,
            "evaluation": report_status("banking_release_v1.json", current), "benchmark": report_status("amlsim_recall_v2.json"),
            "scope": "本机与当前会话的状态检查；不发起外部联网验证，不代表真实银行就绪。"}
