import base64, hashlib, hmac, io, json, tempfile, unittest
from line_notify import LineBot

SECRET = 'a' * 32


class FakeLine:
    def __init__(self): self.calls = []
    def __call__(self, request, timeout):
        path = request.full_url.removeprefix('https://api.line.me')
        body = request.data.decode() if request.data else None
        self.calls.append((path, body, request.get_header('Authorization')))
        reply = {'/oauth2/v3/token': {'access_token': 'tok', 'expires_in': 900},
                 '/v2/bot/info': {'displayName': 'Printer Bot', 'basicId': '@123abc'}}.get(path, {})
        if path.startswith('/v2/bot/profile/'): reply = {'displayName': 'Natt'}
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return json.dumps(reply).encode()
        return R()


def signed(bot, events):
    body = json.dumps({'events': events}).encode()
    return body, base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


class LineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.fake = FakeLine()
        self.bot = LineBot(self.folder, self.fake)
        self.bot.connect('2012565475', SECRET)

    def test_connect_checks_credentials_and_keeps_secret_private(self):
        self.assertIn('client_id=2012565475', self.fake.calls[0][1])
        state = self.bot.public('https://x/api/line/webhook')
        self.assertTrue(state['configured'])
        self.assertNotIn(SECRET, json.dumps(state))
        self.assertEqual(state['bot'], {'name': 'Printer Bot', 'basic_id': '@123abc'})
        with self.assertRaises(ValueError): self.bot.connect('abc', SECRET)

    def test_code_links_account_and_unsigned_calls_are_rejected(self):
        code = self.bot.public('')['code']
        body, sig = signed(self.bot, [{'type': 'message', 'replyToken': 'r', 'source': {'type': 'user', 'userId': 'U1'},
                                       'message': {'type': 'text', 'text': code}}])
        self.assertFalse(self.bot.webhook(body, 'bad'))
        self.assertEqual(self.bot.public('')['recipients'], [])
        self.assertTrue(self.bot.webhook(body, sig))
        state = self.bot.public('')
        self.assertEqual(state['recipients'], [{'id': 'U1', 'name': 'Natt'}])
        self.assertNotEqual(state['code'], code)
        # The used code no longer links anyone else.
        body, sig = signed(self.bot, [{'type': 'message', 'replyToken': 'r', 'source': {'type': 'user', 'userId': 'U2'},
                                       'message': {'type': 'text', 'text': code}}])
        self.bot.webhook(body, sig)
        self.assertEqual(len(self.bot.public('')['recipients']), 1)
        self.assertTrue(LineBot(self.folder, self.fake).verify(*reversed(signed(self.bot, [])[::-1])))

    def test_send_respects_switches(self):
        self.bot.config['recipients'] = {'U1': {'name': 'N'}}
        self.assertEqual(self.bot.send('complete', {'title': 'Q2 1 finished printing', 'body': 'a.gcode', 'printer': 'Q2 1', 'file': 'a.gcode', 'duration': 5025}, wait=True), 1)
        path, body, auth = self.fake.calls[-1]
        self.assertEqual((path, auth), ('/v2/bot/message/multicast', 'Bearer tok'))
        sent = json.loads(body)['messages'][0]
        self.assertEqual(sent['type'], 'flex')
        self.assertTrue(sent['altText'].startswith('✅ Q2 1 finished printing'))
        self.bot.settings(True, ['error'])
        self.assertEqual(self.bot.send('complete', {'title': 't'}, wait=True), 0)
        self.bot.settings(False, ['error'])
        self.assertEqual(self.bot.send('error', {'title': 't'}, wait=True), 0)
        self.assertEqual(self.bot.send('test', {'title': 't'}, wait=True), 1)


    def test_card_layout(self):
        from line_notify import card, hours
        from push import TEXT
        self.assertEqual(hours(5025, lambda k, **v: TEXT['th'][k].format(**v)), '1 ชม. 23 นาที')
        c = card('error', 'Q2 2 stopped', [('Printer', 'Q2 2'), ('Error', '')], {'chip': 'ERROR'}, 'https://x/', 'Open')
        rows = c['contents']['body']['contents']
        self.assertEqual(len(rows), 1)  # empty values are left out
        self.assertEqual(c['contents']['footer']['contents'][0]['action']['uri'], 'https://x/')

if __name__ == '__main__':
    unittest.main()
