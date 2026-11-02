"""Malformed cached inputs cannot become reference quotes or overwrite good data."""
import hashlib
import json
import unittest
from unittest.mock import patch
import httpx
from datetime import timedelta
from fastapi import HTTPException
from backend import banking as b,banking_fx as fx
from backend.tests import test_banking_money_tools as fixtures
from backend.tests import test_banking_external as external_fixtures
from backend import banking_plaid as plaid


class FeedResilienceTests(unittest.TestCase):
    setUp=fixtures.MoneyToolsTests.setUp
    tearDown=fixtures.MoneyToolsTests.tearDown
    seed_rates=fixtures.MoneyToolsTests.seed_rates

    def test_malformed_semantic_cache_is_unavailable_even_with_valid_hash(self):
        with b._connect() as db:original=json.loads(db.execute("SELECT payload FROM sandbox_public_feeds WHERE source='fx_cny'").fetchone()[0])
        for mutate in ('missing_currency','nan_rate','negative_rate','duplicate_currency','invalid_date','missing_date','wrong_base'):
            with self.subTest(mutate=mutate):
                rows=json.loads(json.dumps(original))
                if mutate=='missing_currency':rows.pop()
                elif mutate=='duplicate_currency':rows[0]['quote']=rows[1]['quote']
                elif mutate=='invalid_date':rows[0]['date']='not-a-date'
                elif mutate=='missing_date':rows[0].pop('date')
                elif mutate=='wrong_base':rows[0]['base']='USD'
                else:rows[0]['rate']='NaN' if mutate=='nan_rate' else '-1'
                payload=json.dumps(rows);digest=hashlib.sha256(payload.encode()).hexdigest()
                with b._connect() as db:db.execute("UPDATE sandbox_public_feeds SET payload=?,content_hash=? WHERE source='fx_cny'",(payload,digest))
                value=fx.rates(self.token)
                self.assertFalse(value['usable']);self.assertEqual(value['status'],'unavailable');self.assertEqual(value['rates'],[])
                with self.assertRaises(HTTPException) as error:fx.prepare(fx.BudgetRequest(currency='HKD',foreign_amount='10',snapshot_hash=digest),self.token)
                self.assertEqual(error.exception.status_code,409)

    def test_future_or_invalid_fetch_time_never_yields_usable_quote(self):
        for value in ((b._now()+timedelta(days=1)).isoformat(),'not-a-time','2026-10-01T12:00:00'):
            with self.subTest(value=value):
                with b._connect() as db:db.execute("UPDATE sandbox_public_feeds SET fetched_at=? WHERE source='fx_cny'",(value,))
                self.assertFalse(fx.rates(self.token)['usable'])


class ExternalPayloadTests(unittest.TestCase):
    setUp=external_fixtures.ExternalTests.setUp
    tearDown=external_fixtures.ExternalTests.tearDown
    connect=external_fixtures.ExternalTests.connect
    sync=external_fixtures.ExternalTests.sync
    tx=external_fixtures.ExternalTests.tx
    page=external_fixtures.ExternalTests.page

    def test_non_boolean_pending_rolls_back_new_page_and_cursor(self):
        self.connect();self.sync([self.page(added=[self.tx('known')],cursor='original')])
        for value in ('false','true',0,1,None):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException):self.sync([self.page(added=[self.tx('bad',pending=value)],cursor='changed')])
                result=plaid.status(self.token)
                self.assertEqual([row['id'] for row in result['transactions']],['known'])
                with b._connect() as db:self.assertEqual(db.execute('SELECT cursor FROM sandbox_plaid_connections').fetchone()[0],'original')

    def test_duplicate_or_missing_account_identity_rejects_sync_without_overwrite(self):
        self.connect();self.sync([self.page(added=[self.tx('known')],cursor='original')])
        original=self.accounts
        for accounts in ([original[0],original[0]],[{**original[0],'account_id':None}],[{**original[0],'balances':None}]):
            with self.subTest(accounts=accounts),patch.object(self,'accounts',accounts):
                with self.assertRaises(HTTPException):self.sync([self.page(cursor='changed')])
                self.assertEqual(plaid.status(self.token)['transaction_count'],1)
                with b._connect() as db:self.assertEqual(db.execute('SELECT cursor FROM sandbox_plaid_connections').fetchone()[0],'original')

    def test_rate_limit_and_network_timeout_preserve_successful_snapshot(self):
        self.connect();self.sync([self.page(added=[self.tx('known')],cursor='original')])
        errors=[httpx.ReadTimeout('injected timeout'),None]
        for error in errors:
            with self.subTest(timeout=bool(error)):
                response=httpx.Response(429,json={'error_code':'RATE_LIMIT_EXCEEDED'},request=httpx.Request('POST','https://sandbox.plaid.com/accounts/get'))
                with patch.object(httpx.Client,'post',side_effect=error,return_value=response):
                    with self.assertRaises(HTTPException) as failure:plaid.sync(self.token)
                self.assertEqual(failure.exception.status_code,503)
                self.assertEqual(plaid.status(self.token)['transaction_count'],1)
                with b._connect() as db:self.assertEqual(db.execute('SELECT cursor FROM sandbox_plaid_connections').fetchone()[0],'original')
        self.assertEqual(b.overview(self.token)['balance'],'2500.00')
