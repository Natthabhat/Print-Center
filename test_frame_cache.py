import unittest,time,threading
from frame_cache import FrameCache
class FrameTests(unittest.TestCase):
 def test_readers_share_frame_and_stale_frames_fail(self):
  calls=[]
  cache=FrameCache(lambda:(calls.append(1) or b'\xff\xd8image\xff\xd9'))
  try:
   self.assertEqual(cache.snapshot(),b'\xff\xd8image\xff\xd9')
   for _ in range(10):cache.snapshot()
   self.assertEqual(len(calls),1)
   with cache.condition:cache.at=time.monotonic()-20
   with self.assertRaises(ValueError):cache.snapshot()
  finally:cache.close()
 def test_invalid_image_not_cached(self):
  event=threading.Event()
  def read():event.set();return b'not jpeg'
  cache=FrameCache(read)
  try:
   event.wait(1)
   time.sleep(.01)
   self.assertIsNone(cache.frame)
   self.assertIn('JPEG',cache.error)
  finally:cache.close()
if __name__=='__main__':unittest.main()
