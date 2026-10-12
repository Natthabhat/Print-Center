import unittest,tempfile,pathlib,json
from unittest.mock import patch
import app

class PrinterSettingsTests(unittest.TestCase):
 def test_settings_persist_for_selected_printer_only_without_exposing_api_key(self):
  with tempfile.TemporaryDirectory() as root:
   config=pathlib.Path(root)/'printers.json'
   config.write_text(json.dumps([{'id':'a','name':'A','api_key':'private'},{'id':'b','name':'B','timelapse_mode':'interval'}]))
   with patch.object(app,'CONFIG',config):
    result=app.save_printer_settings('a','layer')
    self.assertEqual(app.selected('a')['timelapse_mode'],'layer')
    self.assertEqual(app.selected('b')['timelapse_mode'],'interval')
    self.assertNotIn('api_key',result)
    before=config.read_text()
    with self.assertRaises(ValueError):app.save_printer_settings('a','unsupported')
    self.assertEqual(config.read_text(),before)
