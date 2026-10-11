import unittest, tempfile, pathlib, io, json, threading
from unittest.mock import patch
import app

class DashboardTests(unittest.TestCase):
    def test_public_login_protects_status_and_controls(self):
        import hashlib, base64
        salt = bytes.fromhex('12' * 16)
        encoded_hash = salt.hex() + ':' + hashlib.pbkdf2_hmac('sha256', b'test-password', salt, 200000).hex()
        handler = object.__new__(app.Handler)
        handler.send_response = lambda code: setattr(handler, 'code', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.reply = lambda value, code=200, **kwargs: setattr(handler, 'code', code)
        handler.command, handler.path = 'GET', '/api/printers'
        with patch.object(app, 'PUBLIC_HOST', 'printer.example.com'), patch.object(app, 'AUTH_USER', 'owner'), patch.object(app, 'AUTH_HASH', encoded_hash):
            handler.headers = {'Host': 'printer.example.com'}
            self.assertFalse(handler.authenticated())
            self.assertEqual(handler.code, 401)
            self.assertTrue(app.LOGIN.verify('owner', 'test-password', 'owner', encoded_hash))
            self.assertFalse(app.LOGIN.verify('owner', 'wrong', 'owner', encoded_hash))
            token = app.LOGIN.issue()
            handler.headers['Cookie'] = 'print_center_session=' + token
            self.assertTrue(handler.authenticated())
            app.LOGIN.revoke(handler.headers['Cookie'])
            self.assertFalse(handler.authenticated())
            handler.headers = {'Host': '127.0.0.1:8787'}
            self.assertTrue(handler.authenticated())
            handler.headers = {'Host': 'printer.example.com'}
            with patch.object(app, 'AUTH_HASH', ''):
                self.assertFalse(handler.authenticated())
                self.assertEqual(handler.code, 503)

    def test_public_host_and_https_origin_are_explicit(self):
        handler = object.__new__(app.Handler)
        with patch.object(app, 'PUBLIC_HOST', 'printer.example.com'):
            handler.headers = {'Host': 'printer.example.com', 'Origin': 'https://printer.example.com'}
            self.assertTrue(handler.host_ok())
            self.assertTrue(handler.origin_ok())
            handler.headers['Origin'] = 'https://other.example.com'
            self.assertFalse(handler.origin_ok())
            handler.headers['Origin'] = 'http://printer.example.com'
            self.assertFalse(handler.origin_ok())
            handler.headers = {'Host': 'other.example.com'}
            self.assertFalse(handler.host_ok())
        with patch.object(app, 'PUBLIC_HOST', ''):
            handler.headers = {'Host': 'printer.example.com'}
            self.assertFalse(handler.host_ok())

    def test_extra_interface_host_validation(self):
        handler = object.__new__(app.Handler)
        with patch.object(app, 'BIND', '100.100.100.100'), patch.object(app, 'EXTRA_BIND', '192.168.1.36'):
            for host in ['100.100.100.100:8787', '192.168.1.36:8787']:
                handler.headers = {'Host': host}
                self.assertTrue(handler.host_ok())
            handler.headers = {'Host': '192.168.1.37:8787'}
            self.assertFalse(handler.host_ok())

    def test_private_addresses_and_ports(self):
        self.assertEqual(app.valid_url('192.168.31.129', 7125), 'http://192.168.31.129:7125')
        self.assertEqual(app.valid_url('100.100.100.100:7126', 7125), 'http://100.100.100.100:7126')
        for url in ['127.0.0.1', '169.254.169.254', 'https://example.com', 'http://192.168.1.1/admin', 'http://user:pass@192.168.1.1']:
            with self.assertRaises(ValueError): app.valid_url(url, 7125)

    def test_registry_persists_and_redacts_keys(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'CONFIG', pathlib.Path(folder)/'printers.json'):
            values = [{'id':'one','name':'First','url':'http://10.0.0.1:7125','camera_base':'http://10.0.0.1','api_key':'private-key'}, {'id':'two','name':'Second','url':'http://10.0.0.2:7125','camera_base':'http://10.0.0.2','api_key':''}]
            app.save_printers(values)
            self.assertEqual(app.selected('two')['name'], 'Second')
            self.assertEqual(app.printers(), values)
            self.assertNotIn('api_key', app.public_printer(values[0]))
            self.assertTrue(app.public_printer(values[0])['has_api_key'])
            with self.assertRaises(ValueError): app.selected(None)

    def test_concurrent_printer_routing(self):
        captured=[]
        class Opener:
            def open(self, request, **kwargs):
                captured.append((request.full_url, request.headers.get('X-api-key')))
                return io.BytesIO(json.dumps({'result':{'ok':True}}).encode())
        def read(host,key):
            app.CURRENT.printer={'id':host,'url':host,'api_key':key}
            app.api('/printer/info')
        with patch.object(app,'OPENER',Opener()):
            threads=[threading.Thread(target=read,args=('http://10.0.0.1:7125','one')), threading.Thread(target=read,args=('http://10.0.0.2:7125','two'))]
            for t in threads:t.start()
            for t in threads:t.join()
        self.assertCountEqual(captured,[('http://10.0.0.1:7125/printer/info','one'),('http://10.0.0.2:7125/printer/info','two')])

    def test_thumbnail_traversal_rejected_before_connection(self):
        for name in ['../config/printer.cfg','/etc/passwd','sub/../../x','sub\\file.gcode']:
            with self.assertRaises(ValueError): app.thumbnail(name)

    def test_heater_targets_use_configuration_and_validate_commands(self):
        status = {'heaters': {'available_heaters': ['extruder', 'extruder1', 'heater_generic chamber', 'heater_generic heater_box1']},
                  'extruder': {'temperature': 44, 'target': 0, 'power': 0},
                  'extruder1': {'temperature': 45, 'target': 220, 'power': .4},
                  'heater_generic chamber': {'temperature': 34, 'target': 0, 'power': 0},
                  'heater_generic heater_box1': {'temperature': 39, 'target': 0, 'power': 0},
                  'temperature_sensor protection': {'temperature': 37}}
        rows = app.temperature_rows(status, {'extruder': {'max_temp': 375}, 'extruder1': {'max_temp': 300}, 'heater_generic chamber': {'max_temp': 70}})
        self.assertEqual(app.heat_script('extruder1', 250, rows), 'SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=250')
        self.assertEqual(app.heat_script('heater_generic chamber', 0, rows), 'SET_HEATER_TEMPERATURE HEATER=chamber TARGET=0')
        self.assertEqual(next(r for r in rows if r['id']=='heater_generic heater_box1')['max'], 70)
        self.assertFalse(next(r for r in rows if r['id']=='temperature_sensor protection')['editable'])
        for heater, target in [('heater_generic chamber', 71), ('extruder1', 301), ('extruder', float('nan')), ('extruder', float('inf')), ('extruder', -1), ('temperature_sensor protection', 50), ('chamber\nRESTART', 0)]:
            with self.assertRaises(ValueError): app.heat_script(heater, target, rows)

    def test_temperature_fan_command_and_positive_minimum(self):
        rows = app.temperature_rows({'temperature_fan chamber': {'temperature': 30, 'target': 40, 'speed': .5}}, {'temperature_fan chamber': {'min_temp': 10, 'max_temp': 70}})
        self.assertEqual(app.heat_script('temperature_fan chamber', 40, rows), 'SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber TARGET=40')
        app.heat_script('temperature_fan chamber', 0, rows)
        with self.assertRaises(ValueError): app.heat_script('temperature_fan chamber', 5, rows)

if __name__=='__main__': unittest.main()
