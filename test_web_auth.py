import unittest
from unittest.mock import patch
from web_auth import WebAuth

class SessionTests(unittest.TestCase):
    def test_expiry_and_logout(self):
        auth = WebAuth()
        with patch('web_auth.time.time', return_value=100):
            cookie = 'print_center_session=' + auth.issue()
            self.assertTrue(auth.valid(cookie))
            self.assertFalse(auth.valid('print_center_session=invalid'))
        with patch('web_auth.time.time', return_value=29000):
            self.assertFalse(auth.valid(cookie))
        cookie = 'print_center_session=' + auth.issue()
        auth.revoke(cookie)
        self.assertFalse(auth.valid(cookie))

    def test_login_attempt_limit_expires(self):
        auth = WebAuth()
        with patch('web_auth.time.time', return_value=100):
            for _ in range(10): self.assertFalse(auth.limited('client'))
            self.assertTrue(auth.limited('client'))
        with patch('web_auth.time.time', return_value=161):
            self.assertFalse(auth.limited('client'))
