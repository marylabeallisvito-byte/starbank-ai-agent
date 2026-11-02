import unittest
from fastapi import HTTPException
from backend.tests import test_banking_trust as fixtures
from backend import banking as b,banking_auth as a


class DeviceCheckTests(unittest.TestCase):
    setUp=fixtures.TrustTests.setUp
    tearDown=fixtures.TrustTests.tearDown
    register=fixtures.TrustTests.register
    assertion=fixtures.TrustTests.assertion

    def test_read_only_check_is_one_time_and_creates_no_transaction_grant(self):
        self.register();options=a.device_check_options(self.token)
        request=a.Verify(challenge_id=options['challenge_id'],credential=self.assertion(options['options']['challenge']))
        result=a.device_check_verify(request,self.token)
        self.assertTrue(result['user_verified']);self.assertFalse(result['funds_moved'])
        with b._connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_auth_grants').fetchone()[0],0)
        with self.assertRaises(HTTPException):a.device_check_verify(request,self.token)
        self.assertTrue(a.device_check_status(self.token)['verified'])
        self.assertEqual(b.overview(self.token)['balance'],'2500.00')

    def test_wrong_origin_or_missing_user_verification_is_rejected(self):
        self.register()
        for origin,flags in [('https://wrong.test',0x05),('http://localhost:5173',0x01)]:
            options=a.device_check_options(self.token)
            with self.assertRaises(HTTPException):a.device_check_verify(a.Verify(challenge_id=options['challenge_id'],credential=self.assertion(options['options']['challenge'],origin,flags=flags)),self.token)
        self.assertFalse(a.device_check_status(self.token)['verified'])

    def test_device_check_cannot_be_reused_as_transfer_authorization(self):
        self.register();options=a.device_check_options(self.token)
        request=a.Verify(challenge_id=options['challenge_id'],credential=self.assertion(options['options']['challenge']))
        with self.assertRaises(HTTPException):a.authorization_verify(request,self.token)
