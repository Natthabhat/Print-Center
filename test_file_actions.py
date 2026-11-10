import unittest
from unittest.mock import patch,MagicMock
import app

class FileActionTests(unittest.TestCase):
    def test_existing_file_preserves_nested_and_unicode_name(self):
        name='parts/my part ไทย.gcode.3mf'
        with patch.object(app,'api',return_value=[{'path':name}]):self.assertEqual(app.existing_printer_file(name),name)
    def test_invalid_paths_rejected_without_network(self):
        with patch.object(app,'api') as api:
            for name in ('../a.gcode','a/../b.gcode','/a.gcode','a\\b.gcode','a\r\nx.gcode','a\0.gcode',None):
                with self.assertRaises(ValueError):app.existing_printer_file(name)
            api.assert_not_called()
    def test_missing_file_rejected(self):
        with patch.object(app,'api',return_value=[]):
            with self.assertRaises(ValueError):app.existing_printer_file('missing.gcode')
    def test_delete_uses_explicit_method_and_printer_key(self):
        app.CURRENT.printer={'url':'http://printer:7125','api_key':'test-key'}
        response=MagicMock();response.read.return_value=b'{"result": {"action": "delete_file"}}'
        response.__enter__.return_value=response
        with patch.object(app.OPENER,'open',return_value=response) as opened:
            self.assertEqual(app.api('/server/files/gcodes/test.gcode',method='DELETE')['action'],'delete_file')
            request=opened.call_args.args[0]
            self.assertEqual(request.get_method(),'DELETE')
            self.assertEqual(request.get_header('X-api-key'),'test-key')

if __name__=='__main__':unittest.main()
