import io,zipfile,unittest
import app
class PlateTests(unittest.TestCase):
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
