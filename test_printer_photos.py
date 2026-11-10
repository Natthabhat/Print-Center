import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch, Mock
from PIL import Image
import app
from printer_photos import prepare_photo, photo_url

def sample():
    output = io.BytesIO()
    Image.new('RGB', (900, 600), 'teal').save(output, format='PNG')
    return output.getvalue()

class PrinterPhotoTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.config = pathlib.Path(self.folder.name)/'printers.json'
        self.patch = patch.object(app, 'CONFIG', self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app.save_printers([{'id':pid,'name':pid,'profile':'qidi-q2','url':f'http://192.168.1.{i}:7125','camera_base':f'http://192.168.1.{i}','api_key':'secret'} for i,pid in enumerate(['one','two'],10)])

    def handler(self, path, data, token=None):
        h = object.__new__(app.Handler)
        h.path = path
        h.rfile = io.BytesIO(data)
        h.headers = {'Content-Length':str(len(data)), 'X-Control-Token':app.TOKEN if token is None else token}
        h.connection = Mock()
        h.host_ok = lambda: True
        h.origin_ok = lambda: True
        h.authenticated = lambda: True
        h.reply = lambda value,status=200,**kw: setattr(h,'result',(status,value,kw))
        return h

    def post(self,path,raw):
        h=self.handler(path,raw)
        h.do_POST()
        return h.result

    def test_photo_is_optimized_and_per_printer(self):
        code,result,_=self.post('/api/printer-photo?printer=one',sample())
        self.assertEqual(code,200)
        self.assertTrue(result['printer']['photo_url'].startswith('/printer-photos/'))
        one,two=app.printers()
        self.assertNotIn('photo',two)
        self.assertEqual(one['api_key'],'secret')
        image=Image.open(self.config.parent/'printer-photos'/one['photo'])
        self.assertEqual(image.format,'WEBP')
        self.assertLessEqual(max(image.size),512)
        self.assertNotIn('api_key',result['printer'])

    def test_connection_edit_keeps_photo_and_reset_restores_default(self):
        self.post('/api/printer-photo?printer=one',sample())
        original=app.printers()[0]['photo']
        code,_,_=self.post('/api/printers',json.dumps({'action':'edit','id':'one','name':'Updated','profile':'qidi-q2','url':'192.168.1.10:7125'}).encode())
        self.assertEqual(code,200)
        self.assertEqual(app.printers()[0]['photo'],original)
        code,reply,_=self.post('/api/printer-photo?printer=one&reset=1',b'{}')
        self.assertEqual(code,200)
        self.assertEqual(reply['printer']['photo_url'],'')
        self.assertNotIn('photo',app.printers()[0])

    def test_invalid_upload_does_not_replace_photo(self):
        self.post('/api/printer-photo?printer=one',sample())
        original=app.printers()[0]['photo']
        for raw in (b'<svg xmlns="http://www.w3.org/2000/svg"/>',b'not an image',sample()[:30]):
            code,_,_=self.post('/api/printer-photo?printer=one',raw)
            self.assertEqual(code,400)
            self.assertEqual(app.printers()[0]['photo'],original)

    def test_unknown_printer_and_csrf_are_rejected(self):
        self.assertEqual(self.post('/api/printer-photo?printer=missing',sample())[0],400)
        h=self.handler('/api/printer-photo?printer=one',sample(),token='invalid')
        h.do_POST()
        self.assertEqual(h.result[0],403)

    def test_photos_require_a_saved_reference_and_safe_path(self):
        for path in ('/printer-photos/../printers.json','/printer-photos/'+'a'*64+'.webp'):
            h=self.handler(path,b'')
            h.do_GET()
            self.assertEqual(h.result[0],404)
        self.assertEqual(photo_url({'photo':'../printers.json'}),'')
        _,result,_=self.post('/api/printer-photo?printer=one',sample())
        h=self.handler(result['printer']['photo_url'],b'')
        h.do_GET()
        self.assertEqual(h.result[0],200)
        self.assertEqual(h.result[2]['kind'],'image/webp')

    def test_large_images_rejected(self):
        with self.assertRaises(ValueError):prepare_photo(b'x'*(5*1024*1024+1))
        output=io.BytesIO()
        Image.new('1',(4100,4100)).save(output,format='PNG')
        with self.assertRaises(ValueError):prepare_photo(output.getvalue())

if __name__ == '__main__':unittest.main()

