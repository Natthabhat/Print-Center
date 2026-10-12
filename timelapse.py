"""Persistent, time-based camera recording for Print Center."""
import json, pathlib, secrets, shutil, subprocess, threading, time, re

class Timelapses:
    def __init__(self, root):
        self.root = pathlib.Path(root)/'timelapses'
        self.lock = threading.RLock()
        self.render_lock = threading.Lock()
        self.thumbnail_lock = threading.Lock()
        self.active = {}

    def path(self, key):
        if not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{24}',key):
            raise ValueError('Invalid video ID')
        return self.root/key

    def save(self, row):
        folder=self.path(row['id']);folder.mkdir(parents=True,exist_ok=True)
        temp=folder/'record.tmp';temp.write_text(json.dumps(row));temp.replace(folder/'record.json')

    def thumbnail(self, key, printer):
        folder = self.path(key)
        row = json.loads((folder/'record.json').read_text())
        if row['printer'] != printer or row['status'] != 'ready':
            raise ValueError('Video is not available')
        target = folder/'thumbnail.jpg'
        with self.thumbnail_lock:
            if not target.exists():
                temporary = folder/'thumbnail.tmp.jpg'
                try:
                    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                        '-i',str(folder/'video.mp4'),'-frames:v','1','-vf','scale=480:-2',
                        '-threads','1',str(temporary)],check=True,timeout=20,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        return target.read_bytes()

    def list(self, printer):
        rows=[]
        if self.root.exists():
            for file in self.root.glob('*/record.json'):
                try:
                    row=json.loads(file.read_text())
                    if row['printer']==printer:
                        if row['status'] in ('waiting','recording','rendering') and row['id'] not in self.active.values():
                            row={**row,'status':'interrupted','error':'Recording interrupted by server restart; saved frames are retained.'}
                        rows.append(row)
                except (OSError,ValueError,KeyError):pass
        return sorted(rows,key=lambda r:r['created'],reverse=True)

    def prepare(self, printer, filename, snapshot, state, mode='interval'):
        if mode not in ('interval','layer'):raise ValueError('Invalid timelapse capture mode')
        if not shutil.which('ffmpeg'):raise ValueError('Timelapse renderer is unavailable')
        self.root.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(self.root).free<1024**3:raise ValueError('Timelapse needs at least 1 GB of free storage')
        with self.lock:
            if printer in self.active:raise ValueError('A timelapse is already recording for this printer')
            frame=snapshot()
            if not frame.startswith(b'\xff\xd8') or b'\xff\xd9' not in frame[-16:]:raise ValueError('Camera did not return a JPEG snapshot')
            row={'id':secrets.token_hex(12),'printer':printer,'filename':filename,'created':time.time(),'status':'waiting','frames':0,'interval':10,'fps':30,'error':'','mode':mode}
            self.save(row);self.active[printer]=row['id']
            threading.Thread(target=self.run,args=(row,snapshot,state),daemon=True).start()
            return row['id']

    def run(self,row,snapshot,state):
        folder=self.path(row['id']);frames=folder/'frames';frames.mkdir(exist_ok=True)
        started=False;deadline=time.monotonic()+300;last=0;errors=0;total=0;last_layer=None
        try:
            while True:
                try:
                    current=state();errors=0
                except Exception as error:
                    errors+=1;row['error']='Printer connection: '+str(error);self.save(row)
                    if errors>30:break
                    time.sleep(2);continue
                printing=current.get('state') in ('printing','paused')
                if not started:
                    if printing and current.get('filename')==row['filename']:started=True;row['status']='recording'
                    elif time.monotonic()>deadline:raise ValueError('Print did not start; recording was not captured')
                elif not printing or current.get('filename')!=row['filename']:break
                layer=current.get('info',{}).get('current_layer') if isinstance(current.get('info'),dict) else None
                valid_layer=type(layer) is int and layer>0
                layer_mode=row.get('mode','interval')=='layer'
                capture=valid_layer and layer!=last_layer and current.get('state')=='printing' if layer_mode else time.monotonic()-last>=row['interval']
                if started and layer_mode and not valid_layer:
                    row['error']='Waiting for layer updates from the sliced file';self.save(row)
                if started and capture:
                    try:
                        frame=snapshot()
                        if not frame.startswith(b'\xff\xd8') or b'\xff\xd9' not in frame[-16:]:raise ValueError('Incomplete camera frame')
                        if total+len(frame)>512*1024**2 or shutil.disk_usage(folder).free<512*1024**2:break
                        (frames/f'{row["frames"]:06d}.jpg').write_bytes(frame)
                        total+=len(frame);row['frames']+=1;row['error']=''
                        if layer_mode:last_layer=layer
                    except Exception as error:row['error']='Camera: '+str(error)
                    last=time.monotonic();self.save(row)
                time.sleep(.25 if layer_mode else 2)
            if not row['frames']:raise ValueError(row['error'] or 'No camera frames were captured')
            row['status']='rendering';self.save(row)
            self.render(row)
        except Exception as error:row['status']='error';row['error']=str(error);self.save(row)
        finally:
            with self.lock:self.active.pop(row['printer'],None)

    def render(self,row):
        folder=self.path(row['id'])
        with self.render_lock:
            result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-framerate',str(row['fps']),'-i',str(folder/'frames/%06d.jpg'),'-vf','scale=trunc(iw/2)*2:trunc(ih/2)*2','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-movflags','+faststart',str(folder/'video.mp4')],capture_output=True,timeout=900)
            if result.returncode:raise ValueError('Video rendering failed: '+result.stderr.decode(errors='replace')[-300:])
        row['status']='ready';row['bytes']=(folder/'video.mp4').stat().st_size;row['error']='';self.save(row)
        shutil.rmtree(folder/'frames')
