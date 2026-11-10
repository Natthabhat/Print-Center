import io, pathlib, tempfile, unittest, uuid
from unittest.mock import patch
from model_library import ModelLibrary,filename,identifier,fields

class LibraryTests(unittest.TestCase):
 def test_identifiers_cannot_escape_storage(self):
  for value in ('../x','/etc/passwd',None,'a'*36):
   with self.assertRaises(ValueError):identifier(value)
  key=str(uuid.uuid4());self.assertEqual(identifier(key),key)
 def test_models_and_sliced_files_supported(self):
  for value in ('part.stl','part.obj','part.STEP','plate.3mf','print.gcode.3mf','ไทย.gcode'):
   self.assertEqual(filename(value),value)
 def test_bad_filenames_rejected(self):
  for value in ('../part.stl','a\\part.stl','.hidden.stl','bad\n.stl','script.html','x".gcode'):
   with self.assertRaises(ValueError):filename(value)
 def test_metadata_limits(self):
  self.assertEqual(fields({'title':' Bracket ','tags':['PETG','PETG']})[:3],('Bracket','',['PETG']))
  for data in ({'title':''},{'title':'x','tags':'bad'},{'title':'x','printer_profile':'other'},{'title':'x','notes':'x'*10001}):
   with self.assertRaises(ValueError):fields(data)
 def test_unconfigured_db_does_not_fall_back_to_local_file(self):
  with tempfile.TemporaryDirectory() as temp:
   lib=ModelLibrary(temp,dsn='')
   with self.assertRaisesRegex(ValueError,'not configured'):lib.list()
   self.assertFalse(lib.root.exists())
 def test_size_guard_runs_before_database(self):
  lib=ModelLibrary('/tmp',dsn='')
  with self.assertRaisesRegex(ValueError,'200 MB'):lib.upload(str(uuid.uuid4()),'part.stl',io.BytesIO(b'x'),201*1024*1024)

if __name__=='__main__':unittest.main()
