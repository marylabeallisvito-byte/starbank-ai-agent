"""HTTP journeys with real signature verification and injected lost responses.

Keys are software test vectors, not a hardware/user or independent assessment.
Every test uses a fresh encrypted database and synthetic account.
"""
import base64
import unittest
import sqlite3
from datetime import timedelta
from unittest.mock import patch
from cryptography.fernet import Fernet
from starlette.testclient import TestClient
from backend.main import app
from backend.tests import test_banking_trust as fixtures
from backend import banking as b,banking_missions as missions


class DropOnce:
    def __init__(self,app):self.app=app;self.path=None
    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope['path']==self.path:
            self.path=None
            async def discard(message):pass
            await self.app(scope,receive,discard)
            raise RuntimeError('injected response loss after application commit')
        await self.app(scope,receive,send)


class JourneyTests(unittest.TestCase):
    register=fixtures.TrustTests.register
    assertion=fixtures.TrustTests.assertion
    def setUp(self):
        self.encryption=patch.dict('os.environ',{'BANKING_DATA_ENCRYPTION_KEY':Fernet.generate_key().decode(),'BANKING_DATA_PREVIOUS_KEYS':''});self.encryption.start()
        fixtures.TrustTests.setUp(self)
        self.transport=DropOnce(app);self.client=TestClient(self.transport);self.headers={'X-Banking-Session':self.token};self.counter=0
    def tearDown(self):
        self.client.close();fixtures.TrustTests.tearDown(self);self.encryption.stop()
    def get(self,path,token=None):return self.client.get('/api/banking'+path,headers=self.headers if token is None else {'X-Banking-Session':token})
    def post(self,path,body=None):return self.client.post('/api/banking'+path,headers=self.headers,json=body or {})
    def ok(self,response):self.assertEqual(response.status_code,200,response.text[:500]);return response.json()
    def transfer(self,amount='80'):
        draft=self.ok(self.post('/transfers/prepare',{'recipient_id':'mom','amount':amount,'note':'journey'}))
        self.ok(self.post('/transfers/'+draft['id']+'/confirm',{'digest':draft['digest']}));return draft
    def sign(self,kind,identifier):
        options=self.ok(self.post('/auth/authorize/options',{'kind':kind,'action_id':identifier}));self.counter+=1
        return self.post('/auth/authorize/verify',{'challenge_id':options['challenge_id'],'credential':self.assertion(options['options']['challenge'],count=self.counter)})
    def recover(self,kind,item,token=None):return self.get('/execution/'+kind+'/'+item['id']+'?expected_digest='+item['digest'],token)
    def reconcile(self):return self.ok(self.get('/execution-reconciliation'))

    def test_lost_transfer_response_recovers_receipt_without_second_debit(self):
        draft=self.transfer();path='/transfers/'+draft['id']+'/execute';key='lost-transfer-response-01'
        self.transport.path='/api/banking'+path
        with self.assertRaisesRegex(RuntimeError,'injected response loss'):self.post(path,{'idempotency_key':key})
        recovered=self.ok(self.recover('transfer',draft));self.assertTrue(recovered['complete']);receipt=recovered['result']['receipt']
        replay=self.ok(self.post(path,{'idempotency_key':key}));self.assertEqual(replay['receipt'],receipt)
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2420.00');self.assertTrue(self.reconcile()['consistent'])
        with b._connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM sandbox_audit WHERE event='COMMITTED'").fetchone()[0],1)

    def test_recovery_does_not_execute_ready_transfer_and_is_owned(self):
        draft=self.transfer();self.assertFalse(self.ok(self.recover('transfer',draft))['complete'])
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2500.00')
        other=b.create_session()['token'];self.assertEqual(self.recover('transfer',draft,other).status_code,404)
        bad={**draft,'digest':'0'*64};self.assertEqual(self.recover('transfer',bad).status_code,409)

    def test_prepared_yellow_transfer_stops_after_other_transfer_crosses_daily_threshold(self):
        draft=self.transfer('900');second=self.transfer('200')
        self.ok(self.post('/transfers/'+second['id']+'/execute',{'idempotency_key':'threshold-other-01'}))
        rejected=self.post('/transfers/'+draft['id']+'/execute',{'idempotency_key':'threshold-original-01'})
        self.assertEqual(rejected.status_code,409);self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2300.00');self.assertTrue(self.reconcile()['consistent'])

    def test_graph_change_invalidates_authorization_before_money_effect(self):
        from backend import banking_evidence as e
        e.fixture(e.FixtureRequest(scenario='normal',recipient_id='mom'),self.token)
        draft=self.transfer()
        e.fixture(e.FixtureRequest(scenario='shared',recipient_id='mom'),self.token)
        self.assertEqual(self.post('/transfers/'+draft['id']+'/execute',{'idempotency_key':'changed-graph-01'}).status_code,409)
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2500.00')

    def test_real_signature_algorithm_and_expired_transaction_remain_separate(self):
        self.register();draft=self.transfer('1001');self.ok(self.sign('transfer',draft['id']))
        with b._connect() as db:db.execute("UPDATE sandbox_transfers SET expires_at='2000-01-01' WHERE id=?",(draft['id'],))
        recovered=self.ok(self.recover('transfer',draft));self.assertTrue(recovered['expired']);self.assertFalse(recovered['can_continue'])
        self.assertEqual(self.post('/transfers/'+draft['id']+'/execute',{'idempotency_key':'expired-authorized-01'}).status_code,409)
        self.assertTrue(self.reconcile()['consistent'])

    def test_wealth_buy_and_redeem_lost_response_reconcile_with_holdings(self):
        self.register();self.ok(self.post('/wealth/profile',{'experience':2,'loss_tolerance':2,'horizon':2}))
        for endpoint,amount in (('/wealth/orders/prepare','200'),('/wealth/redemptions/prepare','50')):
            item=self.ok(self.post(endpoint,{'product_id':'cash','amount':amount}))
            self.ok(self.post('/wealth/orders/'+item['id']+'/confirm',{'digest':item['digest']}));self.ok(self.sign('wealth',item['id']))
            path='/wealth/orders/'+item['id']+'/execute';key='wealth-loss-'+amount;self.transport.path='/api/banking'+path
            with self.assertRaisesRegex(RuntimeError,'injected response loss'):self.post(path,{'idempotency_key':key})
            recovered=self.ok(self.recover('wealth',item));self.assertTrue(recovered['complete'])
            self.assertEqual(self.ok(self.post(path,{'idempotency_key':key}))['receipt'],recovered['result']['receipt'])
            self.assertTrue(self.reconcile()['consistent'])
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2350.00')

    def test_profile_revision_change_blocks_previously_signed_buy_even_same_risk_level(self):
        self.register();answers={'experience':2,'loss_tolerance':2,'horizon':2};self.ok(self.post('/wealth/profile',answers))
        item=self.ok(self.post('/wealth/orders/prepare',{'product_id':'cash','amount':'100'}))
        self.ok(self.post('/wealth/orders/'+item['id']+'/confirm',{'digest':item['digest']}));self.ok(self.sign('wealth',item['id']))
        # A new edition must not depend on wall-clock granularity or changed answers.
        from backend import banking_wealth as wealth
        with patch.object(wealth,'_now',return_value=b._now()):
            self.ok(self.post('/wealth/profile',answers))
        response=self.post('/wealth/orders/'+item['id']+'/execute',{'idempotency_key':'profile-revised-01'})
        self.assertEqual(response.status_code,409);self.assertIn('版本',response.json()['detail']);self.assertTrue(self.reconcile()['consistent'])

    def test_unsuitable_product_is_rejected_before_order_or_money(self):
        self.ok(self.post('/wealth/profile',{'experience':0,'loss_tolerance':0,'horizon':0}))
        self.assertEqual(self.post('/wealth/orders/prepare',{'product_id':'index','amount':'100'}).status_code,403)
        self.assertTrue(self.reconcile()['consistent'])

    def test_device_acceptance_requires_signed_and_committed_red_transfer(self):
        self.assertFalse(self.ok(self.get('/execution-device-acceptance'))['verified'])
        self.register();item=self.transfer('1001');self.ok(self.sign('transfer',item['id']))
        self.assertFalse(self.ok(self.get('/execution-device-acceptance'))['verified'])
        self.ok(self.post('/transfers/'+item['id']+'/execute',{'idempotency_key':'signed-red-proof-01'}))
        proof=self.ok(self.get('/execution-device-acceptance'));self.assertTrue(proof['verified']);self.assertTrue(proof['result']['user_verified'])

    def import_statement(self,identifier):
        day=b._now().astimezone(b.TZ).date().isoformat()
        content=f'日期,商户,金额,流水号\n{day},测试商户,20,{identifier}\n'.encode('utf-8-sig')
        preview=self.ok(self.post('/statements/preview',{'filename':'journey.csv','content_base64':base64.b64encode(content).decode(),'confirmed_local_parse':True,'sign_convention':'positive_expense'}))
        return self.ok(self.post('/statements/'+preview['id']+'/confirm',{'digest':preview['digest'],'confirmed':True}))

    def test_bill_import_is_idempotent_and_source_change_blocks_downstream_budget(self):
        self.import_statement('first');self.import_statement('first')
        self.assertEqual(self.ok(self.get('/statements'))['record_count'],1)
        item=self.ok(self.post('/missions/prepare',{'amount':'100','minimum_balance':'1000','bill_source':'imported'}))
        self.ok(self.post('/missions/'+item['id']+'/action',{'action':'confirm','digest':item['digest']}));missions.tick(item['id'])
        self.import_statement('second');missions.tick(item['id'])
        self.assertEqual(self.ok(self.get('/missions/'+item['id']))['status'],'BLOCKED')
        self.assertEqual(self.ok(self.get('/overview'))['reserved'],'0.00');self.assertTrue(self.reconcile()['consistent'])

    def test_reconciliation_detects_independent_cash_holding_and_reservation_corruption(self):
        with b._connect() as db:
            db.execute('UPDATE sandbox_sessions SET balance_fen=balance_fen+1,reserved_fen=1')
            db.execute("INSERT INTO sandbox_wealth_holdings VALUES(?,'cash',1,?)",(db.execute('SELECT id FROM sandbox_sessions').fetchone()[0],b._now().isoformat()))
        report=self.reconcile();self.assertFalse(report['consistent']);self.assertTrue(all(not c['matched'] for c in report['checks']))

    def test_storage_failure_after_cash_update_rolls_back_transfer_before_safe_retry(self):
        item=self.transfer();path='/transfers/'+item['id']+'/execute';key='atomic-transfer-retry-01'
        with b._connect() as db:db.execute("CREATE TRIGGER fail_commit BEFORE INSERT ON sandbox_audit WHEN NEW.event='COMMITTED' BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected storage failure'):self.post(path,{'idempotency_key':key})
        self.assertFalse(self.ok(self.recover('transfer',item))['complete']);self.assertTrue(self.reconcile()['consistent'])
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2500.00')
        with b._connect() as db:db.execute('DROP TRIGGER fail_commit')
        self.ok(self.post(path,{'idempotency_key':key}));self.assertTrue(self.reconcile()['consistent'])

    def test_storage_failure_rolls_back_wealth_cash_holdings_and_authorization_grant(self):
        self.register();self.ok(self.post('/wealth/profile',{'experience':2,'loss_tolerance':2,'horizon':2}))
        item=self.ok(self.post('/wealth/orders/prepare',{'product_id':'cash','amount':'100'}))
        self.ok(self.post('/wealth/orders/'+item['id']+'/confirm',{'digest':item['digest']}));self.ok(self.sign('wealth',item['id']))
        with b._connect() as db:db.execute("CREATE TRIGGER fail_wealth BEFORE INSERT ON sandbox_audit WHEN NEW.event='WEALTH_COMMITTED' BEGIN SELECT RAISE(ABORT,'injected wealth storage failure'); END")
        path='/wealth/orders/'+item['id']+'/execute';key='atomic-wealth-retry-01'
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected wealth storage failure'):self.post(path,{'idempotency_key':key})
        self.assertTrue(self.reconcile()['consistent']);self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2500.00')
        with b._connect() as db:db.execute('DROP TRIGGER fail_wealth')
        self.ok(self.post(path,{'idempotency_key':key}));self.assertTrue(self.reconcile()['consistent'])

    def test_scheduled_and_mission_reservations_and_releases_reconcile(self):
        from backend import banking_plans as plans
        self.ok(self.post('/consents/subscriptions',{'confirmed':True}))
        subscription=self.ok(self.get('/subscriptions'))['items'][0]['id']
        future=b._now()+timedelta(days=1)
        item=self.ok(self.post('/plans/prepare',{'amount':'100','subscription_id':subscription,'run_at':future.isoformat()}))
        self.ok(self.post('/plans/'+item['id']+'/confirm',{'digest':item['digest']}));plans.run_due_plans(now=future)
        self.assertTrue(self.reconcile()['consistent']);self.assertEqual(self.reconcile()['reservation_sources_fen']['scheduled'],10000)
        self.ok(self.post('/plans/'+item['id']+'/release'));self.assertTrue(self.reconcile()['consistent'])
        with b._connect() as db:db.execute('DELETE FROM sandbox_bills')
        mission=self.ok(self.post('/missions/prepare',{'amount':'100','minimum_balance':'1000'}))
        self.ok(self.post('/missions/'+mission['id']+'/action',{'action':'confirm','digest':mission['digest']}))
        for _ in range(6):missions.tick(mission['id'])
        self.assertEqual(self.ok(self.get('/missions/'+mission['id']))['status'],'COMPLETED')
        self.assertEqual(self.reconcile()['reservation_sources_fen']['missions'],10000);self.assertTrue(self.reconcile()['consistent'])
        self.ok(self.post('/missions/'+mission['id']+'/action',{'action':'release','digest':mission['digest']}));self.assertTrue(self.reconcile()['consistent'])

    def test_fx_reservation_and_release_join_the_same_reconciliation(self):
        from backend.tests import test_banking_money_tools as fixtures
        fixtures.MoneyToolsTests.seed_rates(self)
        rates=self.ok(self.get('/fx/rates'))
        item=self.ok(self.post('/fx/quotes',{'currency':'HKD','foreign_amount':'100','buffer_percent':'0','snapshot_hash':rates['snapshot_hash']}))
        body={'digest':item['digest'],'confirmed':True}
        self.ok(self.post('/fx/quotes/'+item['id']+'/reserve',body))
        report=self.reconcile();self.assertTrue(report['consistent']);self.assertEqual(report['reservation_sources_fen']['fx'],5000)
        self.ok(self.post('/fx/quotes/'+item['id']+'/release',body));self.assertTrue(self.reconcile()['consistent'])
