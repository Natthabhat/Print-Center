"""Qidi box commands, matched to the printer's installed Fluidd client."""
import configparser, re
import xml.etree.ElementTree as ET

def plate_filaments(xml, plate):
    root = ET.fromstring(xml)
    for item in root.findall('plate'):
        values = {v.get('key'):v.get('value') for v in item.findall('metadata')}
        if values.get('index') != str(plate): continue
        rows = []
        for filament in item.findall('filament'):
            if float(filament.get('used_g','0')) <= 0 and float(filament.get('used_mm','0')) <= 0: continue
            tool = int(filament.get('id','0'))-1
            if not 0 <= tool < 16: raise ValueError('Unsupported filament tool index')
            rows.append({'tool':tool,'type':filament.get('type',''),'color':filament.get('color','#888888'),'grams':float(filament.get('used_g','0'))})
        if rows: return sorted(rows,key=lambda r:r['tool'])
    raise ValueError('Could not identify the selected plate’s filaments. Re-export this sliced plate from Qidi Studio.')

def mapping_script(data, status, filaments):
    box = status.get('multi_color_controller',{})
    if not box.get('hardware',{}).get('connected'): raise ValueError('Multimaterial box is disconnected')
    if status.get('webhooks',{}).get('state') != 'ready': raise ValueError('Klipper is not ready')
    if status.get('print_stats',{}).get('state') in ('printing','paused'): raise ValueError('A print job is already active')
    if status.get('box_extras',{}).get('box_button_state') or box.get('operation',{}).get('operate_state'): raise ValueError('Wait for the box operation to finish')
    if type(data.get('use_box')) is not bool: raise ValueError('Select box or external spool')
    if not data['use_box']:
        if len(filaments) != 1: raise ValueError('A multicolor print requires the box')
        extras = status.get('box_extras',{})
        if extras.get('e_endstop_state') != 1 or extras.get('b_endstop_state') == 0: raise ValueError('Load the external spool before starting')
        return 'SAVE_VARIABLE VARIABLE=enable_box VALUE=0'
    mapping = data.get('mapping')
    if not isinstance(mapping,dict) or set(mapping) != {str(f['tool']) for f in filaments}: raise ValueError('Select a box slot for each print color')
    lines = ['SAVE_VARIABLE VARIABLE=enable_box VALUE=1']
    used = set()
    for f in filaments:
        slot = mapping[str(f['tool'])]
        count = min(4,int(box['hardware'].get('box_count',0)))*4
        if type(slot) is not int or not 0 <= slot < count or slot in used: raise ValueError('Select a different connected slot for each color')
        if status.get(f'box_stepper slot{slot}',{}).get('runout_button') != 0: raise ValueError('Selected box slot is empty')
        material = box.get('slots',{}).get('materials',{}).get(f'slot{slot}',{}).get('filament',{})
        if material.get('type','').upper() != f['type'].upper(): raise ValueError('Selected slot material must match '+f['type'])
        used.add(slot)
        lines.append(f'SAVE_VARIABLE VARIABLE=value_t{f["tool"]} VALUE="\'slot{slot}\'"')
    return '\n'.join(lines)

def parse_catalog(text):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    return {
        'filaments': {s[4:]:dict(cfg[s]) for s in cfg.sections() if re.fullmatch(r'fila\d+',s) and cfg[s].get('filament')},
        'colors':dict(cfg['colordict']), 'vendors':dict(cfg['vendor_list'])
    }

def box_script(data, status, catalog):
    box = status.get('multi_color_controller')
    if not box or not box.get('hardware',{}).get('connected'):
        raise ValueError('No connected multimaterial box')
    if status.get('webhooks',{}).get('state') != 'ready':
        raise ValueError('Klipper is not ready')
    if status.get('print_stats',{}).get('state') in ('printing','paused'):
        raise ValueError('Box setup is unavailable during an active print')
    if status.get('box_extras',{}).get('box_button_state') or box.get('operation',{}).get('operate_state'):
        raise ValueError('Wait for the current box operation to finish')
    mode = data.get('mode')
    if mode == 'setting':
        key = data.get('parameter')
        if key not in ('enable_box','auto_read_rfid','auto_init_detect','auto_reload_detect') or type(data.get('value')) is not int or data['value'] not in (0,1):
            raise ValueError('Invalid box setting')
        return f'SAVE_VARIABLE VARIABLE={key} VALUE={data["value"]}'
    slot = data.get('slot')
    count = min(4,max(0,int(box['hardware'].get('box_count',0)))) * 4
    if type(slot) is not int or not (0 <= slot < count or slot == 16):
        raise ValueError('Select a connected box slot or external spool')
    if mode == 'material':
        lines = []
        for key,category in [('vendor','vendors'),('filament','filaments'),('color','colors')]:
            value = str(data.get(key,''))
            if not re.fullmatch(r'\d+',value) or value not in catalog[category]:
                raise ValueError('Select a supported '+key)
            lines.append(f'SAVE_VARIABLE VARIABLE={key}_slot{slot} VALUE={value}')
        return '\n'.join(lines)
    if slot == 16:
        raise ValueError('Select a box slot for this operation')
    if mode == 'rfid': return f'RFID_READ SLOT=slot{slot}'
    if mode not in ('load','unload','eject'): raise ValueError('Unknown box operation')
    sensor = status.get(f'box_stepper slot{slot}',{})
    if sensor.get('runout_button') != 0:
        raise ValueError('This slot has no filament')
    extras = status.get('box_extras',{})
    loaded = box.get('slots',{}).get('last_loaded') == f'slot{slot}'
    if mode in ('unload','eject') and not (loaded and extras.get('b_endstop_state') == 0):
        raise ValueError('This slot is not loaded')
    if mode == 'load' and loaded and extras.get('b_endstop_state') == 0:
        raise ValueError('This slot is already loaded')
    return {'load':'E_LOAD','unload':'E_UNLOAD','eject':'E_BOX'}[mode]+f' slot={slot}'
