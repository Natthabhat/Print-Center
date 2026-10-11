"""Private Klipper dashboard. Bind only to the Ubuntu server's Tailscale IP."""
import http.client, tempfile
import copy
from live_status import PrinterFeed
from camera_monitor import SnapmakerCamera
from box_control import parse_catalog, box_script, plate_filaments, mapping_script
import json, os, pathlib, secrets, urllib.request, urllib.parse, urllib.error, threading, ipaddress, time, posixpath, zipfile, io, re, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).parent
PRINTER = os.environ.get('PRINTER_HOST', '192.168.1.100')
BASE = 'http://' + PRINTER + ':7125'
BIND = os.environ.get('DASHBOARD_BIND', '127.0.0.1')
PORT = int(os.environ.get('DASHBOARD_PORT', '8787'))
TOKEN = secrets.token_urlsafe(32)
CONFIG = pathlib.Path(os.environ.get('DASHBOARD_DATA_DIR', str(ROOT))) / 'printers.json'
LOCK = threading.RLock()
CURRENT = threading.local()
CACHE = {}
IMAGE_CACHE = {}
HEATER_CONFIG = {}
FEEDS = {}
CAMERAS = {}
CAMERA_CONFIG = {}

def heater_config():
    p = CURRENT.printer
    key = (p['id'], p['url'])
    with LOCK:
        cached = HEATER_CONFIG.get(key)
    if cached and time.monotonic() - cached[0] < 86400:
        return cached[1]
    config = api('/printer/objects/query?configfile')['status']['configfile']
    settings = {k: dict(v) for k, v in config.get('config', {}).items()}
    for k, v in config.get('settings', {}).items():
        settings.setdefault(k, {}).update(v)
    with LOCK:
        HEATER_CONFIG[key] = (time.monotonic(), settings)
    return settings

def temperature_rows(status, config):
    available = status.get('heaters', {}).get('available_heaters', [])
    rows = []
    for key, value in status.items():
        if not isinstance(value, dict) or 'temperature' not in value:
            continue
        fan = key.startswith('temperature_fan ')
        editable = key in available or fan
        section = config.get(key, {})
        fallback = 280 if key.startswith('extruder') else 110 if key == 'heater_bed' else 70
        maximum = float(section.get('max_temp', fallback))
        minimum = max(0, float(section.get('min_temp', 0)))
        name = key.split(' ', 1)[-1].replace('_', ' ')
        rows.append({'id': key, 'name': name.capitalize(), 'actual': value['temperature'],
                     'target': value.get('target'), 'power': value.get('power', value.get('speed')),
                     'editable': editable, 'min': minimum, 'max': maximum,
                     'configured_limit': 'max_temp' in section})
    return sorted(rows, key=lambda row: (not row['editable'], row['name'].lower()))

def heat_script(heater, target, rows):
    row = next((r for r in rows if r['id'] == heater and r['editable']), None)
    if row is None:
        raise ValueError('Select a configured heater')
    if not math.isfinite(target) or not (target == 0 or row['min'] <= target <= row['max']):
        raise ValueError(f"Target must be 0 (off) or {row['min']:g}–{row['max']:g}°C")
    name = heater.split(' ', 1)[-1]
    if not re.fullmatch(r'[A-Za-z0-9_]+', name):
        raise ValueError('Unsupported heater name')
    command = 'SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN' if heater.startswith('temperature_fan ') else 'SET_HEATER_TEMPERATURE HEATER'
    return f'{command}={name} TARGET={target:g}'


def box_catalog():
    p = CURRENT.printer
    key = (p['id'], 'box_catalog')
    with LOCK:
        if key in CACHE: return CACHE[key]
    headers = {'X-Api-Key':p['api_key']} if p.get('api_key') else {}
    req = urllib.request.Request(p['url']+'/server/files/config/officiall_filas_list.cfg',headers=headers)
    with urllib.request.build_opener(NoRedirect).open(req,timeout=8) as response:
        value = parse_catalog(response.read(262145).decode('utf-8'))
    with LOCK: CACHE[key] = value
    return value

def control_status():
    objects = api('/printer/objects/list')['objects']
    wanted = [x for x in objects if x in ('print_stats','toolhead','gcode_move','webhooks','fan','multi_color_controller','box_extras','led cavity_led') or re.fullmatch(r'extruder\d*', x) or x.startswith(('box_stepper ','fan_generic ','heater_fan ','controller_fan ','output_pin ','temperature_fan '))]
    return api('/printer/objects/query?' + '&'.join(urllib.parse.quote(x) for x in wanted))['status']

def control_rows(status, config):
    rows = []
    for key, value in status.items():
        if key == 'led cavity_led' and 'white_pin' in config.get(key, {}):
            colors = value.get('color_data', [])
            white = colors[0][3] if colors and len(colors[0]) >= 4 else 0
            rows.append({'id':key,'name':'Caselight','value':white,'editable':True,'output':True,'binary':True,'scale':1})
        if key == 'fan' or key.startswith(('fan_generic ','heater_fan ','controller_fan ','temperature_fan ','output_pin ')):
            section = config.get(key, {})
            output = key.startswith('output_pin ')
            name = key.split(' ',1)[-1]
            # Firmware heater relays are never manual dashboard outputs.
            allowed_output = output and name in ('caselight','beeper','polar_cooler') and key in config and 'static_value' not in section
            editable = key == 'fan' or key.startswith('fan_generic ') or allowed_output
            rows.append({'id':key,'name':name.replace('_',' ').capitalize(),'value':value.get('value',value.get('speed',0)), 'editable':editable,'output':output,'binary':output and not section.get('pwm',False),'scale':float(section.get('scale',1))})
    return rows

def tool_script(data, status, config):
    action = data.get('action')
    tool = status.get('toolhead', {})
    move = status.get('gcode_move', {})
    if status.get('webhooks',{}).get('state','ready') != 'ready':
        raise ValueError('Klipper is not ready')
    active = status.get('print_stats',{}).get('state') in ('printing','paused')
    def num(key, low, high):
        value = float(data.get(key, float('nan')))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be between {low:g} and {high:g}')
        return value
    def axis():
        a = data.get('axis')
        if a not in ('X','Y','Z'): raise ValueError('Select X, Y or Z')
        return a
    if action in ('home','jog','extrude') and active:
        raise ValueError('Wait until the print is finished before moving manually')
    if action == 'home':
        a = data.get('axis')
        if a not in ('all','X','Y','Z'): raise ValueError('Invalid homing axis')
        return 'G28' + ('' if a == 'all' else ' ' + a)
    if action in ('jog','zoffset'):
        a = axis() if action == 'jog' else 'Z'
        if a.lower() not in tool.get('homed_axes',''): raise ValueError('Home '+a+' first')
        amount = num('amount', -100 if action == 'jog' else -.05, 100 if action == 'jog' else .05)
        i = 'XYZ'.index(a)
        destination = tool['position'][i] + amount
        if not tool['axis_minimum'][i] <= destination <= tool['axis_maximum'][i]:
            raise ValueError('Move exceeds printer travel limits')
        if action == 'zoffset':
            if abs(move.get('homing_origin',[0,0,0])[2]+amount)>5: raise ValueError('Z offset exceeds dashboard limit')
            return f'SET_GCODE_OFFSET Z_ADJUST={amount:g} MOVE=1 MOVE_SPEED=5'
        feed = min(5 if a == 'Z' else 50, float(tool.get('max_velocity',50))) * 60 / max(.01,float(move.get('speed_factor',1)))
        return f'SAVE_GCODE_STATE NAME=dashboard_move\nG91\nG1 {a}{amount:g} F{feed:g}\nRESTORE_GCODE_STATE NAME=dashboard_move'
    extruder = tool.get('extruder','extruder')
    if not re.fullmatch(r'extruder\d*',extruder) or extruder not in status: raise ValueError('No active extruder')
    if action == 'extrude':
        section = config.get(extruder,{})
        if status[extruder].get('temperature',0) < float(section.get('min_extrude_temp',170)):
            raise ValueError('Heat the active extruder before extruding or retracting')
        length = num('length',-min(100,float(section.get('max_extrude_only_distance',50))), min(100,float(section.get('max_extrude_only_distance',50))))
        speed = num('speed',.1,min(10,float(section.get('max_extrude_only_velocity',5))))
        if status[extruder].get('can_extrude') is False: raise ValueError('Extrusion is not available')
        factor = max(.01,float(move.get('extrude_factor',1)))
        feed = speed*60 / max(.01,float(move.get('speed_factor',1))) / factor
        return f'SAVE_GCODE_STATE NAME=dashboard_extrude\nM83\nG1 E{length/factor:g} F{feed:g}\nRESTORE_GCODE_STATE NAME=dashboard_extrude'
    if action == 'tune':
        parameter = data.get('parameter')
        if parameter in ('speed','flow'):
            value = num('value',10,200)
            return f"{'M220' if parameter == 'speed' else 'M221'} S{value:g}"
        if parameter in ('pressure_advance','smooth_time'):
            if parameter not in status[extruder] and not (parameter == 'smooth_time' and 'smooth_time' in status[extruder]): raise ValueError('Tuning is not supported by this extruder')
            value = num('value',0 if parameter == 'pressure_advance' else .001,1 if parameter == 'pressure_advance' else .2)
            return f"SET_PRESSURE_ADVANCE EXTRUDER={extruder} {'ADVANCE' if parameter == 'pressure_advance' else 'SMOOTH_TIME'}={value:g}"
        raise ValueError('Unknown tuning parameter')
    if action == 'output':
        key = data.get('id')
        row = next((r for r in control_rows(status,config) if r['id']==key and r['editable']),None)
        if not row: raise ValueError('Select an available fan or user output')
        name = key.split(' ',1)[-1]
        if not re.fullmatch(r'[A-Za-z0-9_]+',name): raise ValueError('Unsupported output name')
        value = num('value',0,1)
        if row['binary'] and value not in (0,1): raise ValueError('Output must be on or off')
        if key == 'led cavity_led': return f'SET_LED LED=cavity_led WHITE={value:g} SYNC=0 TRANSMIT=1'
        if key == 'fan': return f'M106 S{value*255:g}'
        if row['output']: return f"SET_PIN PIN={name} VALUE={value*row['scale']:g}"
        return f'SET_FAN_SPEED FAN={name} SPEED={value:g}'
    raise ValueError('Unknown tool control')

def printers():
    with LOCK:
        if CONFIG.exists():
            return json.loads(CONFIG.read_text())
        return [{'id': 'qidi', 'name': 'Qidi', 'url': BASE, 'camera_base': 'http://' + PRINTER, 'api_key': ''}]

def public_printer(p):
    return {k: p.get(k) for k in ('id', 'name', 'url', 'camera_base')} | {'has_api_key': bool(p.get('api_key'))}

def save_printers(values):
    temp = CONFIG.with_suffix('.tmp')
    with LOCK:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(values, f)
        os.replace(temp, CONFIG)
        valid_ids = {p['id'] for p in values}
        for key in list(FEEDS):
            if key[0] not in valid_ids: FEEDS.pop(key).close()

def valid_url(value, default_port=None):
    value = str(value).strip()
    if '://' not in value:
        value = 'http://' + value
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ('http', 'https') or p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
        raise ValueError('Enter an HTTP(S) printer IP address and port, without a path')
    try:
        ip = ipaddress.ip_address(p.hostname)
    except ValueError:
        raise ValueError('Use the printer’s LAN or Tailscale IP address')
    allowed = [ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10','fc00::/7')]
    if not any(ip in n for n in allowed):
        raise ValueError('Use a private LAN or Tailscale printer address')
    host = '[' + str(ip) + ']' if ip.version == 6 else str(ip)
    port = p.port or default_port
    return p.scheme + '://' + host + (':' + str(port) if port else '')

def selected(pid):
    for p in printers():
        if p['id'] == pid:
            return p
    raise ValueError('Select a saved printer')

def metadata(filename):
    key = (CURRENT.printer['id'], filename)
    with LOCK:
        item = CACHE.get(key)
    if item and time.monotonic() - item[0] < 3600:
        return item[1]
    value = api('/server/files/metadata?filename=' + urllib.parse.quote(filename))
    with LOCK:
        CACHE[key] = (time.monotonic(), value)
    return value

def thumbnail(filename, plate=''):
    if not isinstance(filename, str) or filename.startswith('/') or '..' in pathlib.PurePosixPath(filename).parts or '\\' in filename:
        raise ValueError('Invalid print filename')
    p = CURRENT.printer
    if plate and (not str(plate).isdigit() or not 1 <= int(plate) <= 1000):
        raise ValueError('Invalid plate number')
    key = (p['id'], filename, str(plate))
    with LOCK:
        cached = IMAGE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1:]
    headers = {'X-Api-Key': p['api_key']} if p.get('api_key') else {}
    thumbs = metadata(filename).get('thumbnails', [])
    if not thumbs:
        raise ValueError('No model preview embedded in this file')
    thumb = max(thumbs, key=lambda t: t.get('width', 0) * t.get('height', 0))
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(filename), thumb['relative_path']))
    if relative.startswith(('/', '../')) or '\\' in relative:
        raise ValueError('Invalid thumbnail path')
    try:
        req = urllib.request.Request(p['url'] + '/server/files/gcodes/' + urllib.parse.quote(relative), headers=headers)
        with OPENER.open(req, timeout=8) as r:
            image, kind = r.read(5000001), r.headers.get('Content-Type','').split(';')[0]
    except urllib.error.HTTPError as error:
        if error.code != 404 or not filename.lower().endswith('.gcode.3mf'):
            raise
        req = urllib.request.Request(p['url'] + '/server/files/gcodes/' + urllib.parse.quote(filename), headers=headers)
        with OPENER.open(req, timeout=12) as r:
            archive = r.read(30000001)
        if len(archive) > 30000000:
            raise ValueError('Model archive is too large for preview')
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            images = [item for item in z.infolist() if item.filename.lower().endswith(('.png','.jpg','.jpeg')) and item.file_size <= 5000000]
            match = re.search(r'_plate_(\d+)(?=\.gcode)', filename, re.IGNORECASE)
            plate_number = str(plate) if plate else match[1] if match else ''
            wanted = 'plate_' + plate_number + '.png' if plate_number else posixpath.basename(thumb['relative_path'])
            item = next((item for item in images if posixpath.basename(item.filename) == wanted), None)
            if item is None:
                item = next((item for item in images if 'plate' in item.filename.lower() and 'small' not in item.filename.lower()), None)
            if item is None:
                raise ValueError('No model preview embedded in this archive')
            image = z.read(item)
            kind = 'image/png' if item.filename.lower().endswith('.png') else 'image/jpeg'
    if len(image) > 5000000 or kind not in ('image/png','image/jpeg','image/webp'):
        raise ValueError('Unsupported model preview')
    with LOCK:
        while IMAGE_CACHE and (len(IMAGE_CACHE) >= 100 or sum(len(v[1]) for v in IMAGE_CACHE.values()) + len(image) > 32000000):
            IMAGE_CACHE.pop(next(iter(IMAGE_CACHE)))
        IMAGE_CACHE[key] = (time.monotonic(), image, kind)
    return image, kind

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPENER = urllib.request.build_opener(NoRedirect)
def api(path, data=None):
    body = None if data is None else json.dumps(data).encode()
    p = CURRENT.printer
    headers = {'Content-Type': 'application/json'}
    if p.get('api_key'):
        headers['X-Api-Key'] = p['api_key']
    req = urllib.request.Request(p['url'] + path, body, headers)
    with OPENER.open(req, timeout=8) as response:
        value = json.load(response)
    if 'error' in value:
        raise ValueError(str(value['error']))
    return value.get('result', value)

def camera():
    p = CURRENT.printer
    key = (p['id'], p['url'], p.get('api_key', ''))
    with LOCK:
        cached = CAMERA_CONFIG.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    cams = api('/server/webcams/list').get('webcams', [])
    cam = next((c for c in cams if c.get('enabled', True)), None)
    if not cam:
        info = api('/server/info')
        if 'snapmakercloud' in info.get('components', []) and 'camera' in info.get('registered_directories', []):
            cam = {'name': 'Snapmaker camera', 'service': 'snapmaker-lan', 'rotation': 0}
    with LOCK:
        CAMERA_CONFIG[key] = (time.monotonic(), cam)
    return cam

def snapmaker_camera():
    p = CURRENT.printer
    key = (p['id'], p['url'], p.get('api_key', ''))
    with LOCK:
        if key not in CAMERAS:
            CAMERAS[key] = SnapmakerCamera(p)
        return CAMERAS[key]

def camera_url(value):
    p = CURRENT.printer
    url = urllib.parse.urljoin(p['camera_base'] + '/', value)
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != urllib.parse.urlsplit(p['url']).hostname or parsed.scheme not in ('http', 'https') or parsed.username:
        raise ValueError('Camera URL must use the configured printer address')
    return url


def feed_for(printer):
    key = (printer['id'],printer['url'],printer.get('api_key',''))
    with LOCK:
        for old in list(FEEDS):
            if old[0] == printer['id'] and old != key:
                FEEDS.pop(old).close()
        if key not in FEEDS:
            def read(path):
                CURRENT.printer = printer
                return api(path)
            def config():
                CURRENT.printer = printer
                return heater_config()
            def invalidate(files=False):
                with LOCK:
                    if not files: HEATER_CONFIG.pop((printer['id'],printer['url']),None)
                    else:
                        for cache in (CACHE,IMAGE_CACHE):
                            for item in list(cache):
                                if item[0] == printer['id']: cache.pop(item,None)
            FEEDS[key] = PrinterFeed(printer,read,config,invalidate)
        return FEEDS[key]

def feed_payload(printer, wait=False):
    feed = feed_for(printer)
    if wait: feed.ready.wait(4)
    version,status,config,error,mode = feed.snapshot()
    if error: return version, {'error':error,'offline':True,'printer_id':printer['id']}
    return version, {'status':status,'temperatures':temperature_rows(status,config),
                     'controls':control_rows(status,config),'printer':public_printer(printer),
                     'connection_mode':mode}



def plate_numbers(archive):
    plates = set()
    for item in archive.infolist():
        match = re.fullmatch(r'Metadata/plate_(\d+)\.gcode',item.filename,re.IGNORECASE)
        if match and 1 <= int(match[1]) <= 1000: plates.add(int(match[1]))
    if not plates: raise ValueError('No sliced plate G-code found in this 3MF file')
    return sorted(plates)

def choose_plate(filename, plates):
    if len(plates) == 1: return plates[0]
    match = re.search(r'_plate_(\d+)(?=\.gcode\.3mf$)',filename,re.IGNORECASE)
    if match and int(match[1]) in plates: return int(match[1])
    raise ValueError('This archive contains multiple plates. Export the selected plate as a separate print file.')

def qidi_start_script(filename, plate):
    if not isinstance(filename,str) or any(c in filename for c in ('"','\\','\n','\r')) or filename.startswith('/') or '..' in pathlib.PurePosixPath(filename).parts:
        raise ValueError('Unsupported print filename')
    if type(plate) is not int or not 1 <= plate <= 1000: raise ValueError('Invalid print plate')
    return f'SDCARD_PRINT_FILE FILENAME="{filename}" PLATEINDEX="{plate}" UID="native"'

def archive_print_plan(filename):
    p = CURRENT.printer
    headers = {'X-Api-Key':p['api_key']} if p.get('api_key') else {}
    request = urllib.request.Request(p['url']+'/server/files/gcodes/'+urllib.parse.quote(filename),headers=headers)
    with tempfile.TemporaryFile() as source:
        total = 0
        with OPENER.open(request,timeout=20) as response:
            while chunk := response.read(65536):
                total += len(chunk)
                if total > UPLOAD_LIMIT: raise ValueError('3MF exceeds supported archive size')
                source.write(chunk)
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            plate = choose_plate(filename,plate_numbers(archive))
            with archive.open('Metadata/slice_info.config') as info:
                xml = info.read(2*1024*1024+1)
            if len(xml)>2*1024*1024: raise ValueError('Plate metadata is too large')
            return {'plate':plate,'filaments':plate_filaments(xml,plate)}

def archive_print_plate(filename):
    return archive_print_plan(filename)['plate']

def print_plan(filename):
    files = api('/server/files/list?root=gcodes')
    item = next((f for f in files if f['path']==filename),None)
    if not item: raise ValueError('Select an existing print file')
    key = (CURRENT.printer['id'],'print_plan:'+filename)
    with LOCK: cached = CACHE.get(key)
    if cached and cached[1].get('modified')==item.get('modified'): return cached[1]
    if not filename.lower().endswith('.gcode.3mf'): raise ValueError('For box color selection, export a sliced G-code 3MF from Qidi Studio')
    value = archive_print_plan(filename)
    value['modified']=item.get('modified')
    with LOCK: CACHE[key]=(time.monotonic(),value)
    return value

UPLOAD_LIMIT = 200 * 1024 * 1024

def upload_filename(value):
    if not isinstance(value,str) or not value or len(value.encode('utf-8'))>200 or any(ord(c)<32 for c in value) or any(c in value for c in ('/','\\','"')) or value.startswith('.'):
        raise ValueError('Invalid upload filename')
    if not value.lower().endswith(('.gcode','.gco','.g','.gcode.3mf')):
        raise ValueError('Upload a sliced G-code or .gcode.3mf print file')
    return value

def upload_print_file(source, length, filename):
    filename = upload_filename(filename)
    available = api('/server/files/list?root=gcodes')
    if filename in {f['path'] for f in available}:
        suffix = '.gcode.3mf' if filename.lower().endswith('.gcode.3mf') else pathlib.PurePosixPath(filename).suffix
        filename = filename[:-len(suffix)]+'_'+secrets.token_hex(4)+suffix
    if filename.lower().endswith('.gcode.3mf'):
        try:
            with zipfile.ZipFile(source) as archive:
                if not any(item.filename.lower().endswith('.gcode') for item in archive.infolist()):
                    raise ValueError('The 3MF must contain sliced G-code')
        except zipfile.BadZipFile:
            raise ValueError('Invalid sliced 3MF archive')
        source.seek(0)
    boundary = 'dashboard-'+secrets.token_hex(16)
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\ngcodes\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="print"\r\n\r\nfalse\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    tail = f'\r\n--{boundary}--\r\n'.encode()
    url = urllib.parse.urlsplit(CURRENT.printer['url'])
    cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    connection = cls(url.hostname,url.port,timeout=120)
    try:
        connection.putrequest('POST','/server/files/upload')
        connection.putheader('Content-Type','multipart/form-data; boundary='+boundary)
        connection.putheader('Content-Length',str(len(prefix)+length+len(tail)))
        if CURRENT.printer.get('api_key'): connection.putheader('X-Api-Key',CURRENT.printer['api_key'])
        connection.endheaders()
        connection.send(prefix)
        while chunk := source.read(65536): connection.send(chunk)
        connection.send(tail)
        response = connection.getresponse()
        value = json.loads(response.read(1048576))
        if response.status >= 400 or 'error' in value:
            raise ValueError('Printer rejected upload: '+str(value.get('error',response.status)))
        return {'filename':filename,'result':value.get('result',value)}
    finally:
        connection.close()

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def reply(self, value, status=200, kind='application/json'):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(raw)
    def events(self, pid):
        values = printers() if pid == 'all' else [selected(pid)]
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Accel-Buffering','no')
        self.send_header('Connection','close')
        self.end_headers()
        self.close_connection = True
        versions = {}
        last_sent = {}
        heartbeat = 0
        try:
            while True:
                for printer in values:
                    version,data = feed_payload(printer)
                    if versions.get(printer['id']) != version or time.monotonic()-last_sent.get(printer['id'],0)>=10:
                        raw = json.dumps({'printer':printer['id'],'data':data})
                        self.wfile.write(('data: '+raw+'\n\n').encode())
                        versions[printer['id']] = version
                        last_sent[printer['id']] = time.monotonic()
                if time.monotonic()-heartbeat>15:
                    self.wfile.write(b': heartbeat\n\n')
                    heartbeat = time.monotonic()
                self.wfile.flush()
                time.sleep(.5)
        except (BrokenPipeError,ConnectionResetError):
            pass

    def host_ok(self):
        return self.headers.get('Host') in {f'{BIND}:{PORT}', f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
    def do_GET(self):
        if not self.host_ok():
            return self.reply({'error': 'Invalid host'}, 403)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == '/api/events':
                return self.events(query.get('printer',['all'])[0])
            if path == '/api/printers':
                return self.reply({'printers': [public_printer(p) for p in printers()]})
            if path.startswith(('/api/', '/camera/')):
                CURRENT.printer = selected(query.get('printer', ['qidi'])[0])
            if path == '/':
                self.reply((ROOT/'index.html').read_bytes().replace(b'__CONTROL_TOKEN__', TOKEN.encode()), kind='text/html; charset=utf-8')
            elif path == '/app.js':
                self.reply((ROOT/'app.js').read_bytes(), kind='text/javascript; charset=utf-8')
            elif path == '/api/history':
                start = int(query.get('start',['0'])[0])
                order = query.get('order',['desc'])[0]
                if not 0 <= start <= 1000000 or order not in ('asc','desc'): raise ValueError('Invalid history page')
                result = api(f'/server/history/list?limit=51&start={start}&order={order}')
                jobs = result.get('jobs',[])
                fields = ('job_id','filename','status','start_time','end_time','print_duration','total_duration','filament_used','exists')
                rows = [{**{k:j.get(k) for k in fields},'metadata':{k:j.get('metadata',{}).get(k) for k in ('filament_weight_total','filament_total')}} for j in jobs[:50]]
                self.reply({'jobs':rows,'has_more':len(jobs)>50})
            elif path == '/api/console-log':
                self.reply(api('/server/gcode_store?count=100'))
            elif path == '/api/printer-info':
                self.reply({'printer':public_printer(CURRENT.printer),'totals':api('/server/history/totals').get('job_totals',{})})
            elif path == '/api/print-plan':
                self.reply(print_plan(query.get('filename',[''])[0]))
            elif path == '/api/box/catalog':
                self.reply(box_catalog())
            elif path == '/api/status':
                _, value = feed_payload(CURRENT.printer, wait=True)
                self.reply(value, 502 if 'error' in value else 200)
            elif path == '/api/files':
                self.reply(api('/server/files/list?root=gcodes'))
            elif path == '/api/camera':
                cam = camera()
                if cam:
                    cam = {k: cam.get(k) for k in ['name','service','rotation','flip_horizontal','flip_vertical']}
                self.reply({'camera': cam})
            elif path == '/api/metadata':
                self.reply(metadata(query.get('filename', [''])[0]))
            elif path == '/api/thumbnail':
                filename = query.get('filename', [''])[0]
                image, kind = thumbnail(filename, query.get('plate', [''])[0])
                self.reply(image, kind=kind)
            elif path in ('/camera/stream', '/camera/snapshot'):
                cam = camera()
                if not cam:
                    return self.reply({'error': 'No camera configured'}, 404)
                if cam.get('service') == 'snapmaker-lan':
                    monitor = snapmaker_camera()
                    frame = monitor.snapshot(OPENER)
                    if path.endswith('snapshot'):
                        return self.reply(frame, kind='image/jpeg')
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    while True:
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')
                        self.wfile.flush()
                        time.sleep(2)
                        try:
                            frame = monitor.snapshot(OPENER)
                        except Exception:
                            return
                url = camera_url(cam['stream_url' if path.endswith('stream') else 'snapshot_url'])
                with OPENER.open(url, timeout=12) as response:
                    self.send_response(200)
                    self.send_header('Content-Type', response.headers.get('Content-Type', 'image/jpeg'))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    while chunk := response.read1(16384):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            else:
                self.reply({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.reply({'error': str(error), 'offline': True}, 502)
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ('/api/control', '/api/printers', '/api/upload'):
            return self.reply({'error': 'Not found'}, 404)
        if not self.host_ok() or self.headers.get('X-Control-Token') != TOKEN:
            return self.reply({'error': 'Refresh this dashboard before using controls'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host',''):
            return self.reply({'error': 'Invalid origin'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if path == '/api/upload':
                self.close_connection = True
                if not 0 < length <= UPLOAD_LIMIT: raise ValueError('Upload limit is 200 MB per file')
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                CURRENT.printer = selected(query.get('printer',[''])[0])
                filename = upload_filename(query.get('filename',[''])[0])
                self.connection.settimeout(120)
                with tempfile.TemporaryFile() as source:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536,remaining))
                        if not chunk: raise ValueError('Incomplete upload')
                        source.write(chunk)
                        remaining -= len(chunk)
                    source.seek(0)
                    value = upload_print_file(source,length,filename)
                return self.reply({'ok':True,**value})
            if not 0 < length <= 8192:
                return self.reply({'error': 'Invalid request size'}, 400)
            data = json.loads(self.rfile.read(length))
            if path == '/api/printers':
                with LOCK:
                    values = printers()
                    action = data.get('action')
                    if action == 'remove':
                        if len(values) < 2:
                            raise ValueError('Keep at least one printer')
                        values = [p for p in values if p['id'] != data.get('id')]
                    elif action in ('add', 'edit'):
                        name = str(data.get('name', '')).strip()
                        if not 1 <= len(name) <= 60:
                            raise ValueError('Enter a printer name (up to 60 characters)')
                        url = valid_url(data.get('url', ''), 7125)
                        camera_base = valid_url(data.get('camera_base') or urllib.parse.urlsplit(url).scheme + '://' + urllib.parse.urlsplit(url).hostname)
                        if urllib.parse.urlsplit(camera_base).hostname != urllib.parse.urlsplit(url).hostname:
                            raise ValueError('Camera and Moonraker must use the same printer address')
                        existing = selected(data['id']) if action == 'edit' else {}
                        key = str(data.get('api_key', '')).strip() or existing.get('api_key', '')
                        if len(key) > 512 or '\n' in key or '\r' in key:
                            raise ValueError('Invalid API key')
                        record = {'id': existing.get('id', secrets.token_hex(8)), 'name': name, 'url': url, 'camera_base': camera_base, 'api_key': key}
                        if any(p['url'] == url and p['id'] != record['id'] for p in values):
                            raise ValueError('That printer address is already saved')
                        if action == 'add':
                            if len(values) >= 30:
                                raise ValueError('Up to 30 printers can be saved')
                            values.append(record)
                        else:
                            values = [record if p['id'] == record['id'] else p for p in values]
                    else:
                        raise ValueError('Unknown printer management action')
                    save_printers(values)
                return self.reply({'printers': [public_printer(p) for p in values]})
            CURRENT.printer = selected(data.get('printer'))
            action = data.get('action')
            if action in ('pause','resume','cancel','emergency_stop'):
                endpoint = '/printer/emergency_stop' if action == 'emergency_stop' else '/printer/print/' + action
                result = api(endpoint, {})
            elif action == 'start':
                filename = data.get('filename')
                available = api('/server/files/list?root=gcodes')
                if filename not in [f['path'] for f in available] or not filename.lower().endswith(('.gcode', '.gco', '.g', '.gcode.3mf')):
                    raise ValueError('Select an existing G-code file')
                print_status = api('/printer/objects/query?print_stats')['status']['print_stats']
                state = print_status['state']
                if state in ('printing','paused'):
                    raise ValueError('A print job is already active')
                if filename.lower().endswith('.gcode.3mf'):
                    if 'plateindex' not in print_status:
                        raise ValueError('This printer needs a plain G-code file. Export G-code from your slicer.')
                    status = control_status()
                    if status.get('multi_color_controller',{}).get('hardware',{}).get('connected'):
                        plan = print_plan(filename)
                        script = mapping_script(data,status,plan['filaments'])+'\n'+qidi_start_script(filename,plan['plate'])
                    else:
                        script = qidi_start_script(filename,archive_print_plate(filename))
                    result = api('/printer/gcode/script', {'script':script})
                else:
                    result = api('/printer/print/start', {'filename': filename})
            elif action == 'box':
                script = box_script(data, control_status(), box_catalog())
                result = api('/printer/gcode/script', {'script':script})
            elif action in ('home','jog','extrude','zoffset','tune','output'):
                script = tool_script(data, control_status(), heater_config())
                result = api('/printer/gcode/script', {'script': script})
            elif action == 'heat':
                heater = data.get('heater')
                target = float(data.get('target', -1))
                objects = api('/printer/objects/list')['objects']
                wanted = [x for x in objects if x == 'heaters' or re.fullmatch(r'extruder\d*', x) or x.startswith(('heater_generic ', 'temperature_fan ')) or x == 'heater_bed']
                status = api('/printer/objects/query?' + '&'.join(urllib.parse.quote(x) for x in wanted))['status']
                script = heat_script(heater, target, temperature_rows(status, heater_config()))
                result = api('/printer/gcode/script', {'script': script})
            else:
                return self.reply({'error': 'Unknown control'}, 400)
            self.reply({'ok': True, 'result': result})
        except (ValueError, TypeError) as error:
            self.reply({'error': str(error)}, 400)
        except Exception as error:
            self.reply({'error': str(error)}, 502)

if __name__ == '__main__':
    print(f'Print Center: http://{BIND}:{PORT}', flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
