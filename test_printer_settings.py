import unittest,tempfile,pathlib,json
from unittest.mock import patch
import app

class PrinterSettingsTests(unittest.TestCase):
 def test_profile_inference_validation_and_safe_public_fields(self):
  self.assertEqual(app.public_printer({'name':'Q2 1'})['profile'],'qidi-q2')
  self.assertEqual(app.public_printer({'name':'U1'})['profile'],'snapmaker-u1')
  self.assertEqual(app.public_printer({'name':'Custom','profile':'qidi-q2'})['profile'],'qidi-q2')
  self.assertEqual(app.validate_printer_profile('generic'),'generic')
  with self.assertRaises(ValueError):app.validate_printer_profile('invalid')
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
