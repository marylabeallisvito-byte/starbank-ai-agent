"""Routing, authorization boundaries and cross-channel workflow regressions."""
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from backend.tests import test_banking_evidence as fixtures
from backend import banking as b,banking_contracts as c,banking_nlu as n,banking_missions as m,banking_plans as p


class ContractTests(unittest.TestCase):
    setUp=fixtures.BankingEvidenceTests.setUp
    tearDown=fixtures.BankingEvidenceTests.tearDown
    # Inherit only fixture lifecycle, not the evidence test collection.
    def test_new_semantic_routes_are_connected_without_network_or_money(self):
        examples={'fx_budget':'What is the conversion cost?', 'statement_import':'Can I upload my statement?', 'entity_lookup':'Check registration identity', 'public_notices':'Official fraud alerts'}
        with patch.dict('os.environ',{'BANKING_LLM_ENABLED':'1'}):
            for kind,text in examples.items():
                with self.subTest(kind=kind),patch.object(b,'classify_semantic_intent',return_value=n.IntentGuess(kind=kind)):
                    result=b.interpret_intent(b.IntentRequest(text=text),self.token)
                    self.assertEqual(result['kind'],kind)
                    self.assertEqual(result['capability']['route'],c.INTENTS[kind]['route'])
                    self.assertFalse(result['funds_authorized'])
        self.assertEqual(b.overview(self.token)['balance'],'2500.00')

    def test_multi_domain_or_conditional_requests_do_not_silently_become_transfers(self):
        for text in ('先查余额，再给妈妈转800元','如果够用就给妈妈转800元','给妈妈转800元，不影响房租','分析账单并申购基金'):
            with self.subTest(text=text):
                result=b.interpret_intent(b.IntentRequest(text=text),self.token)
                self.assertEqual(result['kind'],'clarification')
                self.assertFalse(result['funds_authorized'])
        with b._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sandbox_transfers').fetchone()[0],0)

    def test_catalogue_is_session_protected_and_covers_model_vocabulary(self):
        with self.assertRaises(HTTPException):b.capabilities(None)
        result=b.capabilities(self.token)
        self.assertEqual({v['kind'] for v in result['intents']},set(n.IntentGuess.model_json_schema()['properties']['kind']['enum']))

    def test_scope_clarification_discards_incomplete_transfer_context(self):
        b.interpret_intent(b.IntentRequest(text='给妈妈转账'),self.token)
        b.interpret_intent(b.IntentRequest(text='先看账单再转账'),self.token)
        result=b.interpret_intent(b.IntentRequest(text='800元'),self.token)
        self.assertNotEqual(result['kind'],'transfer_draft')

    def test_scheduled_draft_reaches_real_plan_state_machine_without_reserving_early(self):
        response=m.message(m.MessageRequest(text='明天预留500元，提醒云上视频续费',client_id='contract-schedule-01'),self.token)
        draft=response['attachment']
        self.assertEqual(draft['kind'],'plan_draft')
        preview=p.prepare_plan(p.PlanRequest(amount=draft['amount'],subscription_id=draft['subscription_id'],run_at=draft['run_at']),self.token)
        self.assertEqual(preview['status'],'DRAFT')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')
        confirmed=p.confirm_plan(preview['id'],p.PlanConfirm(digest=preview['digest']),self.token)
        self.assertEqual(confirmed['status'],'SCHEDULED')
        p.cancel_plan(preview['id'],self.token)
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_transfer_policy_boundary_and_risk_escalation_are_rechecked(self):
        self.assertEqual(c.transfer_level(0,100000,'standard'),'yellow')
        self.assertEqual(c.transfer_level(1,100000,'standard'),'red')
        self.assertEqual(c.transfer_level(0,1,'review'),'red')
