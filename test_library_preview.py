import base64,io,pathlib,tempfile,unittest,zipfile
from unittest.mock import patch
from PIL import Image
from model_library import ModelLibrary

class LibraryPreviewTests(unittest.TestCase):
 def png(self):
  image=Image.new('RGB',(40,20),'red');out=io.BytesIO();image.save(out,'PNG');return out.getvalue()
 def test_gcode_embedded_thumbnail_and_cache(self):
  with tempfile.TemporaryDirectory() as temp:
   lib=ModelLibrary(temp,'');lib.root.mkdir();path=pathlib.Path(temp)/'print.gcode';path.write_bytes(b'; thumbnail begin 40x20 100\n; '+base64.b64encode(self.png())+b'\n; thumbnail end\n')
   with patch.object(lib,'file',return_value=({'filename':'print.gcode','kind':'sliced'},path)):
    raw=lib.preview('00000000-0000-0000-0000-000000000001');self.assertEqual(Image.open(io.BytesIO(raw)).size,(40,20));self.assertEqual(lib.preview('00000000-0000-0000-0000-000000000001'),raw)
 def test_3mf_preview(self):
  with tempfile.TemporaryDirectory() as temp:
   lib=ModelLibrary(temp,'');lib.root.mkdir();path=pathlib.Path(temp)/'print.3mf'
   with zipfile.ZipFile(path,'w') as z:z.writestr('Metadata/plate_1.png',self.png())
   with patch.object(lib,'file',return_value=({'filename':'print.3mf','kind':'sliced'},path)):
    self.assertEqual(Image.open(io.BytesIO(lib.preview('00000000-0000-0000-0000-000000000002'))).size,(40,20))
 def test_missing_preview_is_clear_error(self):
  with tempfile.TemporaryDirectory() as temp:
   lib=ModelLibrary(temp,'');lib.root.mkdir();path=pathlib.Path(temp)/'print.gcode';path.write_text('; no thumbnail')
   with patch.object(lib,'file',return_value=({'filename':'print.gcode','kind':'sliced'},path)):
    with self.assertRaisesRegex(ValueError,'No embedded print preview'):lib.preview('00000000-0000-0000-0000-000000000003')
