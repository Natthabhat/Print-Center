"""Phone notifications through standard Web Push, for Android browsers and iPhone Home Screen apps.

Messages are encrypted for each device (RFC 8291) and signed with this server's own VAPID key
(RFC 8292), so no third-party notification account is needed.
"""
import base64, json, os, pathlib, struct, threading, time, urllib.error, urllib.parse, urllib.request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

EVENTS = ('complete', 'error')
MAX_DEVICES = 50
# Only real browser push services, so a saved subscription can never make the server call into the local network.
PUSH_HOSTS = ('fcm.googleapis.com', 'android.googleapis.com', 'updates.push.services.mozilla.com')
# Chrome also hands out endpoints on Google hosts such as jmt17.google.com.
PUSH_SUFFIXES = ('.google.com', '.googleapis.com', '.push.apple.com', '.notify.windows.com', '.push.services.mozilla.com')
ACTIVE = ('printing', 'paused')
LANGUAGES = ('en', 'th')
TEXT = {
    'en': {'complete': '{name} finished printing', 'complete_body': 'Print complete',
           'error': '{name} stopped with an error', 'error_body': 'Check the printer',
           'test_push': 'Notifications are working on this device', 'test_line': 'LINE notifications are working',
           'line_follow': 'Hi! To get 3D printer notifications, open Print Center → Notifications and send me the 6-digit LINE code shown there.',
           'line_linked': '✅ Linked! You will get a message here when a print finishes or stops with an error.',
           'line_already': 'You are already linked to Print Center notifications.',
           'line_code': 'Send the 6-digit LINE code shown in Print Center → Notifications to link this chat.'},
    'th': {'complete': 'เครื่อง {name} ปริ้นเสร็จแล้ว', 'complete_body': 'ปริ้นเสร็จเรียบร้อย',
           'error': 'เครื่อง {name} หยุดปริ้นเพราะเกิดข้อผิดพลาด', 'error_body': 'กรุณาตรวจสอบเครื่องปริ้น',
           'test_push': 'การแจ้งเตือนบนอุปกรณ์นี้ใช้งานได้แล้ว', 'test_line': 'การแจ้งเตือนผ่าน LINE ใช้งานได้แล้ว',
           'line_follow': 'สวัสดี! หากต้องการรับการแจ้งเตือนเครื่องปริ้น 3D ให้เปิด Print Center → การแจ้งเตือน แล้วส่งรหัส LINE 6 หลักที่แสดงอยู่มาให้ฉัน',
           'line_linked': '✅ เชื่อมต่อแล้ว! คุณจะได้รับข้อความที่นี่เมื่อปริ้นเสร็จหรือหยุดเพราะเกิดข้อผิดพลาด',
           'line_already': 'บัญชีนี้เชื่อมต่อกับการแจ้งเตือนของ Print Center แล้ว',
           'line_code': 'ส่งรหัส LINE 6 หลักที่แสดงใน Print Center → การแจ้งเตือน เพื่อเชื่อมต่อแชทนี้'},
}


def b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def b64d(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def allowed_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 1024: return False
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or '').lower()
    return parts.scheme == 'https' and not parts.username and parts.port in (None, 443) and \
        (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIXES))


def encrypt(payload, p256dh, auth):
    """aes128gcm body for one device (RFC 8291)."""
    device = b64d(p256dh)
    secret = b64d(auth)
    local = ec.generate_private_key(ec.SECP256R1())
    local_public = local.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = local.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device))
    ikm = HKDF(hashes.SHA256(), 32, secret, b'WebPush: info\x00' + device + local_public).derive(shared)
    salt = os.urandom(16)
    key = HKDF(hashes.SHA256(), 16, salt, b'Content-Encoding: aes128gcm\x00').derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b'Content-Encoding: nonce\x00').derive(ikm)
    return salt + struct.pack('!IB', 4096, len(local_public)) + local_public + AESGCM(key).encrypt(nonce, payload + b'\x02', None)


def subscription(value):
    """Check a browser PushSubscription.toJSON() value and keep only what sending needs."""
    if not isinstance(value, dict): raise ValueError('Invalid notification subscription')
    endpoint, keys = value.get('endpoint'), value.get('keys') or {}
    if not allowed_endpoint(endpoint): raise ValueError('This browser uses an unsupported notification service')
    try:
        device, secret = b64d(keys['p256dh']), b64d(keys['auth'])
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device)
    except Exception:
        raise ValueError('Invalid notification subscription keys')
    if len(device) != 65 or len(secret) != 16: raise ValueError('Invalid notification subscription keys')
    return {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}


def transition(before, job):
    """'complete' or 'error' when a print just ended that way, else None. Nothing is sent for the first reading."""
    state = job.get('state')
    if before is None or state == before: return None
    if state == 'complete' and before in ACTIVE: return 'complete'
    if state == 'error': return 'error'
    return None


class Push:
    def __init__(self, folder, subject):
        self.folder = pathlib.Path(folder)
        self.path = self.folder / 'push-devices.json'
        self.subject = subject
        self.lock = threading.Lock()
        self.states = {}
        self.listeners = []  # other channels (LINE) called with (event, message)
        try: self.devices = json.loads(self.path.read_text())
        except (OSError, ValueError): self.devices = {}
        if not isinstance(self.devices, dict): self.devices = {}
        self._key = None
        self.settings_path = self.folder / 'notify-settings.json'
        try: self.language = json.loads(self.settings_path.read_text()).get('language')
        except (OSError, ValueError, AttributeError): self.language = None
        if self.language not in LANGUAGES: self.language = 'en'

    @property
    def key(self):
        # Made on first use, so merely importing the app never writes a key file.
        with self.lock:
            if self._key is None: self._key = self._load_key()
            return self._key

    def _load_key(self):
        path = self.folder / 'push-vapid.pem'
        try: return serialization.load_pem_private_key(path.read_bytes(), None)
        except (OSError, ValueError): pass
        key = ec.generate_private_key(ec.SECP256R1())
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(temp, 0o600)
        temp.replace(path)
        return key

    def text(self, key, **values):
        return TEXT[self.language][key].format(**values)

    def set_language(self, language):
        if language not in LANGUAGES: raise ValueError('Choose English or Thai')
        with self.lock:
            self.language = language
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_suffix('.tmp')
            temp.write_text(json.dumps({'language': language}))
            temp.replace(self.settings_path)
        return language

    def public_key(self):
        return b64e(self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    def _save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.devices))
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def subscribe(self, value, events):
        sub = subscription(value)
        if not isinstance(events, list) or any(e not in EVENTS for e in events): raise ValueError('Choose which notifications to get')
        with self.lock:
            if sub['endpoint'] not in self.devices and len(self.devices) >= MAX_DEVICES:
                raise ValueError('Too many devices have notifications on. Turn them off on an old device first.')
            self.devices[sub['endpoint']] = {**sub, 'events': sorted(set(events)), 'added': time.time()}
            self._save()
        return self.status(sub['endpoint'])

    def unsubscribe(self, endpoint):
        with self.lock:
            if self.devices.pop(endpoint, None) is not None: self._save()
        return {'subscribed': False, 'events': []}

    def status(self, endpoint):
        with self.lock:
            device = self.devices.get(endpoint) if isinstance(endpoint, str) else None
            return {'subscribed': bool(device), 'events': list(device['events']) if device else []}

    def _authorization(self, endpoint):
        parts = urllib.parse.urlsplit(endpoint)
        head = b64e(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
        body = b64e(json.dumps({'aud': parts.scheme + '://' + parts.netloc, 'exp': int(time.time()) + 12 * 3600,
                                'sub': self.subject}, separators=(',', ':')).encode())
        r, s = decode_dss_signature(self.key.sign((head + '.' + body).encode(), ec.ECDSA(hashes.SHA256())))
        return 'vapid t=%s.%s.%s, k=%s' % (head, body, b64e(r.to_bytes(32, 'big') + s.to_bytes(32, 'big')), self.public_key())

    def deliver(self, device, message, opener=urllib.request.urlopen):
        """Send to one device; returns True when the push service accepted it. Expired devices are removed."""
        raw = encrypt(json.dumps(message).encode(), device['keys']['p256dh'], device['keys']['auth'])
        request = urllib.request.Request(device['endpoint'], data=raw, method='POST', headers={
            'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream', 'TTL': '86400',
            'Urgency': 'high', 'Authorization': self._authorization(device['endpoint'])})
        try:
            with opener(request, timeout=15) as response: return 200 <= response.status < 300
        except urllib.error.HTTPError as failure:
            if failure.code in (404, 410): self.unsubscribe(device['endpoint'])
            return False
        except Exception:
            return False

    def send(self, message, event=None, endpoint=None, wait=False):
        with self.lock:
            devices = [dict(d) for k, d in self.devices.items()
                       if (endpoint is None or k == endpoint) and (event is None or event in d.get('events', []))]
        threads = [threading.Thread(target=self.deliver, args=(d, message), daemon=True) for d in devices]
        for t in threads: t.start()
        if wait:
            for t in threads: t.join(20)
        return len(devices)

    def watch(self, printer, job):
        """Call with each new print_stats reading; notifies when a print finishes or fails."""
        with self.lock:
            before = self.states.get(printer['id'])
            self.states[printer['id']] = job.get('state')
        event = transition(before, job)
        if not event: return None
        name = printer.get('name') or printer['id']
        filename = posix_name(job.get('filename'))
        if event == 'complete':
            message = {'title': self.text('complete', name=name), 'body': filename or self.text('complete_body')}
        else:
            detail = str(job.get('message') or '').strip()
            message = {'title': self.text('error', name=name), 'body': ' · '.join(x for x in (filename, detail[:200]) if x) or self.text('error_body')}
        message.update(tag=printer['id'] + '-' + event, url='/')
        self.send(message, event)
        for listener in self.listeners:
            try: listener(event, message)
            except Exception as failure: print('Notification failed:', failure, flush=True)
        return event


def posix_name(value):
    return str(value or '').rsplit('/', 1)[-1]
