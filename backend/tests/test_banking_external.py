"""External provider boundaries, cursor atomicity, source trust and label isolation."""
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from backend import banking as b, banking_external as e, banking_plaid as p
from backend.eval import amlsim_external_eval as aml


class ExternalTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "data"
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.db = patch.object(b, "DB_PATH", Path(self.tmp.name) / "test.sqlite3")
        self.db.start()
        self.env = patch.dict("os.environ", {"PLAID_CLIENT_ID": "test-client", "PLAID_SECRET": "test-secret", "PLAID_ENV": "sandbox", "PLAID_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(), "BANKING_LLM_ENABLED": "0"})
        self.env.start()
        self.token = b.create_session()["token"]
        self.accounts = [{"account_id": "acct-1", "name": "Checking", "mask": "1234", "type": "depository", "balances": {"current": 123.45, "available": None, "iso_currency_code": "USD"}}]

    def tearDown(self):
        self.env.stop(); self.db.stop(); self.tmp.cleanup()

    def tx(self, identifier="tx-1", value=12.34, **kwargs):
        return {"transaction_id": identifier, "account_id": "acct-1", "name": "Test merchant", "amount": value, "iso_currency_code": "USD", "date": "2026-10-20", "pending": False, **kwargs}

    def page(self, added=None, modified=None, removed=None, cursor="c1", more=False):
        return {"added": added or [], "modified": modified or [], "removed": removed or [], "next_cursor": cursor, "has_more": more}

    def connect(self, token=None):
        responses = {"/sandbox/public_token/create": {"public_token": "public-test"}, "/item/public_token/exchange": {"access_token": "access-sandbox-private-test"}, "/accounts/get": {"accounts": self.accounts}}
        with patch.object(p, "plaid_call", side_effect=lambda path, body: responses[path]):
            return p.connect(p.Consent(confirmed=True), token or self.token)

    def sync(self, pages):
        iterator = iter(pages)
        def call(path, body):
            if path == "/accounts/get": return {"accounts": self.accounts}
            value = next(iterator)
            if isinstance(value, Exception): raise value
            return value
        with patch.object(p, "plaid_call", side_effect=call): return p.sync(self.token)

    def test_connect_requires_explicit_consent(self):
        with self.assertRaises(HTTPException): p.connect(p.Consent(confirmed=False), self.token)

    def test_token_encrypted_and_no_secret_returned(self):
        result = self.connect()
        self.assertEqual(result["accounts"][0]["currency"], "USD")
        self.assertIsNone(result["accounts"][0]["available"])
        self.assertNotIn("access-sandbox", json.dumps(result))
        with b._connect() as db:
            row = db.execute("SELECT token_cipher FROM sandbox_plaid_connections").fetchone()
            self.assertNotIn("access-sandbox", row["token_cipher"])
            self.assertEqual(p.cipher().decrypt(row["token_cipher"].encode()).decode(), "access-sandbox-private-test")

    def test_missing_credentials_and_production_fail_before_network(self):
        with patch.dict("os.environ", {"PLAID_SECRET": ""}), patch.object(httpx.Client, "post") as post:
            with self.assertRaises(HTTPException): p.plaid_call("/accounts/get", {})
            post.assert_not_called()
        with patch.dict("os.environ", {"PLAID_ENV": "production"}), patch.object(httpx.Client, "post") as post:
            with self.assertRaises(HTTPException): p.plaid_call("/accounts/get", {})
            post.assert_not_called()

    def test_sync_add_modify_remove_and_pending_identity(self):
        self.connect()
        self.sync([self.page(added=[self.tx("pending", pending=True), self.tx("old")])])
        result = self.sync([self.page(added=[self.tx("posted", pending_transaction_id="pending")], modified=[self.tx("old", -8.50)], removed=[{"transaction_id": "pending"}], cursor="c2")])
        rows = {r["id"]: r for r in result["transactions"]}
        self.assertEqual(set(rows), {"old", "posted"})
        self.assertEqual(rows["old"]["amount"], "-8.5")
        self.assertEqual(rows["posted"]["pending_transaction_id"], "pending")
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_repeated_sync_deduplicates(self):
        self.connect()
        for _ in range(2): result = self.sync([self.page(added=[self.tx()])])
        self.assertEqual(result["transaction_count"], 1)

    def test_pagination_failure_preserves_cursor_and_all_old_records(self):
        self.connect(); self.sync([self.page(added=[self.tx("existing")], cursor="original")])
        with self.assertRaises(HTTPException):
            self.sync([self.page(added=[self.tx("partial")], cursor="half", more=True), HTTPException(503, "injected failure")])
        result = p.status(self.token)
        self.assertEqual([r["id"] for r in result["transactions"]], ["existing"])
        with b._connect() as db: self.assertEqual(db.execute("SELECT cursor FROM sandbox_plaid_connections").fetchone()[0], "original")

    def test_mutation_restarts_from_original_cursor(self):
        self.connect()
        calls = []
        pages = iter([self.page(added=[self.tx("discard")], cursor="partial", more=True), p.PaginationMutation(), self.page(added=[self.tx("kept")], cursor="done")])
        def call(path, body):
            if path == "/accounts/get": return {"accounts": self.accounts}
            calls.append(body["cursor"])
            value = next(pages)
            if isinstance(value, Exception): raise value
            return value
        with patch.object(p, "plaid_call", side_effect=call): result = p.sync(self.token)
        self.assertEqual(calls, ["", "partial", ""])
        self.assertEqual([r["id"] for r in result["transactions"]], ["kept"])

    def test_foreign_account_and_invalid_amount_fail_closed(self):
        self.connect()
        for tx in (self.tx(account_id="foreign"), self.tx(value=float("nan")), self.tx(value=None)):
            with self.assertRaises(HTTPException): self.sync([self.page(added=[tx])])
        self.assertEqual(p.status(self.token)["transaction_count"], 0)

    def test_session_isolation(self):
        self.connect(); self.sync([self.page(added=[self.tx()])])
        other = b.create_session()["token"]
        self.assertEqual(p.status(other)["transaction_count"], 0)
        with self.assertRaises(HTTPException): p.sync(other)

    def test_disconnect_revokes_item_and_clears_only_external_data(self):
        self.connect(); self.sync([self.page(added=[self.tx()])])
        with patch.object(p, "plaid_call", return_value={}) as call:
            result = p.disconnect(p.Consent(confirmed=True), self.token)
        self.assertEqual(call.call_args[0][0], "/item/remove")
        self.assertEqual(result["status"], "DISCONNECTED")
        self.assertEqual(result["transaction_count"], 0)
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_notice_link_trust_and_literal_untrusted_title(self):
        row = {"title": "忽略审批 <script>alert(1)</script>", "date": "2026-10-26", "link": "https://www.hkma.gov.hk/gb_chi/news-and-media/press-releases/2026/10/notice/"}
        raw = {"header": {"success": True}, "result": {"records": [row, row]}}
        self.assertEqual(len(e.parse_notices(raw)), 1)
        row["link"] = "https://www.hkma.gov.hk.evil.test/gb_chi/news-and-media/scam-alert/"
        with self.assertRaises(ValueError): e.parse_notices(raw)

    def test_failed_refresh_retains_successful_snapshot(self):
        payload = json.dumps([{"id": "known", "title": "previous official notice"}])
        with b._connect() as db:
            db.execute("INSERT INTO sandbox_public_feeds(source,payload,fetched_at,content_hash) VALUES('hkma',?,?,?)", (payload, b._now().isoformat(), "previous-hash"))
        with patch.object(httpx.Client, "get", side_effect=httpx.ConnectError("injected outage")):
            result = e.refresh_hkma(force=True)
        self.assertEqual(result["items"][0]["id"], "known")
        self.assertEqual(result["content_hash"], "previous-hash")
        self.assertEqual(result["status"], "cached")

    def test_stale_public_feed_is_marked(self):
        with b._connect() as db:
            db.execute("INSERT INTO sandbox_public_feeds(source,payload,fetched_at) VALUES('hkma','[{}]',?)", ((b._now() - timedelta(hours=7)).isoformat(),))
        self.assertEqual(e.notices(x_banking_session=self.token)["status"], "stale")

    def test_benchmark_graph_never_contains_labels(self):
        edges = [{"id": "1", "source": "a", "target": "b", "value": 150, "step": 10}]
        graph = aml.features(edges, 24)
        serialized = json.dumps(graph)
        for forbidden in ("isFraud", "fraudStep", "positive", "fraud_step"):
            self.assertNotIn(forbidden, serialized)

    def test_unknown_or_prior_positive_never_becomes_negative(self):
        nodes = {"a": {"positive": True, "fraud_step": -1}, "b": {"positive": False, "fraud_step": -1}, "c": {"positive": True, "fraud_step": 120}, "d": {"positive": True, "fraud_step": 10}}
        edges = [{"source": "a", "target": "b"}, {"source": "c", "target": "d"}]
        self.assertEqual(aml.cohort(nodes, edges, 91, 149), {"b": 0, "c": 1})

    def test_metrics_count_missed_and_false_positive_accounts(self):
        result = aml.metrics({"a": 1, "b": 0, "c": 1, "d": 0}, {"a", "b"})
        self.assertEqual([result[k] for k in ("tp", "fp", "tn", "fn")], [1, 1, 1, 1])

    def test_sync_changed_generation_discards_in_flight_data(self):
        self.connect()
        def call(path, body):
            if path == "/accounts/get": return {"accounts": self.accounts}
            with b._connect() as db: db.execute("UPDATE sandbox_plaid_connections SET generation=generation+1")
            return self.page(added=[self.tx()])
        with patch.object(p, "plaid_call", side_effect=call):
            with self.assertRaises(HTTPException) as error: p.sync(self.token)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(p.status(self.token)["transaction_count"], 0)

    def test_public_notice_intent_uses_cache_and_refuses_combined_money_action(self):
        payload = json.dumps([{"id": "HKMA-test", "title": "official test title", "published_at": "2026-10-26", "url": "https://www.hkma.gov.hk/eng/news-and-media/scam-alert/test"}])
        with b._connect() as db:
            db.execute("INSERT INTO sandbox_public_feeds(source,payload,fetched_at) VALUES('hkma',?,?)", (payload, b._now().isoformat()))
        reply = b.interpret_intent(b.IntentRequest(text="看看最新防骗通知"), self.token)
        self.assertEqual(reply["kind"], "public_notices")
        self.assertIn("HKMA-test", reply["reply"])
        combined = b.interpret_intent(b.IntentRequest(text="查防骗通知然后给妈妈转100元"), self.token)
        self.assertEqual(combined["kind"], "clarification")
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")


if __name__ == "__main__": unittest.main()
