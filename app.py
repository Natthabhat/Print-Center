"""Private Klipper dashboard on explicitly configured server addresses."""
import http.client, tempfile
import copy
import hmac
from web_auth import WebAuth
from upload_jobs import UploadJobs, UploadBusy
import u1_filament
from plate_detection import PlateDetection
from model_library import ModelLibrary
from completion_status import CompletionStatus
from frame_cache import FrameCache
from printer_photos import photo_url, store_photo, PHOTO_NAME, UPLOAD_LIMIT as PHOTO_UPLOAD_LIMIT
from timelapse import Timelapses
from live_status import PrinterFeed
from bambu import BambuFeed, validate_credentials
from bambu_camera import BambuCamera
from bambu_files import BambuFiles
from camera_monitor import SnapmakerCamera
from box_control import parse_catalog, box_script, plate_filaments, mapping_script
import json, os, pathlib, secrets, urllib.request, urllib.parse, urllib.error, threading, ipaddress, time, posixpath, zipfile, io, re, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).parent
PRINTER = os.environ.get('PRINTER_HOST', '192.168.1.100')
BASE = 'http://' + PRINTER + ':7125'
BIND = os.environ.get('DASHBOARD_BIND', '127.0.0.1')
EXTRA_BIND = os.environ.get('DASHBOARD_EXTRA_BIND', '').strip()
PUBLIC_HOST = os.environ.get('DASHBOARD_PUBLIC_HOST', '').strip().lower()
AUTH_USER = os.environ.get('DASHBOARD_AUTH_USER', '')
AUTH_HASH = os.environ.get('DASHBOARD_AUTH_HASH', '')
LOGIN_TOKEN = secrets.token_urlsafe(32)
PORT = int(os.environ.get('DASHBOARD_PORT', '8787'))
TOKEN = secrets.token_urlsafe(32)
CONFIG = pathlib.Path(os.environ.get('DASHBOARD_DATA_DIR', str(ROOT))) / 'printers.json'
LOGIN = WebAuth(CONFIG.parent/'web-sessions.json', AUTH_USER+'\0'+AUTH_HASH)
TIMELAPSES = Timelapses(CONFIG.parent)
PLATES = PlateDetection(CONFIG.parent)
LIBRARY = ModelLibrary(CONFIG.parent)
COMPLETION = CompletionStatus(CONFIG.parent/'completion-status.json')
LOCK = threading.RLock()
CURRENT = threading.local()
CACHE = {}
IMAGE_CACHE = {}
HEATER_CONFIG = {}
FEEDS = {}
CAMERAS = {}
BAMBU_FILES = {}
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
    return {k: p.get(k) for k in ('id', 'name', 'url', 'camera_base')} | {'has_api_key': bool(p.get('api_key')), 'has_access_code':bool(p.get('access_code')), 'serial':p.get('serial',''), 'timelapse_mode':p.get('timelapse_mode','interval'), 'profile':printer_profile(p), 'photo_url':photo_url(p), 'plate_ai_enabled':PLATES.config(p['id'])['enabled'] if p.get('id') else False}

def printer_profile(p):
    profile = p.get('profile')
    if profile in ('qidi-q2','snapmaker-u1','bambu-x2d','generic'): return profile
    name = p.get('name','').lower()
    return 'snapmaker-u1' if 'u1' in name else 'qidi-q2' if 'q2' in name else 'generic'

def validate_printer_profile(profile):
    if profile not in ('qidi-q2','snapmaker-u1','bambu-x2d','generic'): raise ValueError('Choose a supported printer profile')
    return profile

def save_printer_settings(pid, mode):
    if mode not in ('interval','layer'): raise ValueError('Choose a supported timelapse capture mode')
    with LOCK:
        values = printers()
        printer = next((p for p in values if p['id']==pid),None)
        if printer is None: raise ValueError('Select a saved printer')
        printer['timelapse_mode'] = mode
        save_printers(values)
        return public_printer(printer)

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
        for key in list(BAMBU_FILES):
            if key[0] not in valid_ids:BAMBU_FILES.pop(key)

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

FILE_LISTS={}
FILE_LIST_LOCKS={}
def printer_files(refresh=False):
    printer=CURRENT.printer
    key=(printer['id'],printer['url'],printer.get('api_key',''))
    with LOCK:
        guard=FILE_LIST_LOCKS.setdefault(key,threading.Lock())
        cached=FILE_LISTS.get(key)
    if not refresh and cached and time.monotonic()-cached[0]<30:return cached[1]
    with guard:
        with LOCK:cached=FILE_LISTS.get(key)
        if not refresh and cached and time.monotonic()-cached[0]<30:return cached[1]
        value=api('/server/files/list?root=gcodes', timeout=FILE_LIST_TIMEOUT)
        with LOCK:FILE_LISTS[key]=(time.monotonic(),value)
        return value

def warm_file_lists():
    # Separate workers keep an offline printer from delaying healthy printers.
    workers={}
    def warm(printer):
        try:
            CURRENT.printer=printer
            if printer_profile(printer)!='bambu-x2d':printer_files()
        except Exception:pass
    while True:
        for printer in printers():
            old=workers.get(printer['id'])
            if old is None or not old.is_alive():
                worker=threading.Thread(target=warm,args=(dict(printer),),daemon=True)
                workers[printer['id']]=worker;worker.start()
        time.sleep(20)

def metadata(filename):
    if printer_profile(CURRENT.printer)=='bambu-x2d':return bambu_files().archive_info(filename)[0]
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
    if printer_profile(CURRENT.printer)=='bambu-x2d':
        image=bambu_files().archive_info(filename,plate=plate or None)[1]
        if not image:raise ValueError('No thumbnail embedded in this printer file')
        return image,'image/png'
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
# Printers with many files can take 10 s or more to list them.
FILE_LIST_TIMEOUT = 60

def api(path, data=None, timeout=8, method=None):
    body = None if data is None else json.dumps(data).encode()
    p = CURRENT.printer
    headers = {'Content-Type': 'application/json'}
    if p.get('api_key'):
        headers['X-Api-Key'] = p['api_key']
    req = urllib.request.Request(p['url'] + path, body, headers, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        value = json.load(response)
    if 'error' in value:
        raise ValueError(str(value['error']))
    return value.get('result', value)

def existing_printer_file(filename):
    if not isinstance(filename,str) or not filename or any(c in filename for c in ('\r','\n','\\','\0')) or filename.startswith('/') or '..' in filename.split('/'):
        raise ValueError('Invalid printer filename')
    if filename not in [f['path'] for f in api('/server/files/list?root=gcodes', timeout=FILE_LIST_TIMEOUT)]:raise ValueError('Select an existing printer file')
    return filename


def camera():
    p = CURRENT.printer
    if printer_profile(p) == 'bambu-x2d':
        return {'name':'Bambu LAN camera','service':'bambu-lan','rotation':0}
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

def bambu_camera():
    p = CURRENT.printer
    key = (p['id'],p['url'],p.get('access_code',''),p.get('serial',''))
    with LOCK:
        for old in list(CAMERAS):
            if old[0] == p['id'] and old != key and hasattr(CAMERAS[old],'close'):
                CAMERAS.pop(old).close()
        if key not in CAMERAS:
            record=dict(p)
            feed=feed_for(p)
            if not record.get('serial') and feed.client:
                record['serial']=getattr(feed.client,'printer_serial','')
            CAMERAS[key] = BambuCamera(record)
        return CAMERAS[key]

def bambu_files():
    p=CURRENT.printer
    key=(p['id'],p['url'],p.get('access_code',''),p.get('serial',''))
    with LOCK:
        for old in list(BAMBU_FILES):
            if old[0]==p['id'] and old!=key:BAMBU_FILES.pop(old)
        if key not in BAMBU_FILES:BAMBU_FILES[key]=BambuFiles(p,CONFIG.parent/'bambu-cache')
        return BAMBU_FILES[key]

def camera_url(value):
    p = CURRENT.printer
    url = urllib.parse.urljoin(p['camera_base'] + '/', value)
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != urllib.parse.urlsplit(p['url']).hostname or parsed.scheme not in ('http', 'https') or parsed.username:
        raise ValueError('Camera URL must use the configured printer address')
    return url


def feed_for(printer):
    key = (printer['id'],printer['url'],printer.get('api_key',''),printer.get('access_code',''),printer.get('serial',''),printer_profile(printer))
    with LOCK:
        for old in list(FEEDS):
            if old[0] == printer['id'] and old != key:
                FEEDS.pop(old).close()
        if key not in FEEDS:
            if printer_profile(printer) == 'bambu-x2d':
                FEEDS[key] = BambuFeed(printer)
                return FEEDS[key]
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
    rows = temperature_rows(status,config)
    if printer_profile(printer)=='bambu-x2d':
        for row in rows:
            if row['id'] in ('extruder','heater_bed'):
                row.update(editable=True,min=0,max=300 if row['id']=='extruder' else 110,configured_limit=False)
                if row['id']=='extruder': row['name']='Active nozzle'
    display_state=COMPLETION.display(printer['id'],status.get('print_stats',{}))
    return version, {'status':status,'display_state':display_state,'temperatures':rows,
                     'controls':control_rows(status,config),'printer':public_printer(printer),
                     'connection_mode':mode,
                     'u1_filament':u1_filament.slots(status) if printer_profile(printer)=='snapmaker-u1' else [],
                     'u1_filament_job':U1_FILAMENT_JOBS.get(printer['id'])}

U1_FILAMENT_LOCK = threading.Lock()
U1_FILAMENT_JOBS = {}

def run_u1_filament(printer, steps):
    """Run one U1 load/unload in the background: a full unload takes about 90 s, longer than proxies wait."""
    CURRENT.printer = printer
    error = None
    try:
        for step in steps: api('/printer/gcode/script', {'script': step}, timeout=600)
    except ValueError as failure:
        error = str(failure) or 'The printer stopped the filament change'
    except Exception as failure:
        # The connection dropped; the printer may still be working, so leave its heaters alone.
        error = 'Lost contact with the printer during the filament change. Check the printer screen. ('+str(failure)+')'
    if error is None or not error.startswith('Lost contact'):
        try: api('/printer/gcode/script', {'script': u1_filament.STOP}, timeout=60)
        except Exception: pass
    with U1_FILAMENT_LOCK:
        U1_FILAMENT_JOBS[printer['id']] = {**U1_FILAMENT_JOBS.get(printer['id'], {}), 'running': False, 'error': error}
    feed_for(printer).touch()

def start_u1_filament(data):
    printer = dict(CURRENT.printer)
    if printer_profile(printer) != 'snapmaker-u1': raise ValueError('Filament load and unload is only available on the Snapmaker U1')
    status = api('/printer/objects/query?' + '&'.join(urllib.parse.quote(x) for x in u1_filament.OBJECTS))['status']
    mode, index = data.get('mode'), data.get('extruder')
    if mode == 'color':
        api('/printer/gcode/script', {'script': u1_filament.color_script(index, data.get('color'), status)})
        return {'color': data['color'].upper(), 'extruder': index}
    steps = u1_filament.scripts(mode, index, status)
    with U1_FILAMENT_LOCK:
        if (U1_FILAMENT_JOBS.get(printer['id']) or {}).get('running'): raise ValueError('A filament change is already running')
        U1_FILAMENT_JOBS[printer['id']] = {'mode': mode, 'extruder': index, 'running': True, 'error': None}
    threading.Thread(target=run_u1_filament, args=(printer, steps), daemon=True, name='u1-filament-'+printer['id']).start()
    return {'started': True, 'mode': mode, 'extruder': index}



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
    files = api('/server/files/list?root=gcodes', timeout=FILE_LIST_TIMEOUT)
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
# Site icons are public so the sign-in page can show them.
ICONS = {'/favicon.ico': 'favicon.ico', **{'/icons/'+name: name for name in ('favicon-32.png', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png')}}
UPLOADS = UploadJobs(CONFIG.parent/'uploads', UPLOAD_LIMIT)

def normalize_print_archive(source, destination, filename):
    """Swap the selected plate with plate 1, preserving sliced toolpaths."""
    import xml.etree.ElementTree as ET
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        plate = choose_plate(filename, plate_numbers(archive))
        if sum(i.file_size for i in archive.infolist()) > 512*1024*1024:
            raise ValueError('Expanded 3MF is too large')
        def swap_number(n):
            return 1 if n == plate else plate if n == 1 else n
        def rename(value):
            return re.sub(r'(Metadata/(?:plate_no_light|plate|top|pick)_)(\d+)(?=[.])',
                          lambda m:m[1]+str(swap_number(int(m[2]))),value)
        with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as output:
            for item in archive.infolist():
                name = rename(item.filename)
                if item.filename in ('Metadata/model_settings.config','Metadata/slice_info.config'):
                    if item.file_size > 2*1024*1024: raise ValueError('Plate metadata is too large')
                    root = ET.fromstring(archive.read(item))
                    for element in root.iter('metadata'):
                        value = element.get('value','')
                        if element.get('key') in ('index','plater_id') and value.isdigit():
                            element.set('value',str(swap_number(int(value))))
                        else: element.set('value',rename(value))
                    output.writestr(name,ET.tostring(root,encoding='utf-8',xml_declaration=True))
                elif item.filename.endswith('.rels') or item.filename == '[Content_Types].xml':
                    if item.file_size > 2*1024*1024: raise ValueError('Archive references are too large')
                    output.writestr(name,rename(archive.read(item).decode('utf-8')).encode('utf-8'))
                else:
                    with archive.open(item) as incoming, output.open(name,'w') as outgoing:
                        while chunk := incoming.read(65536): outgoing.write(chunk)
    length = destination.tell()
    if length > UPLOAD_LIMIT: raise ValueError('Normalized 3MF is too large')
    destination.seek(0)
    return length

def plate_one_filename(filename):
    return re.sub(r'_plate_\d+(?=\.gcode\.3mf$)','_plate_1',filename,flags=re.I)

def normalized_existing_print(filename):
    plan = print_plan(filename)
    if plan['plate'] == 1: return filename
    key = (CURRENT.printer['id'],'normalized:'+filename)
    with LOCK: cached = CACHE.get(key)
    if cached and cached[1]['modified'] == plan.get('modified'):
        if any(f['path']==cached[1]['filename'] for f in api('/server/files/list?root=gcodes', timeout=FILE_LIST_TIMEOUT)):
            return cached[1]['filename']
    p = CURRENT.printer
    request = urllib.request.Request(p['url']+'/server/files/gcodes/'+urllib.parse.quote(filename),
                headers={'X-Api-Key':p['api_key']} if p.get('api_key') else {})
    with tempfile.TemporaryFile() as source:
        total = 0
        with OPENER.open(request,timeout=30) as response:
            while chunk := response.read(65536):
                total += len(chunk)
                if total > UPLOAD_LIMIT: raise ValueError('3MF exceeds supported archive size')
                source.write(chunk)
        source.seek(0)
        result = upload_print_file(source,total,filename)
    with LOCK: CACHE[key]=(time.monotonic(),{'filename':result['filename'],'modified':plan.get('modified')})
    return result['filename']

def checked_upload_name(printer, value):
    if printer_profile(printer) != 'bambu-x2d': return upload_filename(value)
    if not isinstance(value, str) or not value or posixpath.basename(value) != value or '\\' in value:
        raise ValueError('Choose a filename without folders')
    return value

def save_upload(job, source):
    CURRENT.printer = selected(job['printer'])
    try:
        if printer_profile(CURRENT.printer) == 'bambu-x2d': return bambu_files().upload(source, job['size'], job['filename'])
        return upload_print_file(source, job['size'], job['filename'])
    except (TimeoutError, urllib.error.URLError) as error:
        if isinstance(error, urllib.error.URLError) and not isinstance(error.reason, TimeoutError): raise
        raise ValueError('The printer did not respond in time. Check that it is online, then try again.')

def upload_filename(value):
    if not isinstance(value,str) or not value or len(value.encode('utf-8'))>200 or any(ord(c)<32 for c in value) or any(c in value for c in ('/','\\','"')) or value.startswith('.'):
        raise ValueError('Invalid upload filename')
    if not value.lower().endswith(('.gcode','.gco','.g','.gcode.3mf')):
        raise ValueError('Upload a sliced G-code or .gcode.3mf print file')
    return value

def upload_print_file(source, length, filename):
    if filename.lower().endswith('.gcode.3mf'):
        with tempfile.TemporaryFile(dir=CONFIG.parent) as normalized:
            try: length = normalize_print_archive(source,normalized,filename)
            except zipfile.BadZipFile: raise ValueError('Invalid sliced 3MF archive')
            return send_print_file(normalized,length,plate_one_filename(filename))
    return send_print_file(source,length,filename)

def send_print_file(source, length, filename):
    filename = upload_filename(filename)
    available = api('/server/files/list?root=gcodes', timeout=FILE_LIST_TIMEOUT)
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

def timelapse_callbacks(printer):
    def snapshot():
        CURRENT.printer = printer
        cam = camera()
        if not cam: raise ValueError('No camera configured for this printer')
        if cam.get('service') == 'snapmaker-lan':
            return snapmaker_camera().snapshot(OPENER)
        if cam.get('service') == 'bambu-lan': return bambu_camera().snapshot()
        with OPENER.open(camera_url(cam['snapshot_url']), timeout=12) as response:
            return response.read(10*1024*1024)
    def state():
        CURRENT.printer = printer
        _,status,_,error,_ = feed_for(printer).snapshot()
        if not error and status.get('print_stats'): return status['print_stats']
        return api('/printer/objects/query?print_stats')['status']['print_stats']
    return snapshot, state

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def reply(self, value, status=200, kind='application/json', headers=None):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', (headers or {}).get('Cache-Control','no-store'))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        for key, content in (headers or {}).items():
            if key.lower()!='cache-control':self.send_header(key, content)
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
                if not self.session_active(): return
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
        allowed = {f'{BIND}:{PORT}', f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
        if EXTRA_BIND:
            allowed.add(f'{EXTRA_BIND}:{PORT}')
        if PUBLIC_HOST:
            allowed.add(PUBLIC_HOST)
        return self.headers.get('Host') in allowed

    def origin_ok(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        expected = ('https://' if PUBLIC_HOST and host == PUBLIC_HOST else 'http://') + host
        return not origin or origin == expected

    def session_active(self):
        return not PUBLIC_HOST or self.headers.get('Host') != PUBLIC_HOST or LOGIN.valid(self.headers.get('Cookie'))

    def authenticated(self):
        if not PUBLIC_HOST or self.headers.get('Host') != PUBLIC_HOST:
            return True
        if not AUTH_USER or not AUTH_HASH:
            self.reply({'error': 'Public access requires configured login credentials'}, 503)
            return False
        if LOGIN.valid(self.headers.get('Cookie')):
            return True
        self.close_connection = True
        if self.command == 'GET' and urllib.parse.urlsplit(self.path).path in ('/', '/login'):
            self.reply((ROOT/'login.html').read_bytes().replace(b'__LOGIN_TOKEN__', LOGIN_TOKEN.encode()), kind='text/html; charset=utf-8')
        else:
            self.reply({'error': 'Sign in to Print Center'}, 401)
        return False

    def login_client(self):
        """Rate-limit key. Behind a local tunnel every peer is this host, so use the
        client IP the tunnel reports; other peers cannot supply that header."""
        peer = self.client_address[0]
        if peer not in {'127.0.0.1', '::1', BIND, EXTRA_BIND}:
            return peer
        try:
            return str(ipaddress.ip_address(self.headers.get('CF-Connecting-IP', '').strip()))
        except ValueError:
            return peer

    def login(self):
        self.close_connection = True
        if not PUBLIC_HOST or self.headers.get('Host') != PUBLIC_HOST or not AUTH_HASH or not AUTH_USER:
            return self.reply({'error': 'Login is unavailable on this address'}, 403)
        if not self.origin_ok() or not hmac.compare_digest(self.headers.get('X-Login-Token',''), LOGIN_TOKEN):
            return self.reply({'error': 'Refresh the login page and try again'}, 403)
        address = self.login_client()
        if LOGIN.limited(address):
            return self.reply({'error': 'Too many sign-in attempts. Try again in a minute.'}, 429)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096: raise ValueError('Invalid login request')
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict): raise ValueError('Invalid login request')
            if not LOGIN.verify(data.get('username'), data.get('password'), AUTH_USER, AUTH_HASH):
                return self.reply({'error': 'Incorrect username or password'}, 401)
        except (ValueError, TypeError):
            return self.reply({'error': 'Invalid login request'}, 400)
        LOGIN.clear_failures(address)
        remember = data.get('remember') is True
        try:
            token = LOGIN.issue(remember=remember)
        except OSError:
            return self.reply({'error':'Could not save your session. Please try again.'},503)
        age = '; Max-Age='+str(WebAuth.REMEMBER_AGE) if remember else ''
        self.reply({'ok': True}, headers={'Set-Cookie': 'print_center_session='+token+'; Path=/'+age+'; HttpOnly; Secure; SameSite=Strict'})
    def do_GET(self):
        if not self.host_ok():
            return self.reply({'error': 'Invalid host'}, 403)
        icon = ICONS.get(urllib.parse.urlsplit(self.path).path)
        if icon:
            return self.reply((ROOT/'icons'/icon).read_bytes(), kind='image/x-icon' if icon.endswith('.ico') else 'image/png', headers={'Cache-Control': 'public, max-age=86400'})
        if not self.authenticated():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == '/api/upload/status':
                try: return self.reply(UPLOADS.status(query.get('upload', [''])[0]))
                except ValueError as error: return self.reply({'error': str(error)}, 404)
            if path == '/api/library':
                return self.reply(LIBRARY.list(query.get('q',[''])[0],query.get('archived',['0'])[0]=='1',query.get('favorites',['0'])[0]=='1',query.get('offset',['0'])[0],query.get('folder',['all'])[0]))
            if path == '/api/library/preview':
                return self.reply(LIBRARY.preview(query.get('id',[''])[0]),kind='image/webp')
            if path == '/api/library/file':
                row,file=LIBRARY.file(query.get('id',[''])[0])
                self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(row['bytes']))
                self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(row['filename']));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.close_connection=True
                with file.open('rb') as source:
                    while self.session_active() and (chunk:=source.read(65536)):self.wfile.write(chunk)
                return
            if path == '/library.js':
                return self.reply((ROOT/'library.js').read_bytes(),kind='application/javascript; charset=utf-8')
            if path.startswith('/printer-photos/'):
                name = path.removeprefix('/printer-photos/')
                if not PHOTO_NAME.fullmatch(name) or not any(p.get('photo') == name for p in printers()):
                    return self.reply({'error': 'Photo not found'}, 404)
                file = CONFIG.parent/'printer-photos'/name
                if not file.is_file(): return self.reply({'error': 'Photo not found'}, 404)
                return self.reply(file.read_bytes(), kind='image/webp')
            if path == '/api/events':
                return self.events(query.get('printer',['all'])[0])
            if path == '/api/printers':
                return self.reply({'printers': [public_printer(p) for p in printers()]})
            if path.startswith(('/api/', '/camera/')):
                CURRENT.printer = selected(query.get('printer', ['qidi'])[0])
                if printer_profile(CURRENT.printer) == 'bambu-x2d' and path not in ('/api/status','/api/camera','/api/timelapses','/camera/stream','/camera/snapshot','/api/files','/api/metadata','/api/thumbnail','/api/file-download','/api/bambu-print-plan','/api/plate-status','/api/plate-image'):
                    return self.reply({'error':'This Bambu LAN profile currently supports live status and temperature monitoring. Files, camera, history and printer controls are not yet supported.'},400)
            if path == '/api/bambu-print-plan':
                return self.reply({'plates':bambu_files().archive_info(query.get('filename',[''])[0])[0].get('plates',[])})
            if path == '/api/plate-status':
                return self.reply(PLATES.view(CURRENT.printer))
            if path == '/api/plate-image':
                return self.reply(PLATES.image(CURRENT.printer,query.get('name',['latest'])[0]),kind='image/jpeg')
            if path == '/api/file-download' and printer_profile(CURRENT.printer)!='bambu-x2d':
                filename=existing_printer_file(query.get('filename',[''])[0])
                printer=CURRENT.printer
                headers={'X-Api-Key':printer['api_key']} if printer.get('api_key') else {}
                req=urllib.request.Request(printer['url']+'/server/files/gcodes/'+urllib.parse.quote(filename,safe='/'),headers=headers)
                with OPENER.open(req,timeout=30) as source:
                    self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Cache-Control','no-store')
                    self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(posixpath.basename(filename)))
                    if source.headers.get('Content-Length'):self.send_header('Content-Length',source.headers['Content-Length'])
                    self.end_headers();self.close_connection=True
                    while self.session_active() and (chunk:=source.read(65536)):self.wfile.write(chunk)
                return
            if path == '/api/file-download' and printer_profile(CURRENT.printer)=='bambu-x2d':
                name=query.get('filename',[''])[0]
                with bambu_files().download(name) as (ftp,filename,size):
                    self.send_response(200);self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Length',str(size));self.send_header('Cache-Control','no-store')
                    self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(posixpath.basename(filename)))
                    self.end_headers();self.close_connection=True
                    ftp.read_into(filename,self.wfile,size,self.session_active)
                return
            if path == '/api/timelapses':
                return self.reply({'videos': TIMELAPSES.list(CURRENT.printer['id'])})
            if path == '/api/timelapse/thumbnail':
                return self.reply(TIMELAPSES.thumbnail(query.get('id',[''])[0],CURRENT.printer['id']),kind='image/jpeg')
            if path == '/api/timelapse/video':
                folder = TIMELAPSES.path(query.get('id',[''])[0])
                row = json.loads((folder/'record.json').read_text())
                if row['printer'] != CURRENT.printer['id'] or row['status'] != 'ready':
                    return self.reply({'error':'Video is not available'},404)
                file = folder/'video.mp4';size=file.stat().st_size;start=0;end=size-1
                range_header=self.headers.get('Range')
                if range_header:
                    match=re.fullmatch(r'bytes=(\d+)-(\d*)',range_header)
                    if not match:return self.reply({'error':'Unsupported range'},416)
                    start=int(match[1]);end=min(end,int(match[2])) if match[2] else end
                    if start>end:return self.reply({'error':'Invalid range'},416)
                self.send_response(206 if range_header else 200)
                self.send_header('Content-Type','video/mp4');self.send_header('Content-Length',str(end-start+1))
                self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','no-store')
                if range_header:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                if query.get('download')==['1']:self.send_header('Content-Disposition','attachment; filename="print-center-'+row['id']+'.mp4"')
                self.end_headers()
                with file.open('rb') as source:
                    source.seek(start);remaining=end-start+1
                    while remaining and self.session_active():
                        chunk=source.read(min(65536,remaining))
                        if not chunk:break
                        self.wfile.write(chunk);remaining-=len(chunk)
                return
            if path == '/':
                page = (ROOT/'index.html').read_bytes().replace(b'__CONTROL_TOKEN__', TOKEN.encode()).replace(b'__PUBLIC_ACCESS__', b'1' if PUBLIC_HOST and self.headers.get('Host') == PUBLIC_HOST else b'0')
                self.reply(page, kind='text/html; charset=utf-8')
            elif path in ('/printer-models/qidi-q2.png', '/printer-models/snapmaker-u1.png', '/printer-models/bambu-x2d-default.jpg'):
                image = ROOT/'printer-models'/path.rsplit('/', 1)[1]
                kind = {'.png':'image/png', '.webp':'image/webp', '.jpg':'image/jpeg'}[image.suffix]
                self.reply(image.read_bytes(), kind=kind)
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
                self.reply(bambu_files().listing(trash=query.get('trash')==['1'],refresh=query.get('refresh')==['1']) if printer_profile(CURRENT.printer)=='bambu-x2d' else printer_files(refresh=query.get('refresh')==['1']))
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
                self.reply(image, kind=kind,headers={'Cache-Control':'private, max-age=60'})
            elif path in ('/camera/stream', '/camera/snapshot'):
                monitor=cached_camera(dict(CURRENT.printer))
                frame=monitor.snapshot()
                if path.endswith('snapshot'):
                    return self.reply(frame,kind='image/jpeg',headers={'Cache-Control':'no-store'})
                self.send_response(200)
                self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control','no-store')
                self.send_header('Connection','close')
                self.end_headers();self.close_connection=True
                while self.session_active():
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(frame)).encode()+b'\r\n\r\n'+frame+b'\r\n')
                    self.wfile.flush();time.sleep(.5)
                    try:frame=monitor.snapshot()
                    except Exception:return

            else:
                self.reply({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.reply({'error': str(error), 'offline': True}, 502)
    def do_POST(self):
        if not self.host_ok():
            return self.reply({'error': 'Invalid host'}, 403)
        if urllib.parse.urlsplit(self.path).path == '/api/login':
            return self.login()
        if not self.authenticated():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in ('/api/control', '/api/printers', '/api/upload', '/api/upload/start', '/api/upload/chunk', '/api/upload/finish', '/api/logout', '/api/printer-settings', '/api/printer-photo', '/api/plate-detection', '/api/library', '/api/library/upload'):
            return self.reply({'error': 'Not found'}, 404)
        if not self.host_ok() or self.headers.get('X-Control-Token') != TOKEN:
            return self.reply({'error': 'Refresh this dashboard before using controls'}, 403)
        if not self.origin_ok():
            return self.reply({'error': 'Invalid origin'}, 403)
        if path == '/api/logout':
            self.close_connection = True
            LOGIN.revoke(self.headers.get('Cookie'))
            return self.reply({'ok': True}, headers={'Set-Cookie': 'print_center_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if path == '/api/upload/chunk':
                self.close_connection = True
                self.connection.settimeout(30)
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try: offset = int(query.get('offset', [''])[0])
                except ValueError: raise ValueError('Invalid upload offset')
                try: return self.reply(UPLOADS.chunk(query.get('upload', [''])[0], offset, self.rfile, length))
                except UploadBusy as error: return self.reply({'error': str(error)}, 409)
            if path in ('/api/upload/start', '/api/upload/finish'):
                if not 0 < length <= 8192: raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(length))
                if path == '/api/upload/finish': return self.reply(UPLOADS.finish(data.get('upload'), save_upload))
                printer = selected(data.get('printer', ''))
                return self.reply(UPLOADS.start(printer['id'], checked_upload_name(printer, data.get('filename', '')), data.get('size')))
            if path == '/api/library/upload':
                self.close_connection=True;self.connection.settimeout(120)
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                return self.reply({'file':LIBRARY.upload(query.get('model',[''])[0],query.get('filename',[''])[0],self.rfile,length,query.get('profile',[''])[0])})
            if path == '/api/library':
                if not 0<length<=32768:raise ValueError('Invalid library request size')
                data=json.loads(self.rfile.read(length));action=data.get('action')
                if action=='delete_file':
                    if data.get('confirmed') is not True:raise ValueError('Confirm file deletion')
                    return self.reply(LIBRARY.delete_file(data.get('file')))
                if action=='folder':return self.reply({'folder':LIBRARY.save_folder(data)})
                if action=='delete_folder':
                    if data.get('confirmed') is not True:raise ValueError('Confirm folder deletion')
                    return self.reply(LIBRARY.delete_folder(data.get('id')))
                if action=='assign':return self.reply(LIBRARY.assign(data.get('file'),data.get('profile')))
                if action=='create':return self.reply({'model':LIBRARY.create(data)},201)
                if action in ('edit','favorite','archive','restore'):return self.reply({'model':LIBRARY.update(data.get('id'),data)})
                if action=='send':
                    if data.get('confirmed') is not True:raise ValueError('Confirm this file was sliced for the selected printer')
                    CURRENT.printer=selected(data.get('printer',''))
                    profile=printer_profile(CURRENT.printer)
                    row,file=LIBRARY.variant(data.get('model'),profile)
                    name=row['filename']
                    if name.lower().endswith('.3mf') and not name.lower().endswith('.gcode.3mf'):name=name[:-4]+'.gcode.3mf'
                    with file.open('rb') as source:
                        result=bambu_files().upload(source,row['bytes'],name) if profile=='bambu-x2d' else upload_print_file(source,row['bytes'],upload_filename(name))
                    return self.reply({'ok':True,**result})
                raise ValueError('Unknown library action')
            if path == '/api/printer-photo':
                self.close_connection = True
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                pid = selected(query.get('printer', [''])[0])['id']
                if not 0 < length <= PHOTO_UPLOAD_LIMIT:
                    raise ValueError('Choose a photo up to 5 MB')
                self.connection.settimeout(30)
                raw = self.rfile.read(length)
                if len(raw) != length: raise ValueError('Incomplete photo upload')
                reset = query.get('reset') == ['1']
                name = '' if reset else store_photo(CONFIG.parent, raw)
                with LOCK:
                    values = printers()
                    printer = next((p for p in values if p['id'] == pid), None)
                    if printer is None: raise ValueError('Select a saved printer')
                    if reset: printer.pop('photo', None)
                    else: printer['photo'] = name
                    save_printers(values)
                return self.reply({'printer': public_printer(printer)})
            if path == '/api/upload':
                self.close_connection = True
                if not 0 < length <= UPLOAD_LIMIT: raise ValueError('Upload limit is 200 MB per file')
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                CURRENT.printer = selected(query.get('printer',[''])[0])
                is_bambu=printer_profile(CURRENT.printer)=='bambu-x2d'
                filename = query.get('filename',[''])[0] if is_bambu else upload_filename(query.get('filename',[''])[0])
                if is_bambu and (posixpath.basename(filename)!=filename or '\\' in filename):raise ValueError('Choose a filename without folders')
                self.connection.settimeout(120)
                with tempfile.TemporaryFile(dir=CONFIG.parent) as source:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536,remaining))
                        if not chunk: raise ValueError('Incomplete upload')
                        source.write(chunk)
                        remaining -= len(chunk)
                    source.seek(0)
                    value = bambu_files().upload(source,length,filename) if is_bambu else upload_print_file(source,length,filename)
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
                        existing = selected(data['id']) if action == 'edit' else {}
                        profile = validate_printer_profile(data.get('profile',printer_profile(existing)))
                        bambu = profile == 'bambu-x2d'
                        address = str(data.get('url','')).replace('mqtts://','https://',1) if bambu else data.get('url','')
                        url = valid_url(address, 8883 if bambu else 7125)
                        if bambu:
                            parsed_url = urllib.parse.urlsplit(url)
                            if parsed_url.port != 8883: raise ValueError('Bambu LAN uses port 8883. Enter the printer IP without a Moonraker port.')
                            url = 'mqtts://' + parsed_url.netloc
                        camera_base = '' if bambu else valid_url(data.get('camera_base') or urllib.parse.urlsplit(url).scheme + '://' + urllib.parse.urlsplit(url).hostname)
                        if not bambu and urllib.parse.urlsplit(camera_base).hostname != urllib.parse.urlsplit(url).hostname:
                            raise ValueError('Camera and Moonraker must use the same printer address')
                        key = str(data.get('api_key', '')).strip() or existing.get('api_key', '')
                        if len(key) > 512 or '\n' in key or '\r' in key:
                            raise ValueError('Invalid API key')
                        profile = validate_printer_profile(data.get('profile',printer_profile(existing)))
                        record = {'id': existing.get('id', secrets.token_hex(8)), 'name': name, 'url': url, 'camera_base': camera_base, 'api_key': key, 'timelapse_mode':existing.get('timelapse_mode','interval'), 'profile':profile}
                        if existing.get('photo'): record['photo'] = existing['photo']
                        if bambu:
                            record.update(validate_credentials(data,existing if printer_profile(existing)=='bambu-x2d' else {}))
                            record['api_key'] = ''
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
            if path == '/api/plate-detection':
                printer = dict(CURRENT.printer)
                action = data.get('action')
                if action == 'settings':
                    return self.reply(PLATES.configure(printer,data))
                snapshot, read_state = timelapse_callbacks(printer)
                if action == 'reference':
                    if read_state().get('state') not in ('standby','complete','completed','cancelled','error','ready'):
                        raise ValueError('Save references only while the printer is idle and its state is known')
                    if data.get('confirmed') is not True: raise ValueError('Confirm the physical plate condition first')
                    return self.reply(PLATES.reference(printer,data.get('label'),snapshot(),confirmed=True))
                if action == 'check':
                    return self.reply(PLATES.start(printer,snapshot,read_state),202)
                raise ValueError('Unknown plate detection action')
            if path == '/api/printer-settings':
                return self.reply({'printer':save_printer_settings(CURRENT.printer['id'],data.get('timelapse_mode'))})
            if printer_profile(CURRENT.printer) == 'bambu-x2d':
                if data.get('action')=='start':
                    feed=feed_for(CURRENT.printer)
                    _,live,_,error,_=feed.snapshot()
                    if error or live.get('print_stats',{}).get('state') not in ('standby','complete','cancelled'):raise ValueError('Wait until the printer is connected and idle')
                    plates=bambu_files().archive_info(data.get('filename'))[0].get('plates',[])
                    plate=next((p for p in plates if p['id']==data.get('plate')),None)
                    if plate is None:raise ValueError('Select a sliced plate from this file')
                    slots={int(u['id'])*4+int(s['id']):s for u in live.get('bambu',{}).get('ams',[]) for s in u.get('slots',[]) if str(u['id']).isdigit() and str(s['id']).isdigit()}
                    mapping=data.get('ams_mapping',{})
                    if not isinstance(mapping,dict):raise ValueError('Select filament slots')
                    for f in plate['filaments']:
                        slot=slots.get(mapping.get(str(f['id'])))
                        if not slot or slot.get('material','Empty').upper()!=f['type'].upper():raise ValueError('Choose a loaded AMS slot matching each filament material')
                    data['bambu_plate']=plate
                    return self.reply({'ok':True,'result':feed.command(data)})
                if data.get('action') in ('file_trash','file_restore'):
                    if data['action']=='file_trash':
                        _,live,_,error,_=feed_for(CURRENT.printer).snapshot()
                        if error or live.get('print_stats',{}).get('state') in ('printing','paused','unknown',None):
                            raise ValueError('Wait until the printer is idle before moving files to trash')
                    return self.reply({'ok':True,**bambu_files().trash(data.get('filename'),restore=data['action']=='file_restore')})
                return self.reply({'ok':True,'result':feed_for(CURRENT.printer).command(data)})
            action = data.get('action')
            if action=='file_delete':
                if data.get('confirmed') is not True:raise ValueError('Confirm file deletion first')
                _,live,_,error,_=feed_for(CURRENT.printer).snapshot()
                if error or live.get('print_stats',{}).get('state') not in ('standby','complete','cancelled'):raise ValueError('Wait until the printer is idle before deleting files')
                filename=existing_printer_file(data.get('filename'))
                return self.reply({'ok':True,'result':api('/server/files/gcodes/'+urllib.parse.quote(filename,safe='/'),method='DELETE')})
            if action in ('pause' ,'resume','cancel','emergency_stop'):
                endpoint = '/printer/emergency_stop' if action == 'emergency_stop' else '/printer/print/' + action
                result = api(endpoint, {})
            elif action == 'start':
                if PLATES.config(CURRENT.printer['id'])['enabled'] and data.get('plate_confirmed') is not True:
                    raise ValueError('Confirm that you have checked the build plate before starting')
                filename = data.get('filename')
                available = api('/server/files/list?root=gcodes', timeout=FILE_LIST_TIMEOUT)
                if filename not in [f['path'] for f in available] or not filename.lower().endswith(('.gcode', '.gco', '.g', '.gcode.3mf')):
                    raise ValueError('Select an existing G-code file')
                print_status = api('/printer/objects/query?print_stats')['status']['print_stats']
                state = print_status['state']
                if state in ('printing','paused'):
                    raise ValueError('A print job is already active')
                record = data.get('timelapse',False)
                if type(record) is not bool: raise ValueError('Invalid timelapse selection')
                def prepare_recording(watch_filename=filename):
                    if record:
                        snapshot, read_state = timelapse_callbacks(dict(CURRENT.printer))
                        TIMELAPSES.prepare(CURRENT.printer['id'],watch_filename,snapshot,read_state,mode=CURRENT.printer.get('timelapse_mode','interval'))
                if filename.lower().endswith('.gcode.3mf'):
                    if 'plateindex' not in print_status:
                        raise ValueError('This printer needs a plain G-code file. Export G-code from your slicer.')
                    status = control_status()
                    if status.get('multi_color_controller',{}).get('hardware',{}).get('connected'):
                        plan = print_plan(filename)
                        script = mapping_script(data,status,plan['filaments'])
                        api('/printer/gcode/script', {'script':script})
                    # Match the installed Fluidd client: Moonraker stages the
                    # 3MF in its cache before issuing SDCARD_PRINT_FILE.
                    start_filename = normalized_existing_print(filename)
                    prepare_recording(start_filename)
                    result = api('/printer/print/start', {'filename': start_filename}, timeout=60)
                else:
                    prepare_recording()
                    result = api('/printer/print/start', {'filename': filename})
            elif action == 'u1_filament':
                result = start_u1_filament(data)
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

FRAME_CACHES={}
def cached_camera(printer):
    key=(printer['id'],printer['url'],printer.get('api_key',''),printer.get('access_code',''),printer.get('serial',''))
    with LOCK:
        if key not in FRAME_CACHES:
            def read():
                CURRENT.printer=printer
                cam=camera()
                if not cam:raise ValueError('No camera configured')
                if cam.get('service')=='bambu-lan':return bambu_camera().snapshot(OPENER)
                if cam.get('service')=='snapmaker-lan':return snapmaker_camera().snapshot(OPENER)
                with OPENER.open(camera_url(cam['snapshot_url']),timeout=8) as response:
                    return response.read(4*1024*1024+1)
            FRAME_CACHES[key]=FrameCache(read)
        return FRAME_CACHES[key]

def background_printers():
    while True:
        try:
            values=printers();ids={p['id'] for p in values}
            for printer in values:
                try:
                    feed=feed_for(printer)
                    _,status,_,error,_=feed.snapshot()
                    if not error:COMPLETION.display(printer['id'],status.get('print_stats',{}))
                    cached_camera(printer)
                except Exception:continue
            with LOCK:
                for key in list(FRAME_CACHES):
                    current=next((p for p in values if p['id']==key[0]),None)
                    if current is None or key!=(current['id'],current['url'],current.get('api_key',''),current.get('access_code',''),current.get('serial','')):
                        FRAME_CACHES.pop(key).close()
                for key in list(FEEDS):
                    if key[0] not in ids:FEEDS.pop(key).close()
        except Exception:pass
        time.sleep(5)

def automatic_plate_checks():
    while True:
        time.sleep(5)
        for printer in printers():
            try:
                if not PLATES.config(printer['id'])['enabled']: continue
                _,status,_,error,_ = feed_for(printer).snapshot()
                if error: continue
                snapshot,read_state = timelapse_callbacks(dict(printer))
                PLATES.automatic_tick(dict(printer),status.get('print_stats',{}),snapshot,read_state)
            except Exception:
                continue

if __name__ == '__main__':
    threading.Thread(target=background_printers,daemon=True).start()
    threading.Thread(target=warm_file_lists,daemon=True).start()
    threading.Thread(target=automatic_plate_checks,daemon=True).start()
    addresses = list(dict.fromkeys([BIND] + ([EXTRA_BIND] if EXTRA_BIND else [])))
    servers = [ThreadingHTTPServer((address, PORT), Handler) for address in addresses]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for address in addresses:
        print(f'Print Center: http://{address}:{PORT}', flush=True)
    servers[0].serve_forever()
