"""Revocable sessions; remembered tokens survive server restarts."""
import hashlib, hmac, secrets, threading, time, json, os, pathlib, tempfile
from http.cookies import SimpleCookie, CookieError

class WebAuth:
    SESSION_AGE = 8 * 60 * 60
    REMEMBER_AGE = 30 * 24 * 60 * 60

    def __init__(self, store=None, credentials=''):
        self.lock = threading.Lock()
        self.sessions = {}
        self.failures = {}
        self.store = pathlib.Path(store) if store else None
        self.credentials = hashlib.sha256(credentials.encode()).hexdigest()
        self.remembered = set()
        if self.store:
            try:
                data = json.loads(self.store.read_text())
                if data.get('credentials') == self.credentials:
                    now = time.time()
                    self.sessions = {key:expiry for key,expiry in data.get('sessions',{}).items()
                                     if isinstance(key,str) and len(key)==64 and isinstance(expiry,(int,float)) and now < expiry <= now+self.REMEMBER_AGE}
                    self.remembered = set(self.sessions)
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def _key(self, token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _save(self):
        if not self.store: return
        self.store.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='sessions-', dir=self.store.parent)
        try:
            with os.fdopen(fd,'w') as target:
                json.dump({'credentials':self.credentials,'sessions':{key:expiry for key,expiry in self.sessions.items() if key in self.remembered and expiry > time.time()}},target)
            os.replace(temp,self.store)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def verify(self, username, password, expected_user, encoded_hash):
        try:
            salt, expected = encoded_hash.split(':', 1)
            actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200000).hex()
            return hmac.compare_digest(username, expected_user) and hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, AttributeError):
            return False

    def token(self, header):
        try:
            cookie = SimpleCookie(); cookie.load(header or '')
            return cookie['print_center_session'].value if 'print_center_session' in cookie else ''
        except CookieError:
            return ''

    def valid(self, header):
        with self.lock:
            return self.sessions.get(self._key(self.token(header)), 0) > time.time()

    def issue(self, remember=False):
        token = secrets.token_urlsafe(32)
        key = self._key(token)
        with self.lock:
            self.sessions = {t:expiry for t,expiry in self.sessions.items() if expiry > time.time()}
            if len(self.sessions) >= 1000: self.sessions.pop(next(iter(self.sessions)))
            self.remembered.intersection_update(self.sessions)
            self.sessions[key] = time.time() + (self.REMEMBER_AGE if remember else self.SESSION_AGE)
            if remember: self.remembered.add(key)
            try:
                self._save()
            except OSError:
                self.sessions.pop(key,None)
                self.remembered.discard(key)
                raise
        return token

    def revoke(self, header):
        with self.lock:
            key = self._key(self.token(header))
            self.sessions.pop(key, None)
            self.remembered.discard(key)
            self._save()

    def limited(self, address):
        with self.lock:
            now = time.time()
            self.failures = {k:v for k,v in self.failures.items() if v[1] > now}
            count, expiry = self.failures.get(address, (0, now + 60))
            self.failures[address] = (count + 1, expiry)
            return count >= 10

    def clear_failures(self, address):
        with self.lock: self.failures.pop(address, None)
