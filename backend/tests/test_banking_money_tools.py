"""Money boundaries for statement imports, reference quotes and graph features."""
import base64
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import openpyxl
import numpy as np
from fastapi import HTTPException
from backend import banking as b, banking_statements as s, banking_fx as fx
from backend.rag.temporal_patterns import detect_temporal_patterns
from backend.rag.graph_features import extract


class MoneyToolsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "data"; root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.db = patch.object(b, "DB_PATH", Path(self.temp.name) / "test.sqlite3"); self.db.start()
        self.env = patch.dict("os.environ", {"BANKING_LLM_ENABLED": "0"}); self.env.start()
        self.token = b.create_session()["token"]
        self.seed_rates()

    def tearDown(self):
        self.env.stop(); self.db.stop(); self.temp.cleanup()

    def seed_rates(self):
        import hashlib
        rows = [{"base": "CNY", "quote": c, "rate": rate, "date": datetime.now(b.TZ).date().isoformat(), "provider_count": 1} for c, rate in [("HKD", "2"), ("USD", ".15"), ("EUR", ".13"), ("JPY", "23.61")]]
        payload = json.dumps(rows)
        with b._connect() as db:
            db.execute("INSERT INTO sandbox_public_feeds(source,payload,content_hash,fetched_at) VALUES('fx_cny',?,?,?) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload,content_hash=excluded.content_hash,fetched_at=excluded.fetched_at", (payload, hashlib.sha256(payload.encode()).hexdigest(), b._now().isoformat()))

    def preview(self, text, convention="positive_expense", token=None, extension="csv", **kwargs):
        data = text.encode("utf-8-sig") if isinstance(text, str) else text
        return s.preview(s.PreviewRequest(filename="synthetic." + extension, content_base64=base64.b64encode(data).decode(), confirmed_local_parse=True, sign_convention=convention, **kwargs), token or self.token)

    def commit(self, draft, token=None):
        return s.confirm(draft["id"], s.Confirm(digest=draft["digest"], confirmed=True), token or self.token)

    def budget(self, amount="3000", **kwargs):
        return fx.prepare(fx.BudgetRequest(currency=kwargs.pop("currency", "HKD"), foreign_amount=amount, buffer_percent=kwargs.pop("buffer_percent", "0"), snapshot_hash=fx.rates(self.token)["snapshot_hash"], **kwargs), self.token)

    def act(self, quote, action="reserve", token=None):
        return fx.action(quote["id"], action, fx.Action(digest=quote["digest"], confirmed=True), token or self.token)

    def test_csv_direction_and_currency_totals_are_independent(self):
        d = self.preview("日期,商户,金额,收支,币种\n2026-10-20,咖啡,28,支出,CNY\n2026-10-21,工资,5000,收入,CNY\n2026-10-22,酒店,100,支出,HKD\n")
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")
        self.commit(d); view = s.overview(self.token)
        self.assertEqual(view["record_count"], 3)
        totals = {r["currency"]: r for r in view["totals"]}
        self.assertEqual(Decimal(totals["CNY"]["income"]), Decimal(5000))
        self.assertEqual(Decimal(totals["CNY"]["expense"]), Decimal(28))
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_negative_expense_and_gb18030(self):
        d = self.preview("日期,商户,金额\n2026/10/20,地铁,-12.30\n".encode("gb18030"), convention="negative_expense")
        self.assertEqual(d["rows"][0]["amount"], "12.30")

    def test_repeat_file_and_confirm_are_idempotent(self):
        text = "日期,商户,金额,流水号\n2026-10-20,咖啡,20,one\n"
        d = self.preview(text); self.commit(d)
        self.assertEqual(self.commit(d)["imported_count"], 1)
        d2 = self.preview(text)
        self.assertEqual(d2["rows"][0]["preview_status"], "duplicate")
        self.assertEqual(self.commit(d2)["imported_count"], 0)

    def test_no_id_identical_charges_remain_separate_for_review(self):
        text = "日期,商户,金额\n2026-10-20,咖啡,20\n2026-10-20,咖啡,20\n"
        self.commit(self.preview(text)); self.commit(self.preview(text))
        view = s.overview(self.token)
        self.assertEqual(view["record_count"], 2)
        self.assertEqual(view["suspected_duplicates"][0]["count"], 2)

    def test_conflicting_transaction_id_never_overwrites(self):
        self.commit(self.preview("日期,商户,金额,流水号\n2026-10-20,咖啡,20,one\n"))
        conflict = self.preview("日期,商户,金额,流水号\n2026-10-20,咖啡,200,one\n")
        self.assertEqual(conflict["rows"][0]["preview_status"], "conflict")
        with self.assertRaises(HTTPException): self.commit(conflict)
        self.assertEqual(s.overview(self.token)["records"][0]["amount"], "20.00")

    def test_partial_errors_have_row_numbers(self):
        d = self.preview("日期,商户,金额\n2026-10-20,咖啡,20\ninvalid,超市,12\n2026-10-21,商户,nan\n")
        self.assertEqual(len(d["rows"]), 1); self.assertEqual([r["row"] for r in d["issues"]], [3, 4])

    def test_decimal_comma_and_closed_transaction_are_not_silently_counted(self):
        d = self.preview('日期,商户,金额,当前状态\n2026-10-20,咖啡,"12,30",支付成功\n2026-10-20,商店,100,交易关闭\n2026-10-20,正常,"1,234.50",支付成功\n')
        self.assertEqual(len(d["rows"]), 1)
        self.assertEqual(d["rows"][0]["amount"], "1234.50")
        self.assertEqual(len(d["issues"]), 2)

    def test_multisheet_requires_choice_and_formulas_are_excluded(self):
        book = openpyxl.Workbook(); ws = book.active; ws.title = "账单"
        ws.append(["日期", "商户", "金额"]); ws.append(["2026-10-20", "咖啡", 20]); ws.append(["2026-10-21", "公式", "=2+2"]); book.create_sheet("说明")
        stream = io.BytesIO(); book.save(stream); data = stream.getvalue()
        first = self.preview(data, extension="xlsx"); self.assertTrue(first["needs_sheet"])
        selected = self.preview(data, extension="xlsx", sheet="账单")
        self.assertEqual(len(selected["rows"]), 1); self.assertIn("公式", selected["issues"][0]["reason"])

    def test_invalid_xls_and_base64_return_validation_error(self):
        with self.assertRaises(HTTPException) as error: self.preview(b"invalid", extension="xls")
        self.assertEqual(error.exception.status_code, 422)
        with self.assertRaises(HTTPException): s.preview(s.PreviewRequest(filename="x.csv", content_base64="!invalid", sign_convention="positive_expense", confirmed_local_parse=True), self.token)

    def test_generated_legacy_xls_and_xlsx_have_same_records(self):
        root = Path(__file__).resolve().parents[2] / "docs/examples"
        old = self.preview((root / "statement_synthetic.xls").read_bytes(), extension="xls")
        new = self.preview((root / "statement_synthetic.xlsx").read_bytes(), extension="xlsx")
        self.assertEqual(len(old["rows"]), 3)
        self.assertEqual([r["content_signature"] for r in old["rows"]], [r["content_signature"] for r in new["rows"]])

    def test_import_ownership_digest_and_expiry(self):
        d = self.preview("日期,商户,金额\n2026-10-20,咖啡,20\n")
        other = b.create_session()["token"]
        with self.assertRaises(HTTPException): self.commit(d, other)
        with self.assertRaises(HTTPException): s.confirm(d["id"], s.Confirm(digest="wrong", confirmed=True), self.token)
        with b._connect() as db: db.execute("UPDATE statement_drafts SET expires_at='2000-01-01'")
        with self.assertRaises(HTTPException): self.commit(d)
        with b._connect() as db: self.assertEqual(db.execute("SELECT rows_json FROM statement_drafts").fetchone()[0], "[]")

    def test_erase_only_own_import_does_not_move_money(self):
        d = self.preview("日期,商户,金额\n2026-10-20,咖啡,20\n"); self.commit(d)
        s.erase(d["id"], s.Erase(confirmed=True), self.token)
        self.assertEqual(s.overview(self.token)["record_count"], 0)
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_preview_limit_can_be_recovered_by_discarding(self):
        text = "日期,商户,金额\n2026-10-20,咖啡,20\n"
        drafts = [self.preview(text) for _ in range(5)]
        with self.assertRaises(HTTPException) as error: self.preview(text)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(len(s.overview(self.token)["batches"]), 5)
        s.erase(drafts[0]["id"], s.Erase(confirmed=True), self.token)
        self.assertEqual(self.preview(text)["status"], "PREVIEW")

    def test_quote_is_read_only_reserve_release_once(self):
        q = self.budget()
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")
        self.act(q); self.act(q)
        self.assertEqual(b.overview(self.token)["balance"], "1000.00")
        self.act(q, "release"); self.act(q, "release")
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_budget_limit_and_account_balance_rechecked(self):
        q = self.budget("5000", budget_cny="1000")
        self.assertFalse(q["within_budget"])
        with self.assertRaises(HTTPException): self.act(q)
        q = self.budget("6000", budget_cny="4000")
        with self.assertRaises(HTTPException): self.act(q)

    def test_parallel_reservations_cannot_overspend(self):
        qs = [self.budget(), self.budget()]
        def run(q):
            try: return self.act(q)["status"]
            except HTTPException: return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool: result = list(pool.map(run, qs))
        self.assertEqual(result.count("RESERVED"), 1)
        self.assertEqual(b.overview(self.token)["balance"], "1000.00")

    def test_quote_ownership_and_changed_snapshot(self):
        q = self.budget(); other = b.create_session()["token"]
        with self.assertRaises(HTTPException): self.act(q, token=other)
        with b._connect() as db: db.execute("UPDATE sandbox_public_feeds SET content_hash='changed' WHERE source='fx_cny'")
        with self.assertRaises(HTTPException): self.act(q)

    def test_stale_source_blocks_new_quote(self):
        with b._connect() as db: db.execute("UPDATE sandbox_public_feeds SET fetched_at=? WHERE source='fx_cny'", ((b._now() - timedelta(days=3)).isoformat(),))
        with self.assertRaises(HTTPException): self.budget()

    def test_rate_failure_retains_last_success(self):
        previous = fx.rates(self.token)
        with patch.object(httpx.Client, "get", side_effect=httpx.ConnectError("injected outage")):
            result = fx.refresh_rates(force=True)
        self.assertEqual(result["snapshot_hash"], previous["snapshot_hash"])
        self.assertEqual(result["status"], "cached")

    def test_jpy_integer_and_affordable_rounding(self):
        with self.assertRaises(HTTPException): self.budget("100.5", currency="JPY")
        q = self.budget("1000", currency="JPY", budget_cny="100.01", buffer_percent="3", fee_cny="1")
        check = self.budget(q["affordable_foreign"], currency="JPY", budget_cny="100.01", buffer_percent="3", fee_cny="1")
        self.assertTrue(check["within_budget"])

    def test_natural_routes_and_foreign_transfer_never_becomes_cny(self):
        for text, expected in [("导入账单", "statement_import"), ("做跨境预算", "fx_budget"), ("给妈妈转100美元", "clarification")]:
            self.assertEqual(b.interpret_intent(b.IntentRequest(text=text), self.token)["kind"], expected)


class TemporalFeaturesTests(unittest.TestCase):
    def graph(self, rows):
        epoch = datetime(2020, 1, 1, tzinfo=timezone.utc)
        ids = sorted({n for a, z, _, _ in rows for n in (a, z)})
        return {"nodes": [{"id": n, "labels": ["Account"], "properties": {}} for n in ids], "edges": [{"id": str(i), "source": a, "target": z, "type": "TRANSFER", "properties": {"amount": amount, "date": (epoch + timedelta(hours=hour)).isoformat()}} for i, (a, z, amount, hour) in enumerate(rows)]}

    def test_causal_cycle_and_exact_source_ids(self):
        graph = self.graph([("a", "b", 300, 0), ("b", "c", 290, 1), ("c", "a", 280, 2)])
        result = detect_temporal_patterns(graph)
        cycles = [r for r in result["findings"] if r["code"] == "TEMPORAL_CYCLE"]
        self.assertEqual(len(cycles), 1); self.assertEqual(set(cycles[0]["source_ids"]), {"0", "1", "2"})

    def test_same_topology_but_impossible_temporal_order_is_rejected(self):
        graph = self.graph([("a", "b", 300, 0), ("b", "c", 300, 2), ("c", "a", 300, 1)])
        self.assertFalse(any(r["code"] == "TEMPORAL_CYCLE" for r in detect_temporal_patterns(graph)["findings"]))

    def test_fanin_does_not_require_outgoing_and_retains_participants(self):
        graph = self.graph([("a", "hub", 300, 0), ("b", "hub", 310, 1), ("c", "hub", 320, 2)])
        finding = detect_temporal_patterns(graph)["findings"][0]
        self.assertEqual(set(finding["node_ids"]), {"a", "b", "c", "hub"})

    def test_features_ignore_labels_and_account_id_numbers(self):
        graph = self.graph([("1", "2", 300, 0), ("2", "3", 290, 1)])
        ids, matrix = extract(graph)
        for node in graph["nodes"]: node["properties"] = {"isFraud": True, "fraudStep": 100}
        _, changed = extract(graph)
        np.testing.assert_array_equal(matrix, changed)
        mapping = {n: "x" + n for n in ids}
        for node in graph["nodes"]: node["id"] = mapping[node["id"]]
        for edge in graph["edges"]: edge["source"], edge["target"] = mapping[edge["source"]], mapping[edge["target"]]
        _, renamed = extract(graph); np.testing.assert_array_equal(matrix, renamed)


if __name__ == "__main__": unittest.main()
