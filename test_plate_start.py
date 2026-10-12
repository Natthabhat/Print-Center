import io,zipfile,unittest,json
from unittest.mock import patch,Mock,call
import app
class PlateTests(unittest.TestCase):
 def test_3mf_start_uses_moonraker_staging_after_mapping(self):
  filename='extension grip.gcode.3mf'
  data=json.dumps({'action':'start','printer':'one','filename':filename,'use_box':True,'mapping':{'0':1}}).encode()
  handler=object.__new__(app.Handler)
  handler.path='/api/control';handler.headers={'Content-Length':str(len(data)),'X-Control-Token':app.TOKEN};handler.rfile=io.BytesIO(data)
  handler.host_ok=lambda:True;handler.origin_ok=lambda:True;handler.authenticated=lambda:True;handler.reply=Mock()
  def api(path,data=None,**kwargs):
   if path.startswith('/server/files/list'):return [{'path':filename}]
   if path.startswith('/printer/objects/query'):return {'status':{'print_stats':{'state':'standby','plateindex':'1'}}}
   return 'ok'
  with patch.object(app,'normalized_existing_print',return_value='normalized.gcode.3mf'),patch.object(app,'api',side_effect=api) as requests,patch.object(app,'selected',return_value={'id':'one'}),patch.object(app,'control_status',return_value={'multi_color_controller':{'hardware':{'connected':True}}}),patch.object(app,'print_plan',return_value={'plate':1,'filaments':[]}),patch.object(app,'mapping_script',return_value='SAVE_VARIABLE VARIABLE=enable_box VALUE=1'):
   handler.do_POST()
  self.assertEqual(requests.call_args_list[-2:], [call('/printer/gcode/script',{'script':'SAVE_VARIABLE VARIABLE=enable_box VALUE=1'}),call('/printer/print/start',{'filename':'normalized.gcode.3mf'},timeout=60)])
  self.assertTrue(handler.reply.call_args.args[0]['ok'])
 def archive(self,numbers):
  source=io.BytesIO()
  with zipfile.ZipFile(source,'w') as z:
   for n in numbers:z.writestr('Metadata/plate_'+str(n)+'.gcode','; test')
   z.writestr('Metadata/plate_1.png','thumbnail')
  source.seek(0)
  return zipfile.ZipFile(source)
 def test_actual_gcode_plate_selected_instead_of_thumbnail_or_default(self):
  with self.archive([14]) as archive:plates=app.plate_numbers(archive)
  self.assertEqual(plates,[14])
  self.assertEqual(app.choose_plate('Qidi ST002_plate_14.gcode.3mf',plates),14)
  self.assertEqual(app.choose_plate('renamed.gcode.3mf',plates),14)
  self.assertIn('PLATEINDEX="14"',app.qidi_start_script('Qidi ST002_plate_14.gcode.3mf',14))
 def test_multiple_plates_require_unambiguous_selection(self):
  self.assertEqual(app.choose_plate('file_plate_12.gcode.3mf',[1,12]),12)
  with self.assertRaises(ValueError):app.choose_plate('file.gcode.3mf',[1,12])
 def test_invalid_gcode_parameters_rejected(self):
  for filename,plate in [('bad".gcode.3mf',14),('file.gcode.3mf',0),('file.gcode.3mf','14'),('../file.gcode.3mf',14)]:
   with self.assertRaises(ValueError):app.qidi_start_script(filename,plate)
if __name__=='__main__':unittest.main()
