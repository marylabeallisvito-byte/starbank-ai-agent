"""Cross-business transport and transaction boundaries on isolated synthetic accounts."""
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from backend import banking as b
from backend.tests import test_banking_journeys as fixtures


class LifecycleMatrixTests(unittest.TestCase):
    setUp=fixtures.JourneyTests.setUp
    tearDown=fixtures.JourneyTests.tearDown
    register=fixtures.JourneyTests.register
    assertion=fixtures.JourneyTests.assertion
    get=fixtures.JourneyTests.get
    post=fixtures.JourneyTests.post
    ok=fixtures.JourneyTests.ok
    sign=fixtures.JourneyTests.sign
    recover=fixtures.JourneyTests.recover
    reconcile=fixtures.JourneyTests.reconcile
    transfer=fixtures.JourneyTests.transfer
    KINDS=('transfer','card','wealth','subscription')
    TABLES={'transfer':'sandbox_transfers','card':'sandbox_card_actions','wealth':'sandbox_wealth_orders','subscription':'sandbox_subscription_actions'}
    EVENTS={'transfer':'COMMITTED','card':'CARD_ACTION_EXECUTED','wealth':'WEALTH_COMMITTED','subscription':'SUBSCRIPTION_CANCEL_EXECUTED'}

    def prepare(self,kind):
        if kind=='transfer':return self.transfer(),'/transfers/'
        if kind=='card':
            item=self.ok(self.post('/card/actions/prepare',{'action':'freeze'}));prefix='/card/actions/'
        elif kind=='wealth':
            self.ok(self.post('/wealth/profile',{'experience':2,'loss_tolerance':2,'horizon':2}))
            item=self.ok(self.post('/wealth/orders/prepare',{'product_id':'cash','amount':'100'}));prefix='/wealth/orders/'
        else:
            sub=self.ok(self.get('/subscriptions'))['items'][0]['id']
            item=self.ok(self.post('/subscriptions/'+sub+'/cancel/prepare'))['action'];prefix='/subscriptions/actions/'
        self.ok(self.post(prefix+item['id']+'/confirm',{'digest':item['digest']}))
        if kind=='wealth':self.ok(self.sign(kind,item['id']))
        return item,prefix

    def test_lost_response_and_concurrent_same_key_never_duplicate_effects(self):
        self.register()
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                item,prefix=self.prepare(kind);path=prefix+item['id']+'/execute';key='matrix-lost-'+kind
                self.transport.path='/api/banking'+path
                with self.assertRaisesRegex(RuntimeError,'injected response loss'):self.post(path,{'idempotency_key':key})
                snapshot=self.ok(self.recover(kind,item));self.assertTrue(snapshot['complete'])
                with ThreadPoolExecutor(max_workers=3) as pool:
                    responses=list(pool.map(lambda _:self.post(path,{'idempotency_key':key}),range(3)))
                for response in responses:self.ok(response)
                self.assertEqual(self.post(path,{'idempotency_key':'different-'+key}).status_code,409)
                with b._connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sandbox_audit WHERE transfer_id=? AND event=?',(item['id'],self.EVENTS[kind])).fetchone()[0],1)
                self.assertTrue(self.reconcile()['consistent'])

    def test_expired_actions_cannot_execute_and_recovery_stays_read_only(self):
        self.register()
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                item,prefix=self.prepare(kind)
                with b._connect() as db:db.execute('UPDATE '+self.TABLES[kind]+" SET expires_at='2000-01-01' WHERE id=?",(item['id'],))
                recovered=self.ok(self.recover(kind,item));self.assertTrue(recovered['expired']);self.assertFalse(recovered['can_continue'])
                self.assertEqual(self.post(prefix+item['id']+'/execute',{'idempotency_key':'matrix-expired-'+kind}).status_code,409)
                self.assertTrue(self.reconcile()['consistent'])
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2500.00')

    def test_foreign_session_cannot_recover_or_execute_any_action(self):
        self.register();other=b.create_session()['token']
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                item,prefix=self.prepare(kind)
                self.assertEqual(self.recover(kind,item,other).status_code,404)
                response=self.client.post('/api/banking'+prefix+item['id']+'/execute',headers={'X-Banking-Session':other},json={'idempotency_key':'matrix-foreign-'+kind})
                self.assertEqual(response.status_code,404)
                self.assertFalse(self.ok(self.recover(kind,item))['complete'])
        self.assertEqual(self.ok(self.get('/overview'))['total_balance'],'2500.00')

    def test_storage_failure_rolls_back_business_state_before_retry(self):
        self.register()
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                item,prefix=self.prepare(kind);path=prefix+item['id']+'/execute';key='matrix-rollback-'+kind
                with b._connect() as db:
                    db.execute("CREATE TRIGGER matrix_fail BEFORE INSERT ON sandbox_audit WHEN NEW.event='"+self.EVENTS[kind]+"' BEGIN SELECT RAISE(ABORT,'matrix injected failure'); END")
                try:
                    with self.assertRaisesRegex(sqlite3.IntegrityError,'matrix injected failure'):self.post(path,{'idempotency_key':key})
                    self.assertFalse(self.ok(self.recover(kind,item))['complete'])
                    self.assertTrue(self.reconcile()['consistent'])
                    if kind=='card':self.assertEqual(self.ok(self.get('/card'))['status'],'ACTIVE')
                    if kind=='subscription':self.assertEqual(self.ok(self.recover(kind,item))['result']['subscription']['status'],'ACTIVE')
                finally:
                    with b._connect() as db:db.execute('DROP TRIGGER matrix_fail')
                self.ok(self.post(path,{'idempotency_key':key}));self.assertTrue(self.ok(self.recover(kind,item))['complete'])
                self.assertTrue(self.reconcile()['consistent'])

    def test_unlocking_card_requires_strong_authentication(self):
        item,prefix=self.prepare('card');self.ok(self.post(prefix+item['id']+'/execute',{'idempotency_key':'matrix-initial-freeze'}))
        unlock=self.ok(self.post('/card/actions/prepare',{'action':'unfreeze'}))
        self.assertEqual(unlock['level'],'red')
        confirmed=self.ok(self.post('/card/actions/'+unlock['id']+'/confirm',{'digest':unlock['digest']}))
        self.assertEqual(confirmed['status'],'AWAITING_VERIFICATION')
        self.assertEqual(self.post('/card/actions/'+unlock['id']+'/execute',{'idempotency_key':'matrix-unverified-unfreeze'}).status_code,409)
        self.assertEqual(self.ok(self.get('/card'))['status'],'FROZEN')
        self.register();self.ok(self.sign('card',unlock['id']))
        self.ok(self.post('/card/actions/'+unlock['id']+'/execute',{'idempotency_key':'matrix-authorized-unfreeze'}))
        self.assertEqual(self.ok(self.get('/card'))['status'],'ACTIVE')

    def test_recovery_does_not_offer_continuation_during_account_lock(self):
        item=self.transfer()
        with b._connect() as db:db.execute('UPDATE sandbox_sessions SET locked_until=?',((b._now()+timedelta(minutes=5)).isoformat(),))
        value=self.ok(self.recover('transfer',item))
        self.assertFalse(value['can_continue']);self.assertEqual(value['next_step'],'wait_unlock')
        self.assertEqual(self.post('/transfers/'+item['id']+'/execute',{'idempotency_key':'matrix-locked-transfer'}).status_code,423)

    def test_legacy_yellow_unlock_preview_cannot_bypass_current_policy(self):
        item,prefix=self.prepare('card');self.ok(self.post(prefix+item['id']+'/execute',{'idempotency_key':'legacy-initial-freeze'}))
        unlock=self.ok(self.post('/card/actions/prepare',{'action':'unfreeze'}))
        with b._connect() as db:db.execute("UPDATE sandbox_card_actions SET level='yellow',status='READY' WHERE id=?",(unlock['id'],))
        self.assertEqual(self.post('/card/actions/'+unlock['id']+'/execute',{'idempotency_key':'legacy-unfreeze-denied'}).status_code,409)
        self.assertEqual(self.ok(self.get('/card'))['status'],'FROZEN')
