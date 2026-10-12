import tempfile,pathlib,unittest,json
from unittest.mock import patch
from timelapse import Timelapses

class TimelapseTests(unittest.TestCase):
 def test_thumbnail_generated_once_and_requires_matching_printer(self):
  with tempfile.TemporaryDirectory() as root:
   t=Timelapses(root);key='e'*24
   t.save({'id':key,'printer':'p','status':'ready'})
   def ffmpeg(args,**kwargs):pathlib.Path(args[-1]).write_bytes(b'jpeg')
   with patch('timelapse.subprocess.run',side_effect=ffmpeg) as run:
    with self.assertRaises(ValueError):t.thumbnail(key,'other')
    self.assertEqual(t.thumbnail(key,'p'),b'jpeg')
    self.assertEqual(t.thumbnail(key,'p'),b'jpeg')
    self.assertEqual(run.call_count,1)
 def test_ids_cannot_escape_storage(self):
  t=Timelapses('/tmp/test')
  for value in ['../config','/etc/passwd','x',None]:
   with self.assertRaises(ValueError):t.path(value)
 def test_recording_captures_frames_and_finalizes_when_print_ends(self):
  with tempfile.TemporaryDirectory() as root:
   t=Timelapses(root);row={'id':'a'*24,'printer':'p','filename':'job.gcode','created':1,'status':'waiting','frames':0,'interval':10,'fps':30,'error':''};t.save(row);t.active['p']=row['id']
   states=iter([{'state':'printing','filename':'job.gcode'},{'state':'complete','filename':'job.gcode'}])
   def render(row):row['status']='ready';t.save(row)
   with patch('timelapse.time.sleep'),patch.object(t,'render',side_effect=render):t.run(row,lambda:b'\xff\xd8\xfftest\xff\xd9',lambda:next(states))
   self.assertEqual(row['frames'],1);self.assertEqual(t.list('p')[0]['status'],'ready');self.assertNotIn('p',t.active)
 def test_wrong_job_never_gets_recorded(self):
  with tempfile.TemporaryDirectory() as root:
   t=Timelapses(root);row={'id':'b'*24,'printer':'p','filename':'intended','created':1,'status':'waiting','frames':0,'interval':10,'fps':30,'error':''};t.save(row)
   with patch('timelapse.time.monotonic',side_effect=[0,301]),patch('timelapse.time.sleep'):
    t.run(row,lambda:self.fail('Wrong job must not capture'),lambda:{'state':'printing','filename':'different'})
   self.assertEqual(row['status'],'error');self.assertEqual(row['frames'],0)
 def test_list_retains_finished_videos_and_marks_interrupted(self):
  with tempfile.TemporaryDirectory() as root:
   t=Timelapses(root);t.save({'id':'c'*24,'printer':'p','created':1,'status':'recording','frames':2});t.save({'id':'d'*24,'printer':'other','created':2,'status':'ready'})
   self.assertEqual(len(t.list('p')),1);self.assertEqual(t.list('p')[0]['status'],'interrupted')
