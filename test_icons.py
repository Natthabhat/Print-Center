import io
import unittest
from unittest.mock import patch
import app


class IconTests(unittest.TestCase):
    def get(self, path, host='printer.example.com'):
        h = object.__new__(app.Handler)
        h.path = path
        h.command = 'GET'
        h.headers = {'Host': host}
        h.reply = lambda value, status=200, kind='application/json', headers=None, **kw: setattr(h, 'result', (status, value, kind, headers))
        with patch.object(app, 'PUBLIC_HOST', 'printer.example.com'), patch.object(app, 'AUTH_USER', 'owner'), patch.object(app, 'AUTH_HASH', 'hash'):
            h.do_GET()
        return h.result

    def test_icons_are_served_before_sign_in(self):
        for path, kind in (('/favicon.ico', 'image/x-icon'), ('/icons/favicon-32.png', 'image/png'), ('/icons/apple-touch-icon.png', 'image/png')):
            status, body, content_type, headers = self.get(path)
            self.assertEqual((status, content_type), (200, kind))
            self.assertTrue(body)
            self.assertIn('max-age', headers['Cache-Control'])

    def test_other_paths_still_require_sign_in(self):
        self.assertEqual(self.get('/icons/../app.py')[0], 401)
        self.assertEqual(self.get('/api/printers')[0], 401)

    def test_pages_link_the_icon(self):
        for page in ('index.html', 'login.html'):
            self.assertIn(b'href="/favicon.ico"', (app.ROOT/page).read_bytes())


if __name__ == '__main__':
    unittest.main()
