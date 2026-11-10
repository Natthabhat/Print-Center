import unittest
from unittest.mock import patch
import app


def handler(peer, headers=None):
    h = object.__new__(app.Handler)
    h.client_address = (peer, 1234)
    h.headers = headers or {}
    return h


class LoginClientTests(unittest.TestCase):
    def test_local_tunnel_uses_reported_client_ip(self):
        for peer in ('127.0.0.1', '::1', '100.64.0.1'):
            with patch.object(app, 'BIND', '100.64.0.1'):
                self.assertEqual(handler(peer, {'CF-Connecting-IP': ' 203.0.113.9 '}).login_client(), '203.0.113.9')

    def test_remote_peer_cannot_spoof_client_ip(self):
        with patch.object(app, 'BIND', '100.64.0.1'), patch.object(app, 'EXTRA_BIND', '192.168.1.10'):
            self.assertEqual(handler('192.168.1.50', {'CF-Connecting-IP': '203.0.113.9'}).login_client(), '192.168.1.50')

    def test_missing_or_invalid_header_falls_back_to_peer(self):
        self.assertEqual(handler('127.0.0.1').login_client(), '127.0.0.1')
        self.assertEqual(handler('127.0.0.1', {'CF-Connecting-IP': 'not-an-ip'}).login_client(), '127.0.0.1')

    def test_tunnel_clients_have_separate_limits(self):
        auth = app.WebAuth()
        for _ in range(10):
            auth.limited(handler('127.0.0.1', {'CF-Connecting-IP': '203.0.113.9'}).login_client())
        self.assertTrue(auth.limited('203.0.113.9'))
        self.assertFalse(auth.limited(handler('127.0.0.1', {'CF-Connecting-IP': '198.51.100.7'}).login_client()))


if __name__ == '__main__':
    unittest.main()
