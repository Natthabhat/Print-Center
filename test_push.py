import base64, io, json, os, struct, tempfile, unittest, urllib.error
from unittest.mock import patch
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import push
from push import Push, b64e, b64d

ENDPOINT = 'https://fcm.googleapis.com/fcm/send/abc'


def device():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return key, {'endpoint': ENDPOINT, 'keys': {'p256dh': b64e(public), 'auth': b64e(auth)}}


def decrypt(body, key, sub):
    """What the browser does with a push message (RFC 8291)."""
    salt, size, idlen = body[:16], struct.unpack('!I', body[16:20])[0], body[20]
    server = body[21:21 + idlen]
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server))
    ua = b64d(sub['keys']['p256dh'])
    ikm = HKDF(hashes.SHA256(), 32, b64d(sub['keys']['auth']), b'WebPush: info\x00' + ua + server).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b'Content-Encoding: aes128gcm\x00').derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b'Content-Encoding: nonce\x00').derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert size == 4096 and plain.endswith(b'\x02')
    return plain[:-1]


class Response:
    status = 201
    def __enter__(self): return self
    def __exit__(self, *a): return False


class PushTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.push = Push(self.folder, 'https://printer.example.com')

    def test_message_decrypts_and_jwt_verifies(self):
        key, sub = device()
        self.push.subscribe(sub, ['complete'])
        seen = {}
        def opener(request, timeout):
            seen.update(body=request.data, headers=dict(request.header_items()))
            return Response()
        self.assertTrue(self.push.deliver(self.push.devices[ENDPOINT], {'title': 'Done'}, opener))
        self.assertEqual(json.loads(decrypt(seen['body'], key, sub)), {'title': 'Done'})
        auth = seen['headers']['Authorization']
        token, k = auth.removeprefix('vapid t=').split(', k=')
        head, claims, signature = token.split('.')
        self.assertEqual(k, self.push.public_key())
        self.assertEqual(json.loads(b64d(claims))['aud'], 'https://fcm.googleapis.com')
        raw = b64d(signature)
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(k))
        public.verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
                      (head + '.' + claims).encode(), ec.ECDSA(hashes.SHA256()))
        self.assertEqual(seen['headers']['Content-encoding'], 'aes128gcm')

    def test_key_and_devices_survive_restart(self):
        _, sub = device()
        self.push.subscribe(sub, ['error'])
        again = Push(self.folder, 'https://printer.example.com')
        self.assertEqual(again.public_key(), self.push.public_key())
        self.assertEqual(again.status(ENDPOINT), {'subscribed': True, 'events': ['error']})
        again.unsubscribe(ENDPOINT)
        self.assertFalse(Push(self.folder, 'x').status(ENDPOINT)['subscribed'])

    def test_rejects_non_push_endpoints_and_bad_keys(self):
        for endpoint in ('http://fcm.googleapis.com/x', 'https://192.168.31.36/x', 'https://evil.example/x',
                         'https://fcm.googleapis.com.evil.example/x', 'https://evilgoogle.com/x', 'https://u@fcm.googleapis.com/x', 'https://fcm.googleapis.com:8443/x'):
            _, sub = device(); sub['endpoint'] = endpoint
            with self.assertRaises(ValueError): self.push.subscribe(sub, ['complete'])
        for endpoint in ('https://jmt17.google.com/fcm/send/abc', 'https://web.push.apple.com/abc', 'https://wns2-par02p.notify.windows.com/w/?token=1'):
            _, sub = device(); sub['endpoint'] = endpoint
            self.push.subscribe(sub, ['complete'])
        _, sub = device(); sub['keys']['auth'] = b64e(b'short')
        with self.assertRaises(ValueError): self.push.subscribe(sub, ['complete'])
        _, sub = device()
        with self.assertRaises(ValueError): self.push.subscribe(sub, ['everything'])

    def test_expired_device_is_removed(self):
        _, sub = device()
        self.push.subscribe(sub, ['complete'])
        def gone(request, timeout): raise urllib.error.HTTPError(ENDPOINT, 410, 'Gone', {}, io.BytesIO())
        self.assertFalse(self.push.deliver(self.push.devices[ENDPOINT], {}, gone))
        self.assertFalse(self.push.status(ENDPOINT)['subscribed'])

    def test_finish_and_error_notify_only_on_change(self):
        _, sub = device()
        self.push.subscribe(sub, ['complete', 'error'])
        printer = {'id': 'qidi', 'name': 'Q2 1'}
        sent = []
        with patch.object(self.push, 'send', side_effect=lambda m, e=None, **k: sent.append((e, m))):
            self.assertIsNone(self.push.watch(printer, {'state': 'complete', 'filename': 'old.gcode'}))  # first reading after start-up
            self.push.watch(printer, {'state': 'printing', 'filename': 'a/part.gcode'})
            self.assertEqual(self.push.watch(printer, {'state': 'complete', 'filename': 'a/part.gcode'}), 'complete')
            self.assertIsNone(self.push.watch(printer, {'state': 'complete', 'filename': 'a/part.gcode'}))
            self.assertIsNone(self.push.watch(printer, {'state': 'standby'}))
            self.assertIsNone(self.push.watch(printer, {'state': 'complete'}))  # not from a running print
            self.push.watch(printer, {'state': 'printing', 'filename': 'b.gcode'})
            self.assertEqual(self.push.watch(printer, {'state': 'error', 'filename': 'b.gcode', 'message': 'Heater extruder not heating'}), 'error')
            self.assertIsNone(self.push.watch(printer, {'state': 'error'}))
        self.assertEqual(sent[0], ('complete', {'title': 'Q2 1 finished printing', 'body': 'part.gcode', 'tag': 'qidi-complete', 'url': '/'}))
        self.assertEqual(sent[1][1]['body'], 'b.gcode · Heater extruder not heating')

    def test_send_respects_each_devices_choices(self):
        _, a = device(); _, b = device(); b['endpoint'] = 'https://web.push.apple.com/b'
        self.push.subscribe(a, ['complete']); self.push.subscribe(b, ['error'])
        with patch.object(self.push, 'deliver', return_value=True) as deliver:
            self.assertEqual(self.push.send({}, 'error', wait=True), 1)
        self.assertEqual(deliver.call_args[0][0]['endpoint'], 'https://web.push.apple.com/b')


if __name__ == '__main__':
    unittest.main()
