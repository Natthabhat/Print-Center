"""Print notifications through a LINE Messaging API bot.

Only the channel ID and channel secret are needed: short-lived access tokens are issued from them
when sending. People link their LINE account by sending the bot the code shown in Print Center,
so a stranger who adds the bot as a friend never receives printer messages.
"""
import base64, hashlib, hmac, json, os, pathlib, secrets, threading, time, urllib.error, urllib.parse, urllib.request

API = 'https://api.line.me'
EVENTS = ('complete', 'error')
MAX_RECIPIENTS = 20
ICONS = {'complete': '✅', 'error': '⚠️', 'test': '🔔'}


def call(path, data=None, token=None, form=False, opener=urllib.request.urlopen):
    headers = {}
    body = None
    if token: headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    request = urllib.request.Request(API + path, data=body, headers=headers, method='POST' if body is not None else 'GET')
    try:
        with opener(request, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as failure:
        try: detail = json.loads(failure.read()).get('message') or ''
        except Exception: detail = ''
        raise ValueError('LINE rejected the request (%d%s)' % (failure.code, ': ' + detail if detail else ''))
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('Could not reach LINE. Check the server internet connection.')


class LineBot:
    def __init__(self, folder, opener=urllib.request.urlopen):
        self.path = pathlib.Path(folder) / 'line-bot.json'
        self.opener = opener
        self.lock = threading.Lock()
        self.token = (None, 0)
        try: self.config = json.loads(self.path.read_text())
        except (OSError, ValueError): self.config = {}
        if not isinstance(self.config, dict): self.config = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.config))
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def _call(self, *args, **kw):
        return call(*args, opener=self.opener, **kw)

    def _issue(self, channel_id, secret):
        value = self._call('/oauth2/v3/token', {'grant_type': 'client_credentials', 'client_id': channel_id, 'client_secret': secret}, form=True)
        if not value.get('access_token'): raise ValueError('LINE did not return an access token')
        return value['access_token'], time.time() + min(int(value.get('expires_in') or 900), 900) - 60

    def access_token(self):
        with self.lock:
            token, until = self.token
            if token and time.time() < until: return token
            cid, secret = self.config.get('channel_id'), self.config.get('channel_secret')
        if not cid or not secret: raise ValueError('Connect the LINE bot first')
        self.token = self._issue(cid, secret)
        return self.token[0]

    def public(self, webhook_url):
        with self.lock:
            c = self.config
            if c.get('channel_id') and not c.get('code'):
                c['code'] = '%06d' % secrets.randbelow(10 ** 6); self._save()
            return {'configured': bool(c.get('channel_id') and c.get('channel_secret')), 'channel_id': c.get('channel_id', ''),
                    'enabled': c.get('enabled', True), 'events': c.get('events', list(EVENTS)), 'code': c.get('code', ''),
                    'webhook_url': webhook_url, 'bot': c.get('bot') or {},
                    'recipients': [{'id': uid, 'name': r.get('name') or 'LINE user'} for uid, r in (c.get('recipients') or {}).items()]}

    def connect(self, channel_id, secret):
        channel_id = str(channel_id or '').strip()
        secret = str(secret or '').strip() or self.config.get('channel_secret', '')
        if not channel_id.isdigit() or len(channel_id) > 20: raise ValueError('Enter the numeric Channel ID from LINE Developers')
        if not secret or len(secret) > 64 or not secret.isalnum(): raise ValueError('Enter the Channel secret from LINE Developers')
        token = self._issue(channel_id, secret)  # proves the ID and secret belong together
        try: bot = self._call('/v2/bot/info', token=token[0])
        except ValueError: bot = {}
        with self.lock:
            if self.config.get('channel_id') != channel_id: self.config['recipients'] = {}
            self.config.update(channel_id=channel_id, channel_secret=secret, code='%06d' % secrets.randbelow(10 ** 6),
                               bot={'name': bot.get('displayName', ''), 'basic_id': bot.get('basicId', '')})
            self.config.setdefault('enabled', True); self.config.setdefault('events', list(EVENTS))
            self.token = token
            self._save()

    def settings(self, enabled, events):
        if not isinstance(enabled, bool) or not isinstance(events, list) or any(e not in EVENTS for e in events):
            raise ValueError('Invalid LINE notification settings')
        with self.lock:
            self.config.update(enabled=enabled, events=sorted(set(events))); self._save()

    def remove(self, uid):
        with self.lock:
            if (self.config.get('recipients') or {}).pop(uid, None) is not None: self._save()

    def disconnect(self):
        with self.lock:
            self.config = {}; self.token = (None, 0); self._save()

    def verify(self, body, signature):
        secret = self.config.get('channel_secret')
        if not secret or not signature: return False
        expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(expected, signature)

    def _reply(self, reply_token, text):
        if not reply_token: return
        try: self._call('/v2/bot/message/reply', {'replyToken': reply_token, 'messages': [{'type': 'text', 'text': text}]}, token=self.access_token())
        except ValueError: pass

    def webhook(self, body, signature):
        """Handle one signed webhook call from LINE. Returns False when the signature is wrong."""
        if not self.verify(body, signature): return False
        try: events = json.loads(body).get('events') or []
        except ValueError: return True
        for event in events:
            source = event.get('source') or {}
            uid = source.get('userId')
            if source.get('type') != 'user' or not isinstance(uid, str): continue
            if event.get('type') == 'follow':
                self._reply(event.get('replyToken'), 'Hi! To get 3D printer notifications, open Print Center → Notifications and send me the 6-digit LINE code shown there.')
            elif event.get('type') == 'message' and (event.get('message') or {}).get('type') == 'text':
                text = ''.join(ch for ch in event['message'].get('text', '') if ch.isdigit())
                with self.lock:
                    code = self.config.get('code')
                    recipients = self.config.setdefault('recipients', {})
                    linked = uid in recipients
                    ok = bool(code) and hmac.compare_digest(text, code) and (linked or len(recipients) < MAX_RECIPIENTS)
                    if ok:
                        recipients[uid] = {'name': '', 'added': time.time()}
                        self.config['code'] = '%06d' % secrets.randbelow(10 ** 6)  # each code links one account
                        self._save()
                if ok:
                    try: name = self._call('/v2/bot/profile/' + urllib.parse.quote(uid), token=self.access_token()).get('displayName', '')
                    except ValueError: name = ''
                    with self.lock:
                        if uid in self.config.get('recipients', {}): self.config['recipients'][uid]['name'] = name[:80]; self._save()
                    self._reply(event.get('replyToken'), '✅ Linked! You will get a message here when a print finishes or stops with an error.')
                elif linked:
                    self._reply(event.get('replyToken'), 'You are already linked to Print Center notifications.')
                else:
                    self._reply(event.get('replyToken'), 'Send the 6-digit LINE code shown in Print Center → Notifications to link this chat.')
        return True

    def send(self, event, message, wait=False):
        """Send one print notification to every linked LINE account, if this event is switched on."""
        with self.lock:
            c = self.config
            if not c.get('channel_secret') or (event != 'test' and (not c.get('enabled', True) or event not in c.get('events', EVENTS))):
                return 0
            users = list((c.get('recipients') or {}).keys())
        if not users: return 0
        text = ICONS.get(event, '') + ' ' + message['title'] + ('\n' + message['body'] if message.get('body') else '')
        def deliver():
            try:
                token = self.access_token()
                for start in range(0, len(users), 500):
                    self._call('/v2/bot/message/multicast', {'to': users[start:start + 500], 'messages': [{'type': 'text', 'text': text[:4900]}]}, token=token)
            except ValueError as failure:
                print('LINE notification failed:', failure, flush=True)
                if wait: raise
        if wait: deliver()
        else: threading.Thread(target=deliver, daemon=True, name='line-notify').start()
        return len(users)
