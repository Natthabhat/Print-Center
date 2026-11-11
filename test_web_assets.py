import gzip
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock
import app
from web_assets import StaticFiles, accepts_gzip, etag_matches, home_payload


def handler(headers=None):
    h = object.__new__(app.Handler)
    h.headers = headers or {}
    h.wfile = io.BytesIO()
    h.sent = []
    h.send_response = lambda status: h.sent.append(('status', status))
    h.send_header = lambda k, v: h.sent.append((k, v))
    h.end_headers = lambda: None
    return h


class CompressionTests(unittest.TestCase):
    def test_accept_encoding(self):
        self.assertTrue(accepts_gzip({'Accept-Encoding': 'gzip, deflate, br'}))
        self.assertFalse(accepts_gzip({'Accept-Encoding': 'br, gzip;q=0'}))
        self.assertFalse(accepts_gzip({}))

    def test_large_json_is_gzipped_only_when_accepted(self):
        value = {'rows': ['x' * 50] * 100}
        h = handler({'Accept-Encoding': 'gzip'}); h.reply(value)
        self.assertIn(('Content-Encoding', 'gzip'), h.sent)
        self.assertEqual(json.loads(gzip.decompress(h.wfile.getvalue())), value)
        h = handler(); h.reply(value)
        self.assertNotIn(('Content-Encoding', 'gzip'), h.sent)
        self.assertEqual(json.loads(h.wfile.getvalue()), value)

    def test_small_and_binary_replies_are_left_alone(self):
        h = handler({'Accept-Encoding': 'gzip'}); h.reply({'ok': True})
        self.assertNotIn(('Content-Encoding', 'gzip'), h.sent)
        h = handler({'Accept-Encoding': 'gzip'}); h.reply(b'\x89PNG' * 1000, kind='image/png')
        self.assertNotIn(('Content-Encoding', 'gzip'), h.sent)


class StaticTests(unittest.TestCase):
    def test_unchanged_file_returns_304(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        path = pathlib.Path(folder.name) / 'a.js'; path.write_text('let a=1;' * 400)
        h = handler({'Accept-Encoding': 'gzip'}); h.send_static(path, 'text/javascript', 'private, no-cache')
        etag = dict(h.sent)['ETag']
        self.assertEqual(gzip.decompress(h.wfile.getvalue()), path.read_bytes())
        h = handler({'If-None-Match': etag}); h.send_static(path, 'text/javascript', 'private, no-cache')
        self.assertIn(('status', 304), h.sent); self.assertEqual(h.wfile.getvalue(), b'')
        self.assertTrue(etag_matches({'If-None-Match': 'W/' + etag}, etag))

    def test_page_scripts_get_content_hashes(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        root = pathlib.Path(folder.name); (root / 'app.js').write_text('one'); (root / 'library.js').write_text('two')
        files = StaticFiles()
        page = files.versioned_page(b'<script src="/app.js?v=old"></script><script src="/library.js?v=x">', root)
        self.assertIn(b'/app.js?v=' + files.get(root / 'app.js')['hash'].encode(), page)
        (root / 'app.js').write_text('changed')
        self.assertNotEqual(files.versioned_page(b'<script src="/app.js?v=old">', root), page[:len(page)])

    def test_icon_links_get_content_hashes(self):
        for name in ('index.html', 'login.html'):
            page = app.STATIC.versioned_page((app.ROOT / name).read_bytes(), app.ROOT)
            self.assertIn(b'href="/favicon.ico?v=' + app.STATIC.get(app.ROOT / 'icons' / 'favicon.ico')['hash'].encode() + b'"', page)
            self.assertNotIn(b'.png"', page.split(b'</head>')[0])

    def test_real_page_references_real_files(self):
        page = app.STATIC.versioned_page((app.ROOT / 'index.html').read_bytes(), app.ROOT)
        self.assertNotIn(b'app.js?v=caselight', page)
        for name in app.PRINTER_MODELS: self.assertTrue((app.ROOT / 'printer-models' / name).is_file())
        for name in app.ICONS.values(): self.assertTrue((app.ROOT / 'icons' / name).is_file())


class HomePayloadTests(unittest.TestCase):
    def test_keeps_only_card_fields(self):
        data = {'status': {'print_stats': {'state': 'printing', 'filename': 'a.gcode'}, 'virtual_sdcard': {'progress': .5, 'file_position': 9},
                           'toolhead': {'position': [1, 2, 3]}, 'extruder': {'temperature': 200}},
                'display_state': 'printing', 'temperatures': [{'id': 'extruder'}], 'printer': {'id': 'q'},
                'controls': [{'id': 'fan', 'value': 1}, {'id': 'output_pin caselight', 'value': 0, 'editable': True}]}
        slim = home_payload(data)
        self.assertEqual(slim['status'], {'print_stats': {'state': 'printing', 'filename': 'a.gcode'}, 'virtual_sdcard': {'progress': .5}})
        self.assertEqual([r['id'] for r in slim['controls']], ['output_pin caselight'])
        self.assertNotIn('temperatures', slim)
        self.assertEqual(home_payload({'error': 'down', 'offline': True}), {'error': 'down', 'offline': True})


if __name__ == '__main__':
    unittest.main()
