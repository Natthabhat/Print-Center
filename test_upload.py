import io,json,unittest,zipfile
from unittest.mock import patch
import app

class UploadTests(unittest.TestCase):
 def test_selected_plate_normalized_with_matching_thumbnail_metadata_and_unchanged_gcode(self):
  source=io.BytesIO(); output=io.BytesIO()
  with zipfile.ZipFile(source,'w') as z:
   z.writestr('Metadata/plate_2.gcode',b'; exact sliced toolpaths\nG1 X7\n')
   z.writestr('Metadata/plate_1.png',b'other thumbnail')
   z.writestr('Metadata/plate_2.png',b'selected thumbnail')
   z.writestr('Metadata/model_settings.config','<config><plate><metadata key="plater_id" value="2"/><metadata key="gcode_file" value="Metadata/plate_2.gcode"/></plate></config>')
   z.writestr('Metadata/slice_info.config','<config><plate><metadata key="index" value="2"/><filament id="2" type="PETG"/></plate></config>')
  length=app.normalize_print_archive(source,output,'part_plate_2.gcode.3mf')
  self.assertEqual(length,len(output.getvalue()))
  with zipfile.ZipFile(output) as z:
   self.assertEqual(app.plate_numbers(z),[1])
   self.assertEqual(z.read('Metadata/plate_1.gcode'),b'; exact sliced toolpaths\nG1 X7\n')
   self.assertEqual(z.read('Metadata/plate_1.png'),b'selected thumbnail')
   self.assertIn(b'key="index" value="1"',z.read('Metadata/slice_info.config'))
   self.assertIn(b'Metadata/plate_1.gcode',z.read('Metadata/model_settings.config'))
  self.assertEqual(app.plate_one_filename('part_plate_2.gcode.3mf'),'part_plate_1.gcode.3mf')
 def test_multiplate_selection_preserves_other_toolpaths(self):
  source=io.BytesIO();output=io.BytesIO()
  with zipfile.ZipFile(source,'w') as z:
   z.writestr('Metadata/plate_1.gcode',b'first')
   z.writestr('Metadata/plate_14.gcode',b'selected')
  app.normalize_print_archive(source,output,'part_plate_14.gcode.3mf')
  with zipfile.ZipFile(output) as z:
   self.assertEqual(z.read('Metadata/plate_1.gcode'),b'selected')
   self.assertEqual(z.read('Metadata/plate_14.gcode'),b'first')
 def test_filename_validation(self):
  self.assertEqual(app.upload_filename('ทดสอบ.gcode.3mf'),'ทดสอบ.gcode.3mf')
  for name in ['../config/printer.cfg','file.stl','file.3mf','/file.gcode','a\\file.gcode','a\r\n.gcode','a".gcode','.hidden.gcode']:
   with self.assertRaises(ValueError):app.upload_filename(name)
 def test_multipart_upload_is_gcodes_only_without_autoprint_and_renames_collision(self):
  captured=[]
  class Response:
   status=201
   def read(self,limit):return json.dumps({'result':{'item':{'root':'gcodes'}}}).encode()
  class Connection:
   def __init__(self,*a,**k):pass
   def putrequest(self,*a):captured.append(a)
   def putheader(self,*a):pass
   def endheaders(self):pass
   def send(self,b):captured.append(b)
   def getresponse(self):return Response()
   def close(self):pass
  app.CURRENT.printer={'id':'test','url':'http://192.168.1.2:7125','api_key':''}
  body=b'; no printer motion\n'
  with patch.object(app,'api',return_value=[{'path':'test.gcode'}]),patch.object(app.http.client,'HTTPConnection',Connection):
   result=app.upload_print_file(io.BytesIO(body),len(body),'test.gcode')
  self.assertNotEqual(result['filename'],'test.gcode')
  self.assertTrue(result['filename'].endswith('.gcode'))
  wire=b''.join(x for x in captured if isinstance(x,bytes))
  self.assertIn(b'name="root"\r\n\r\ngcodes',wire)
  self.assertIn(b'name="print"\r\n\r\nfalse',wire)
  self.assertIn(body,wire)
  self.assertEqual(captured[0],('POST','/server/files/upload'))
 def test_unsliced_archive_rejected(self):
  source=io.BytesIO()
  with zipfile.ZipFile(source,'w') as z:z.writestr('model.stl','test')
  source.seek(0)
  with patch.object(app,'api',return_value=[]):
   with self.assertRaises(ValueError):app.upload_print_file(source,len(source.getvalue()),'test.gcode.3mf')
if __name__=='__main__':unittest.main()
