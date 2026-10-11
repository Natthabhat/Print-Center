import io,json,unittest,zipfile
from unittest.mock import patch
import app

class UploadTests(unittest.TestCase):
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
