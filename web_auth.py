"""Short-lived, revocable server-side login sessions."""
import hashlib, hmac, secrets, threading, time
from http.cookies import SimpleCookie, CookieError

class WebAuth:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}
        self.failures = {}

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
            return self.sessions.get(self.token(header), 0) > time.time()

    def issue(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions = {t:expiry for t,expiry in self.sessions.items() if expiry > time.time()}
            if len(self.sessions) >= 1000: self.sessions.pop(next(iter(self.sessions)))
            self.sessions[token] = time.time() + 28800
        return token

    def revoke(self, header):
        with self.lock: self.sessions.pop(self.token(header), None)

    def limited(self, address):
        with self.lock:
            now = time.time()
            self.failures = {k:v for k,v in self.failures.items() if v[1] > now}
            count, expiry = self.failures.get(address, (0, now + 60))
            self.failures[address] = (count + 1, expiry)
            return count >= 10

    def clear_failures(self, address):
        with self.lock: self.failures.pop(address, None)
