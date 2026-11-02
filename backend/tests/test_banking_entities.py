import json
import unittest
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from backend.tests import test_banking_evidence as fixtures
from backend import banking_entities as e
from backend import banking as b

LEI='MLU0ZO3ML4LN2LL2TL39'


class EntityTests(unittest.TestCase):
    setUp=fixtures.BankingEvidenceTests.setUp
    tearDown=fixtures.BankingEvidenceTests.tearDown

    def payload(self):return {'data':{'id':LEI,'attributes':{'lei':LEI,'entity':{'legalName':{'name':'Synthetic institution'},'jurisdiction':'GB','status':'ACTIVE'},'registration':{'status':'ISSUED','lastUpdateDate':'2026-10-01'}}}}

    def lookup(self):return e.lookup(e.Lookup(lei=LEI,confirmed=True),self.token)

    def test_official_lookup_is_cached_and_does_not_authorize_money(self):
        response=httpx.Response(200,json=self.payload(),request=httpx.Request('GET',e.BASE+LEI))
        with patch('httpx.Client.get',return_value=response) as call:
            first=self.lookup();second=self.lookup()
        self.assertEqual(call.call_count,1)
        self.assertEqual(first['status'],'fresh')
        self.assertEqual(second['status'],'cached')
        self.assertIn('不证明',first['scope'])

    def test_bad_checksum_and_unconfirmed_request_never_contact_provider(self):
        with patch('httpx.Client.get') as call:
            for req in [e.Lookup(lei=LEI,confirmed=False),e.Lookup(lei=LEI[:-1]+'5',confirmed=True)]:
                with self.assertRaises(HTTPException):e.lookup(req,self.token)
        call.assert_not_called()

    def test_identifier_mismatch_is_not_cached(self):
        payload=self.payload();payload['data']['id']='another'
        with patch('httpx.Client.get',return_value=httpx.Response(200,json=payload,request=httpx.Request('GET',e.BASE+LEI))):
            with self.assertRaises(HTTPException) as error:self.lookup()
        self.assertEqual(error.exception.status_code,503)

    def test_not_found_is_not_a_risk_clearance(self):
        with patch('httpx.Client.get',return_value=httpx.Response(404,request=httpx.Request('GET',e.BASE+LEI))):
            with self.assertRaises(HTTPException) as error:self.lookup()
        self.assertEqual(error.exception.status_code,404)

    def test_entity_intent_opens_confirmation_panel_without_network_or_money(self):
        with patch('httpx.Client.get') as call:
            result=b.interpret_intent(b.IntentRequest(text='查询机构 LEI 登记'),self.token)
        self.assertEqual(result['kind'],'entity_lookup')
        call.assert_not_called()
        self.assertEqual(b.overview(self.token)['balance'],'2500.00')
