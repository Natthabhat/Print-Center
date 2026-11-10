import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch, Mock
import app
from web_auth import WebAuth

class RememberLoginTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.file=pathlib.Path(self.folder.name)/'sessions.json'

    def test_remembered_session_survives_restart_and_logout_is_durable(self):
        auth=WebAuth(self.file,'credentials')
        token=auth.issue(remember=True)
        cookie='print_center_session='+token
        self.assertNotIn(token,self.file.read_text())
        restarted=WebAuth(self.file,'credentials')
        self.assertTrue(restarted.valid(cookie))
        restarted.revoke(cookie)
        self.assertFalse(WebAuth(self.file,'credentials').valid(cookie))

    def test_normal_session_is_not_persisted(self):
        auth=WebAuth(self.file,'credentials')
        cookie='print_center_session='+auth.issue()
        self.assertTrue(auth.valid(cookie))
        self.assertFalse(WebAuth(self.file,'credentials').valid(cookie))

    def test_credential_change_invalidates_remembered_session(self):
        token=WebAuth(self.file,'old credentials').issue(remember=True)
        self.assertFalse(WebAuth(self.file,'new credentials').valid('print_center_session='+token))

    def test_remembered_session_expires_after_30_days(self):
        with patch('web_auth.time.time',return_value=100):
            auth=WebAuth(self.file,'credentials')
            cookie='print_center_session='+auth.issue(remember=True)
        with patch('web_auth.time.time',return_value=100+WebAuth.REMEMBER_AGE-1):
            self.assertTrue(auth.valid(cookie))
            self.assertTrue(WebAuth(self.file,'credentials').valid(cookie))
        with patch('web_auth.time.time',return_value=100+WebAuth.REMEMBER_AGE):
            self.assertFalse(auth.valid(cookie))
            self.assertFalse(WebAuth(self.file,'credentials').valid(cookie))

    def test_invalid_store_fails_closed(self):
        self.file.write_text('not json')
        self.assertFalse(WebAuth(self.file,'credentials').valid('print_center_session=anything'))

    def test_login_cookies_have_correct_lifetime_and_security_flags(self):
        for remember in (False,True):
            auth=WebAuth(self.file,'credentials')
            h=object.__new__(app.Handler)
            body=json.dumps({'username':'owner','password':'test','remember':remember}).encode()
            h.headers={'Host':'printer.example.com','X-Login-Token':app.LOGIN_TOKEN,'Content-Length':str(len(body))}
            h.rfile=io.BytesIO(body)
            h.connection=Mock()
            h.client_address=('client',1234)
            h.origin_ok=lambda:True
            h.reply=lambda value,status=200,**kw:setattr(h,'result',(status,value,kw))
            with patch.object(app,'PUBLIC_HOST','printer.example.com'),patch.object(app,'AUTH_USER','owner'),patch.object(app,'AUTH_HASH','hash'),patch.object(app,'LOGIN',auth),patch.object(auth,'verify',return_value=True):
                h.login()
            self.assertEqual(h.result[0],200)
            cookie=h.result[2]['headers']['Set-Cookie']
            for flag in ('HttpOnly','Secure','SameSite=Strict'):self.assertIn(flag,cookie)
            if remember:self.assertIn('Max-Age=2592000',cookie)
            else:self.assertNotIn('Max-Age',cookie)

if __name__=='__main__':unittest.main()
