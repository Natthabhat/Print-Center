"""Shared read-only Moonraker subscriptions and HTTP fallback."""
import copy, json, threading, time, urllib.parse
try:
    import websocket
    websocket.setdefaulttimeout(8)
except ImportError:
    websocket = None


def wanted_objects(objects):
    import re
    base = {'print_stats','heater_bed','heaters','toolhead','virtual_sdcard','display_status','fan','gcode_move','webhooks','multi_color_controller','box_extras','led cavity_led'}
    return {x: None for x in objects if x in base or re.fullmatch(r'extruder\d*',x) or x.startswith(('box_stepper ','aht20_f ','temperature_sensor ','temperature_fan ','heater_generic ','fan_generic ','heater_fan ','controller_fan ','output_pin '))}


def merge_status(status, delta):
    for key, fields in delta.items():
        if isinstance(fields, dict):
            status.setdefault(key, {}).update(copy.deepcopy(fields))
        else:
            status[key] = copy.deepcopy(fields)


class PrinterFeed:
    def __init__(self, printer, api, configuration, invalidate):
        self.printer = dict(printer)
        self.api, self.configuration, self.invalidate = api, configuration, invalidate
        self.lock = threading.RLock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.status, self.config = {}, {}
        self.error, self.mode, self.version = 'Connecting to printer', 'connecting', 0
        self.socket = None
        threading.Thread(target=self.run, daemon=True, name='moonraker-'+printer['id']).start()

    def publish(self, status=None, error=None, mode=None, delta=False):
        with self.lock:
            if status is not None:
                if delta: merge_status(self.status, status)
                else: self.status = copy.deepcopy(status)
            self.error = error
            if mode: self.mode = mode
            self.version += 1
            self.ready.set()

    def snapshot(self):
        with self.lock:
            return self.version, copy.deepcopy(self.status), self.config, self.error, self.mode

    def close(self):
        self.stop.set()
        if self.socket: self.socket.close()

    def run(self):
        failures = 0
        while not self.stop.is_set():
            try:
                self.invalidate()
                objects = self.api('/printer/objects/list')['objects']
                self.config = self.configuration()
                wanted = wanted_objects(objects)
                if websocket is None: raise RuntimeError('WebSocket dependency unavailable')
                url = self.printer['url'].replace('http://','ws://',1).replace('https://','wss://',1)+'/websocket'
                headers = ['X-Api-Key: '+self.printer['api_key']] if self.printer.get('api_key') else []
                def opened(ws):
                    ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'printer.objects.subscribe','params':{'objects':wanted}}))
                def message(ws, raw):
                    value = json.loads(raw)
                    if value.get('id') == 1:
                        if 'error' in value:
                            self.publish(error='Moonraker subscription rejected',mode='offline')
                            ws.close()
                            return
                        self.publish(value['result']['status'], mode='websocket')
                    elif value.get('method') == 'notify_status_update':
                        self.publish(value['params'][0], mode='websocket', delta=True)
                    elif value.get('method') in ('notify_klippy_ready','notify_klippy_disconnected','notify_klippy_shutdown'):
                        self.publish(error='Klipper is reconnecting',mode='connecting')
                        ws.close()
                    elif value.get('method') == 'notify_filelist_changed':
                        self.invalidate(files=True)
                self.socket = websocket.WebSocketApp(url,header=headers,on_open=opened,on_message=message)
                self.socket.run_forever(ping_interval=20,ping_timeout=8,suppress_origin=True)
                if self.stop.is_set(): return
                raise RuntimeError('Printer live connection disconnected')
            except Exception as error:
                self.publish(error=str(error),mode='offline')
                try:
                    objects = self.api('/printer/objects/list')['objects']
                    self.config = self.configuration()
                    query = '&'.join(urllib.parse.quote(x) for x in wanted_objects(objects))
                    self.publish(self.api('/printer/objects/query?'+query)['status'],mode='polling')
                    failures = 0
                except Exception as fallback_error:
                    self.publish(error=str(fallback_error),mode='offline')
                    failures += 1
            self.stop.wait(min(30,5*max(1,failures)))
