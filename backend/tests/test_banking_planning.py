"""Behavior checks for dependency execution, authorization revision and recovery."""
import json
import base64
import unittest
from fastapi import HTTPException
from backend.tests import test_banking_evidence as fixtures
from backend import banking as b, banking_missions as m, banking_statements as statements


class PlanningTests(unittest.TestCase):
    setUp = fixtures.BankingEvidenceTests.setUp
    tearDown = fixtures.BankingEvidenceTests.tearDown

    def draft(self, amount="500", floor="1000"):
        return m.prepare(m.MissionRequest(amount=amount, minimum_balance=floor), self.token)

    def confirm(self, plan):
        return m.action(plan["id"], m.MissionAction(action="confirm", digest=plan["digest"]), self.token)

    def test_edit_draft_invalidates_old_authorization_without_money_effect(self):
        plan = self.draft()
        updated = m.revise(plan["id"], m.MissionRevision(amount="600", minimum_balance="1200", digest=plan["digest"]), self.token)
        self.assertNotEqual(plan["digest"], updated["digest"])
        with self.assertRaises(HTTPException): self.confirm(plan)
        self.assertEqual(b.overview(self.token)["reserved"], "0.00")
        self.confirm(updated)
        with self.assertRaises(HTTPException):
            m.revise(plan["id"], m.MissionRevision(amount="800", minimum_balance="0", digest=updated["digest"]), self.token)

    def test_execute_shuffled_storage_by_dependency_and_resume_without_double_reserve(self):
        with b._connect() as db: db.execute("DELETE FROM sandbox_bills")
        plan = self.draft()
        # Reorder before authorization as a planner may do; scheduling cannot rely on array order.
        nodes = list(reversed(plan["nodes"]))
        with b._connect() as db:
            row = db.execute("SELECT * FROM banking_missions WHERE id=?", (plan["id"],)).fetchone()
            digest = m.plan_digest(row["session_id"], plan["id"], plan["input"], nodes)
            db.execute("UPDATE banking_missions SET nodes_json=?,digest=? WHERE id=?", (json.dumps(nodes), digest, plan["id"]))
        plan["digest"] = digest
        self.confirm(plan)
        for _ in range(8): m.tick(plan["id"])
        current = m.list_missions(self.token)["items"][0]
        self.assertEqual(current["status"], "COMPLETED")
        self.assertEqual(b.overview(self.token)["reserved"], "500.00")
        self.assertFalse(m.tick(plan["id"]))
        self.assertEqual(b.overview(self.token)["reserved"], "500.00")

    def test_cyclic_plan_is_rejected_before_authorization(self):
        plan = self.draft()
        nodes = plan["nodes"]
        nodes[0]["depends_on"] = ["notify"]
        with b._connect() as db: db.execute("UPDATE banking_missions SET nodes_json=? WHERE id=?", (json.dumps(nodes), plan["id"]))
        with self.assertRaises(HTTPException): self.confirm(plan)

    def test_post_authorization_tampering_blocks_before_effect(self):
        plan = self.draft()
        self.confirm(plan)
        nodes = plan["nodes"]
        next(n for n in nodes if n["id"] == "reserve")["depends_on"] = []
        with b._connect() as db: db.execute("UPDATE banking_missions SET nodes_json=? WHERE id=?", (json.dumps(nodes), plan["id"]))
        m.tick(plan["id"])
        self.assertEqual(m.list_missions(self.token)["items"][0]["status"], "BLOCKED")
        self.assertEqual(b.overview(self.token)["reserved"], "0.00")

    def test_balance_change_between_check_and_reserve_is_rechecked(self):
        with b._connect() as db: db.execute("DELETE FROM sandbox_bills")
        plan = self.draft()
        self.confirm(plan)
        for _ in range(3): m.tick(plan["id"])
        with b._connect() as db: db.execute("UPDATE sandbox_sessions SET reserved_fen=150000")
        m.tick(plan["id"])
        self.assertEqual(m.list_missions(self.token)["items"][0]["status"], "BLOCKED")
        self.assertEqual(b.overview(self.token)["reserved"], "1500.00")

    def import_rows(self):
        today=b._now().strftime('%Y-%m-%d')
        csv=f"日期,商户,金额,币种,交易单号\n{today} 12:00:00,测试商户,28,CNY,a1\n{today} 12:01:00,测试商户,28,CNY,a2\n{today} 12:00:00,其他币种,99,HKD,a3\n"
        draft=statements.preview(statements.PreviewRequest(filename='synthetic.csv',content_base64=base64.b64encode(csv.encode()).decode(),confirmed_local_parse=True,sign_convention='positive_expense'),self.token)
        return statements.confirm(draft['id'],statements.Confirm(digest=draft['digest'],confirmed=True),self.token)

    def test_imported_source_drives_evidence_without_crediting_funds(self):
        self.import_rows()
        plan=m.prepare(m.MissionRequest(amount='500',minimum_balance='1000',bill_source='imported'),self.token)
        self.assertEqual(plan['input']['source_record_count'],2)
        self.confirm(plan)
        for _ in range(3): m.tick(plan['id'])
        current=m.list_missions(self.token)['items'][0]
        self.assertEqual(current['status'],'WAITING_REVIEW')
        self.assertEqual(current['evidence'][0]['merchant'],'测试商户')
        self.assertTrue(all(x.startswith('IMPORT-') for x in current['evidence'][0]['transaction_ids']))
        self.assertEqual(b.overview(self.token)['balance'],'2500.00')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_erased_import_stops_authorized_plan_before_any_effect(self):
        batch=self.import_rows()
        plan=m.prepare(m.MissionRequest(amount='500',bill_source='imported'),self.token)
        self.confirm(plan)
        statements.erase(batch['id'],statements.Erase(confirmed=True),self.token)
        m.tick(plan['id'])
        self.assertEqual(m.list_missions(self.token)['items'][0]['status'],'BLOCKED')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_missing_import_is_not_silently_replaced_with_synthetic_bills(self):
        with self.assertRaises(HTTPException) as error:
            m.prepare(m.MissionRequest(amount='500',bill_source='imported'),self.token)
        self.assertEqual(error.exception.status_code,422)
