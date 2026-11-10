"""One bounded snapshot producer per printer, shared by all viewers."""
import threading,time
class FrameCache:
 def __init__(self,read):
  self.read=read;self.condition=threading.Condition();self.stop=threading.Event()
  self.frame=None;self.at=0;self.viewed=0;self.error='Camera warming up'
  threading.Thread(target=self.run,daemon=True).start()
 def close(self):self.stop.set()
 def run(self):
  while not self.stop.is_set():
   try:
    frame=self.read()
    if not isinstance(frame,bytes) or len(frame)>4*1024*1024 or not frame.startswith(b'\xff\xd8') or b'\xff\xd9' not in frame[-16:]:raise ValueError('Camera returned an incomplete JPEG')
    with self.condition:
     self.frame=frame;self.at=time.monotonic();self.error=None;self.condition.notify_all()
   except Exception as e:
    with self.condition:self.error=str(e);self.condition.notify_all()
   self.stop.wait(3 if self.error else .5 if time.monotonic()-self.viewed<15 else 5)
 def snapshot(self):
  with self.condition:
   self.viewed=time.monotonic()
   if self.frame is None:self.condition.wait_for(lambda:self.frame is not None or self.stop.is_set(),timeout=6)
   if self.frame is None or time.monotonic()-self.at>12:raise ValueError(self.error or 'Camera frame expired')
   return self.frame
