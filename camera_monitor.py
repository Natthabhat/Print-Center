"""Snapmaker's LAN camera snapshots, with bounded startup and freshness checks."""
import json
import threading
import time
from email.utils import parsedate_to_datetime
import urllib.request
from live_status import websocket


def fresh_jpeg(data, modified, now=None):
    try:
        age = (time.time() if now is None else now) - parsedate_to_datetime(modified).timestamp()
        return -5 <= age <= 15 and data.startswith(b'\xff\xd8\xff') and b'\xff\xd9' in data[-16:]
    except (TypeError, ValueError, AttributeError):
        return False


class SnapmakerCamera:
    def __init__(self, printer):
        self.printer = dict(printer)
        self.lock = threading.Lock()
        self.started = 0
        self.frame = None
        self.frame_at = 0

    def start(self):
        if websocket is None:
            raise ValueError('Camera WebSocket dependency is unavailable')
        url = self.printer['url'].replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + '/websocket'
        headers = ['X-Api-Key: ' + self.printer['api_key']] if self.printer.get('api_key') else []
        ws = websocket.create_connection(url, timeout=5, suppress_origin=True, header=headers)
        try:
            ws.send(json.dumps({'jsonrpc': '2.0', 'id': 1001, 'method': 'camera.start_monitor',
                                'params': {'domain': 'lan', 'interval': 0}}))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                value = json.loads(ws.recv())
                if value.get('id') == 1001:
                    if value.get('error') or value.get('result', {}).get('state') != 'success':
                        raise ValueError('Printer rejected camera monitoring')
                    self.started = time.monotonic()
                    return
            raise ValueError('Camera monitoring did not respond')
        finally:
            ws.close()

    def snapshot(self, opener):
        with self.lock:
            if self.frame and time.monotonic() - self.frame_at < 1:
                return self.frame
            if time.monotonic() - self.started > 20:
                self.start()
            deadline = time.monotonic() + 5
            headers = {'X-Api-Key': self.printer['api_key']} if self.printer.get('api_key') else {}
            request = urllib.request.Request(self.printer['url'] + '/server/files/camera/monitor.jpg', headers=headers)
            while True:
                with opener.open(request, timeout=5) as response:
                    data = response.read(4 * 1024 * 1024)
                    modified = response.headers.get('Last-Modified')
                if fresh_jpeg(data, modified):
                    self.frame, self.frame_at = data, time.monotonic()
                    return data
                if time.monotonic() >= deadline:
                    self.started = 0
                    raise ValueError('Camera has not produced a fresh complete image')
                time.sleep(.5)
