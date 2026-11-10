"""Shared Bambu RTSPS camera, with verified TLS and server-side credentials."""
import pathlib
import hashlib
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from bambu import certificate_serial


def rtsp_packet(buffer):
    if buffer.startswith(b'$'):
        if len(buffer)<4: return None
        size=4+int.from_bytes(buffer[2:4],'big')
    else:
        end=buffer.find(b'\r\n\r\n')
        if end<0:
            if len(buffer)>65536: raise ValueError('Oversized RTSP header')
            return None
        match=re.search(br'(?im)^Content-Length:\s*(\d+)',buffer[:end])
        length=int(match[1]) if match else 0
        if length>1048576: raise ValueError('Oversized RTSP message')
        size=end+4+length
    return (buffer[:size],buffer[size:]) if len(buffer)>=size else None


def rewrite_rtsp(packet, local_base, remote_base, password, outbound):
    if packet.startswith(b'$'): return packet
    head,body=packet.split(b'\r\n\r\n',1)
    text=head.decode('latin1')
    if outbound:
        text=text.replace(local_base,remote_base)
        # FFmpeg calculated Digest for the loopback URI; recompute for the verified printer URI.
        auth=re.search(r'(?im)^Authorization:\s*Digest (.+)$',text)
        if auth:
            pairs={m[1]:(m[2] if m[2] is not None else m[3]) for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))',auth[1])}
            required=('username','realm','nonce','uri')
            if all(k in pairs for k in required):
                method=text.split(' ',1)[0]
                digest=lambda v:hashlib.md5(v.encode()).hexdigest()
                a1=digest(pairs['username']+':'+pairs['realm']+':'+password)
                a2=digest(method+':'+pairs['uri'])
                if pairs.get('qop'):
                    response=digest(':'.join([a1,pairs['nonce'],pairs.get('nc',''),pairs.get('cnonce',''),pairs['qop'],a2]))
                else: response=digest(a1+':'+pairs['nonce']+':'+a2)
                line=re.sub(r'response="[^"]*"','response="'+response+'"',auth[0])
                text=text[:auth.start()]+line+text[auth.end():]
    else:
        text=text.replace(remote_base,local_base).replace(remote_base.replace('rtsps://','rtsp://'),local_base)
        body=body.replace(remote_base.encode(),local_base.encode()).replace(remote_base.replace('rtsps://','rtsp://').encode(),local_base.encode())
        text=re.sub(r'(?im)^Content-Length:\s*\d+','Content-Length: '+str(len(body)),text)
    return text.encode('latin1')+b'\r\n\r\n'+body


class CameraTunnel:
    """Loopback RTSP -> verified printer RTSPS. Video stays interleaved over TCP."""
    def __init__(self, printer):
        self.printer = printer
        self.stop = threading.Event()
        self.sockets = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(2)
        self.listener.settimeout(1)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.stop.is_set():
            try:
                local, _ = self.listener.accept()
                self.sockets.append(local)
                threading.Thread(target=self.bridge, args=(local,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                return

    def bridge(self, local):
        remote = None
        ended = threading.Event()
        try:
            host = urllib.parse.urlsplit(self.printer['url']).hostname
            ctx = ssl.create_default_context(cafile=str(pathlib.Path(__file__).with_name('bambu_ca.pem')))
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            ctx.check_hostname = False
            raw = socket.create_connection((host, 322), timeout=8)
            try:
                remote = ctx.wrap_socket(raw, server_hostname=self.printer.get('serial') or host)
            except Exception:
                raw.close()
                raise
            # Verify serial before forwarding any RTSP authentication data.
            certificate_serial(remote.getpeercert(), self.printer.get('serial', ''))
            self.sockets.append(remote)
            remote.settimeout(1)
            local.settimeout(1)
            local_base=f'rtsp://127.0.0.1:{self.port}'
            remote_base=f'rtsps://{host}:322'
            def pump(source, target, outbound):
                buffer=b''
                try:
                    while not ended.is_set() and not self.stop.is_set():
                        try:
                            chunk = source.recv(65536)
                        except socket.timeout:
                            continue
                        if not chunk: break
                        buffer+=chunk
                        while True:
                            packet=rtsp_packet(buffer)
                            if packet is None: break
                            message,buffer=packet
                            target.sendall(rewrite_rtsp(message,local_base,remote_base,self.printer['access_code'],outbound))
                except (OSError,ValueError):
                    pass
                finally:
                    ended.set()
            threading.Thread(target=pump, args=(local, remote, True), daemon=True).start()
            pump(remote, local, False)
        except (OSError, ssl.SSLError):
            pass
        finally:
            ended.set()
            local.close()
            if remote: remote.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        for sock in self.sockets:
            try: sock.close()
            except OSError: pass


class BambuCamera:
    def __init__(self, printer):
        self.printer = dict(printer)
        self.condition = threading.Condition()
        self.frame, self.frame_at, self.last_access = None, 0, 0
        self.running = False
        self.closed = False
        self.process = None

    def close(self):
        self.closed = True
        if self.process:
            try: self.process.kill()
            except OSError: pass

    def snapshot(self, opener=None):
        with self.condition:
            self.last_access = time.monotonic()
            if not self.running:
                self.running = True
                threading.Thread(target=self.run, daemon=True, name='bambu-camera-' + self.printer['id']).start()
            deadline = time.monotonic() + 10
            while not self.frame or time.monotonic() - self.frame_at > 5:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Bambu camera unavailable. Check LAN live view and the access code, then reconnect.')
                self.condition.wait(remaining)
            return self.frame

    def run(self):
        try:
            while not self.closed and time.monotonic() - self.last_access < 25:
                tunnel, process = None, None
                try:
                    tunnel = CameraTunnel(self.printer)
                    code = urllib.parse.quote(self.printer['access_code'], safe='')
                    url = f'rtsp://bblp:{code}@127.0.0.1:{tunnel.port}/streaming/live/1'
                    process = subprocess.Popen(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                        '-rtsp_transport', 'tcp', '-i', url, '-an', '-vf', 'fps=2,scale=960:-2',
                        '-threads', '2', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    self.process = process
                    started = time.monotonic()
                    def watch(proc=process, began=started):
                        while proc.poll() is None:
                            if self.closed or time.monotonic() - self.last_access > 25 or time.monotonic() - max(began, self.frame_at) > 20:
                                try: proc.kill()
                                except OSError: pass
                                return
                            time.sleep(1)
                    threading.Thread(target=watch, daemon=True).start()
                    buffer = b''
                    while chunk := process.stdout.read(8192):
                        buffer += chunk
                        while True:
                            start = buffer.find(b'\xff\xd8')
                            end = buffer.find(b'\xff\xd9', max(0, start))
                            if start < 0 or end < 0: break
                            frame, buffer = buffer[start:end+2], buffer[end+2:]
                            if len(frame) <= 4*1024*1024:
                                with self.condition:
                                    self.frame, self.frame_at = frame, time.monotonic()
                                    self.condition.notify_all()
                        if len(buffer) > 4*1024*1024: buffer = b''
                except (OSError, ValueError):
                    pass
                finally:
                    if process:
                        if process.poll() is None: process.kill()
                        process.wait(timeout=5)
                        if process.stdout: process.stdout.close()
                    if tunnel: tunnel.close()
                    self.process = None
                if not self.closed: time.sleep(3)
        finally:
            with self.condition:
                self.running = False
                self.condition.notify_all()
