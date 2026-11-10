"""Bambu LAN telemetry and bounded, acknowledged printer controls."""
import copy
import json
import math
import pathlib
import re
import ssl
import threading
import time
import urllib.parse

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def validate_credentials(data, existing):
    code = str(data.get('access_code', '')).strip() or existing.get('access_code', '')
    serial = str(data.get('serial', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9]{8}', code):
        raise ValueError('Enter the 8-character LAN access code from the printer')
    if serial and not re.fullmatch(r'[A-Za-z0-9]{8,32}', serial):
        raise ValueError('Enter a valid printer serial number, or leave it blank for detection')
    return {'access_code': code, 'serial': serial}


def normalize(report):
    state = {'RUNNING': 'printing', 'PAUSE': 'paused', 'FINISH': 'complete',
             'FAILED': 'error', 'IDLE': 'standby', 'PREPARE': 'printing',
             'SLICING': 'printing'}.get(report.get('gcode_state'), 'unknown')
    if number(report.get('print_error')) not in (None, 0):
        state = 'error'
    progress = number(report.get('mc_percent'))
    status = {
        'print_stats': {'state': state, 'filename': report.get('subtask_name') or report.get('gcode_file') or '',
                        'info': {'current_layer': number(report.get('layer_num')),
                                 'total_layer': number(report.get('total_layer_num'))}},
        'virtual_sdcard': {'progress': min(1, max(0, (progress or 0) / 100))},
        'webhooks': {'state': 'ready'}, 'heaters': {'available_heaters': []}}
    remaining = number(report.get('mc_remaining_time'))
    if remaining is not None:
        status['print_stats']['remaining_time'] = max(0, remaining * 60)
    speed = number(report.get('spd_mag'))
    if speed is not None: status['gcode_move'] = {'speed_factor':speed/100}
    started = number(report.get('gcode_start_time'))
    if state in ('printing','paused') and started and 946684800 < started <= time.time():
        status['print_stats']['print_duration'] = max(0, time.time()-started)
    for key, actual, target in [('extruder', 'nozzle_temper', 'nozzle_target_temper'),
                                ('heater_bed', 'bed_temper', 'bed_target_temper'),
                                ('temperature_sensor chamber', 'chamber_temper', 'chamber_target_temper')]:
        value = number(report.get(actual))
        if value is not None:
            status[key] = {'temperature': value, 'target': number(report.get(target))}
    device = report.get('device', {})
    for item in device.get('extruder', {}).get('info', []):
        raw = number(item.get('temp'))
        if raw is not None and 0 <= raw <= 0xffffffff:
            index = item.get('id')
            if index in (0,1):
                status['temperature_sensor nozzle_'+str(index+1)] = {'temperature':int(raw)&0xffff,'target':(int(raw)>>16)&0xffff}
    chamber = number(device.get('ctc',{}).get('info',{}).get('temp'))
    if chamber is not None:
        status['temperature_sensor chamber'] = {'temperature':int(chamber)&0xffff,'target':(int(chamber)>>16)&0xffff}
    status['bambu'] = printer_details(report)
    return status


def merge_report(target, delta):
    for key, value in delta.items():
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict): target[key] = {}
            merge_report(target[key], value)
        elif isinstance(value,list) and value and all(isinstance(x,dict) and 'id' in x for x in value):
            previous={str(x['id']):x for x in target.get(key,[]) if isinstance(x,dict) and 'id' in x}
            for item in value:
                row=previous.setdefault(str(item['id']),{})
                merge_report(row,item)
            target[key]=list(previous.values())
        else: target[key]=copy.deepcopy(value)


def printer_details(report):
    ams=report.get('ams',{})
    units=[]
    for unit in ams.get('ams',[]):
        slots=[]
        for slot in unit.get('tray',[]):
            color=str(slot.get('tray_color',''))
            color='#'+color[:6] if re.fullmatch(r'[0-9A-Fa-f]{8}',color) else None
            slots.append({'id':str(slot.get('id','')),'material':slot.get('tray_type','Empty'),
                          'color':color,'remaining':number(slot.get('remain'))})
        units.append({'id':str(unit.get('id','')),'temperature':number(unit.get('temp')),
                      'humidity':number(unit.get('humidity_raw')),'slots':slots})
    fans={}
    for key,label in [('cooling_fan_speed','Part cooling'),('big_fan1_speed','Auxiliary'),('big_fan2_speed','Chamber'),('heatbreak_fan_speed','Hotend')]:
        value=number(report.get(key))
        if value is not None: fans[key]={'name':label,'percent':round(max(0,min(15,value))*100/15)}
    lights=report.get('lights_report',[])
    return {'wifi':str(report.get('wifi_signal','')),'speed_level':number(report.get('spd_lvl')),
            'error_code':number(report.get('print_error')) or 0,'fault_count':len(report.get('hms',[])),
            'external_spools':[{'id':str(slot.get('id','')),'material':slot.get('tray_type') or 'Not reported','color':'#'+str(slot['tray_color'])[:6] if re.fullmatch(r'[0-9A-Fa-f]{8}',str(slot.get('tray_color',''))) else None} for slot in (report.get('vir_slot') or []) if isinstance(slot,dict)],
            'ams':units,'active_tray':str(ams.get('tray_now','255')),'fans':fans,
            'light':next((x.get('mode') for x in lights if x.get('node')=='chamber_light'),None),
            'camera_resolution':report.get('ipcam',{}).get('resolution',''),
            'nozzle_diameter':report.get('nozzle_diameter',''),'nozzle_type':report.get('nozzle_type','')}


def control_message(data, state, homed=''):
    action=data.get('action')
    if action=='start':
        if state not in ('standby','complete','cancelled'):raise ValueError('Wait until the printer is idle before starting')
        from bambu_files import file_path
        path=file_path(data.get('filename'))
        if not path.lower().endswith('.3mf'):raise ValueError('Export a sliced Bambu 3MF file first')
        plate=data.get('bambu_plate')
        if not isinstance(plate,dict) or not re.fullmatch(r'Metadata/plate_[1-9][0-9]{0,2}[.]gcode',plate.get('param','')) or not re.fullmatch(r'[0-9a-f]{32}',plate.get('md5','')):raise ValueError('Select a valid sliced plate')
        if data.get('plate_confirmed') is not True:raise ValueError('Confirm the build plate is installed and clear')
        use=data.get('use_ams')
        if type(use) is not bool:raise ValueError('Choose AMS or external spool')
        filaments=plate.get('filaments',[])
        if not filaments:raise ValueError('This file has no filament metadata. Re-export it from Bambu Studio.')
        if not use:raise ValueError('External-spool starting for this dual-nozzle printer is not supported yet. Use Bambu Studio for external spools.')
        mapping=data.get('ams_mapping')
        if not isinstance(mapping,dict):raise ValueError('Select an AMS slot for each filament')
        resolved=[-1]*max(f['id'] for f in filaments)
        for f in filaments:
            slot=mapping.get(str(f['id']))
            if type(slot) is not int or not (0<=slot<=103 or 128<=slot<=135):raise ValueError('Select an AMS slot for each filament')
            resolved[f['id']-1]=slot
        return 'print',{'command':'project_file','url':'ftp://'+urllib.parse.quote(path,safe='/'),'file':path,'param':plate['param'],'md5':plate['md5'],
            'subtask_name':pathlib.PurePosixPath(path).name,'project_id':'0','profile_id':'0','task_id':'0','subtask_id':'0',
            'bed_type':'auto','bed_leveling':1,'flow_cali':1,'vibration_cali':1,'layer_inspect':1,'timelapse':False,'use_ams':True,
            'ams_mapping':resolved,'ams_mapping2':[{'ams_id':255,'slot_id':255} if n<0 else {'ams_id':n,'slot_id':0} if n>=128 else {'ams_id':n//4,'slot_id':n%4} for n in resolved]}
    if action in ('home','jog'):
        if state not in ('standby','complete','cancelled'):raise ValueError('Manual movement is unavailable during an active print, error or unknown state')
        axis=data.get('axis')
        if action=='home':
            if axis not in ('all','X','Y','Z'):raise ValueError('Select a valid homing axis')
            if axis=='Z' and not all(a in homed for a in 'xy'):raise ValueError('Home X and Y first, or use Home all')
            return 'print',{'command':'gcode_line','param':'G28'+('' if axis=='all' else ' '+axis)+'\n'}
        amount=number(data.get('amount'))
        if axis not in ('X','Y','Z') or axis.lower() not in homed:raise ValueError('Home this axis in Print Center before jogging')
        if amount is None or not 0<abs(amount)<=(1 if axis=='Z' else 10):raise ValueError('Jog limit is 10 mm for X/Y and 1 mm for Z')
        feed=300 if axis=='Z' else 1800
        return 'print',{'command':'gcode_line','param':f'G91\nG1 {axis}{amount:g} F{feed}\nG90\n'}
    if action in ('pause','resume','cancel'):
        allowed=('printing',) if action=='pause' else ('paused',) if action=='resume' else ('printing','paused')
        if state not in allowed: raise ValueError('This command is unavailable in the current print state')
        return 'print', {'command':'stop' if action=='cancel' else action,'param':''}
    if action=='bambu_light':
        if type(data.get('enabled')) is not bool: raise ValueError('Choose light on or off')
        return 'system',{'command':'ledctrl','led_node':'chamber_light','led_mode':'on' if data['enabled'] else 'off',
                         'led_on_time':500,'led_off_time':500,'loop_times':1,'interval_time':1000}
    if action=='bambu_speed':
        if type(data.get('level')) is not int or data['level'] not in (1,2,3,4): raise ValueError('Choose a print speed preset')
        return 'print',{'command':'print_speed','param':str(data['level'])}
    if action=='bambu_fan':
        fan={'cooling_fan_speed':1,'big_fan1_speed':2,'big_fan2_speed':3}.get(data.get('fan'))
        percent=number(data.get('percent'))
        if not fan or percent is None or not 0<=percent<=100: raise ValueError('Choose a controllable fan and 0–100%')
        return 'print',{'command':'gcode_line','param':f'M106 P{fan} S{round(percent*255/100)}\n'}
    if action=='heat':
        heater=data.get('heater');target=number(data.get('target'))
        maximum=300 if heater=='extruder' else 110 if heater=='heater_bed' else None
        if maximum is None or target is None or not 0<=target<=maximum: raise ValueError('Unsupported heater or target temperature')
        return 'print',{'command':'gcode_line','param':f'{"M104" if heater=="extruder" else "M140"} S{target:g}\n'}
    raise ValueError('This Bambu control is not supported')


def certificate_serial(cert, expected=''):
    names = [value for group in cert.get('subject', ()) for name, value in group if name == 'commonName']
    serial = names[0] if len(names) == 1 else ''
    if not re.fullmatch(r'[A-Za-z0-9]{8,32}', serial):
        raise ssl.CertificateError('Printer certificate does not contain a valid serial number')
    if expected and serial.lower() != expected.lower():
        raise ssl.CertificateError('Printer certificate does not match the configured serial number')
    return serial


if mqtt:
    class VerifiedClient(mqtt.Client):
        """Validate Bambu CA and certificate serial before sending MQTT credentials."""
        def _ssl_wrap_socket(self, tcp_sock):
            sock = self._ssl_context.wrap_socket(tcp_sock, server_hostname=self.expected_serial or self._host)
            try:
                self.printer_serial = certificate_serial(sock.getpeercert(), self.expected_serial)
                return sock
            except Exception:
                sock.close()
                raise


class BambuFeed:
    def __init__(self, printer):
        self.printer = dict(printer)
        self.lock = threading.RLock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.client = None
        self.version, self.status, self.report = 0, {}, {}
        self.error, self.mode = 'Connecting to Bambu LAN', 'connecting'
        self.last_report = 0
        self.pending = {}
        self.sequence = int(time.time()*1000)%100000000
        self.homed = ''
        threading.Thread(target=self.run, daemon=True, name='bambu-' + printer['id']).start()

    def publish(self, error=None):
        with self.lock:
            self.error = error
            self.mode = 'bambu-mqtt' if error is None else 'offline'
            self.version += 1
            self.ready.set()

    def snapshot(self):
        with self.lock:
            return self.version, copy.deepcopy(self.status), {}, self.error, self.mode

    def close(self):
        self.stop.set()
        if self.client:
            self.client.disconnect()

    def message(self, client, userdata, message):
        if message.topic != 'device/' + client.printer_serial + '/report' or len(message.payload) > 1048576:
            return
        try:
            payload = json.loads(message.payload)
            for section, ack in payload.items():
                if not isinstance(ack,dict): continue
                with self.lock:
                    waiting=self.pending.get(str(ack.get('sequence_id','')))
                    if waiting and section==waiting['section'] and ack.get('command')==waiting['command'] and 'result' in ack:
                        waiting['result']=str(ack['result']).lower()
                        waiting['event'].set()
            value = payload.get('print')
            if not isinstance(value, dict) or not any(k in value for k in ('gcode_state', 'mc_percent', 'nozzle_temper', 'bed_temper', 'chamber_temper', 'nozzle_target_temper', 'bed_target_temper', 'chamber_target_temper', 'layer_num', 'total_layer_num', 'mc_remaining_time', 'subtask_name', 'gcode_file', 'print_error','device','ams','lights_report','spd_lvl','spd_mag','cooling_fan_speed','big_fan1_speed','big_fan2_speed','heatbreak_fan_speed','hms','wifi_signal','vir_slot')):
                return
            with self.lock:
                merge_report(self.report,value)
                self.status = normalize(self.report)
                if self.status.get('print_stats',{}).get('state') in ('printing','paused'):self.homed=''
                self.status['toolhead']={'homed_axes':self.homed}
                self.last_report = time.monotonic()
                self.publish()
        except (ValueError, TypeError, AttributeError):
            return

    def command(self, data):
        with self.lock:
            if self.error or not self.client or not self.client.is_connected(): raise ValueError('Bambu printer is not connected')
            section,body=control_message(data,self.status.get('print_stats',{}).get('state'),self.homed)
            if data.get('action')=='start':
                if time.monotonic()-getattr(self,'last_start',-1000)<90:raise ValueError('A print start was already sent. Check printer status before retrying.')
                self.last_start=time.monotonic()
            if len(self.pending)>=8: raise ValueError('Wait for the previous printer command')
            self.sequence+=1;sequence=str(self.sequence)
            body['sequence_id']=sequence
            waiting={'section':section,'command':body['command'],'event':threading.Event(),'result':None}
            self.pending[sequence]=waiting
            client=self.client
        try:
            sent=client.publish('device/'+client.printer_serial+'/request',json.dumps({section:body}),qos=1)
            if sent.rc!=0: raise ValueError('Bambu command could not be sent')
            if not waiting['event'].wait(8):
                raise ValueError('Printer did not confirm the command. It may require LAN Developer Mode. Check the printer before retrying.')
            if waiting['result']!='success':
                raise ValueError('Printer rejected the command. Check the printer screen and third-party LAN command permissions.')
            if data.get('action')=='home':
                with self.lock:
                    axes='xyz' if data.get('axis')=='all' else data['axis'].lower()
                    self.homed=''.join(a for a in 'xyz' if a in self.homed+axes)
                    self.status['toolhead']={'homed_axes':self.homed}
                    self.publish()
            return {'confirmed':True}
        finally:
            with self.lock: self.pending.pop(sequence,None)

    def run(self):
        if mqtt is None:
            self.publish('Bambu MQTT dependency missing. Install requirements.txt and restart Print Center.')
            return
        host = urllib.parse.urlsplit(self.printer['url']).hostname
        while not self.stop.is_set():
            client = None
            try:
                ctx = ssl.create_default_context(cafile=str(pathlib.Path(__file__).with_name('bambu_ca.pem')))
                # Bambu's CA lacks extensions required by Python 3.13's strict flag.
                # Chain/signature/expiry and the printer's serial are still checked.
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
                ctx.check_hostname = False  # Certificate CN is a serial, checked in VerifiedClient.
                client = VerifiedClient(mqtt.CallbackAPIVersion.VERSION2, client_id='print-center-' + self.printer['id'])
                self.client = client
                client.expected_serial = self.printer.get('serial', '')
                client.tls_set_context(ctx)
                client.username_pw_set('bblp', self.printer['access_code'])
                client.connect_timeout = 8
                def connected(c, userdata, flags, reason, properties):
                    if reason.is_failure:
                        self.publish('Bambu rejected login. Check the LAN access code and LAN mode.')
                        c.disconnect()
                        return
                    c.subscribe('device/' + c.printer_serial + '/report', qos=0)
                    c.publish('device/' + c.printer_serial + '/request', json.dumps({'pushing': {'sequence_id': '0', 'command': 'pushall', 'version': 1, 'push_target': 1}}))
                client.on_connect = connected
                client.on_message = self.message
                self.report = {}
                self.homed = ''
                self.last_report = time.monotonic()
                client.connect(host, 8883, keepalive=30)
                while not self.stop.is_set():
                    if client.loop(timeout=1) != mqtt.MQTT_ERR_SUCCESS:
                        raise ConnectionError('MQTT disconnected')
                    if time.monotonic() - self.last_report > 90:
                        raise TimeoutError('No status reports')
                return
            except ssl.SSLError:
                self.publish('Bambu certificate verification failed. Check the IP, serial number, and server clock.')
            except Exception:
                self.publish('Bambu LAN connection unavailable. Check printer power, IP, access code, and LAN mode (MQTT port 8883).')
            finally:
                if client:
                    client.disconnect()
            self.stop.wait(10)
