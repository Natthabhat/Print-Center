"""Snapmaker U1 filament slots: colours from the printer's filament profiles, plus load and unload.

Load and unload send the same AUTO_FEEDING_BATCH sequence the U1 touchscreen uses, so the printer
heats, cuts, purges and restores temperatures exactly as it does when run from the screen.
"""
import re

SLOTS = 4
OBJECTS = ('print_task_config', 'machine_state_manager', 'filament_feed left', 'filament_feed right', 'print_stats', 'webhooks')
# Feeder states that mean filament is parked in the feeder, ready to load into the toolhead.
PARKED = ('preload_finish', 'unload_finish')
STATE_LABELS = {
    'load_finish': 'Loaded', 'unload_finish': 'Unloaded · ready to load', 'preload_finish': 'In feeder · ready to load',
    'wait_insert': 'Empty · insert filament', 'preload_prepare': 'Detecting filament…', 'preload_feeding': 'Feeding to buffer…',
    'load_picking': 'Loading · picking toolhead…', 'load_feeding': 'Loading · feeding…', 'load_heating': 'Loading · heating…',
    'load_extruding': 'Loading · extruding…', 'load_flushing': 'Loading · purging…',
    'unload_homing': 'Unloading · homing…', 'unload_picking': 'Unloading · picking toolhead…', 'unload_heating': 'Unloading · heating…',
    'unload_heat_finish': 'Unloading · heated', 'unload_doing': 'Unloading · retracting…',
}


def feed_channel(status, index):
    for side in ('filament_feed left', 'filament_feed right'):
        channel = (status.get(side) or {}).get('extruder%d' % index)
        if isinstance(channel, dict): return channel
    return {}


def _item(values, index, default=None):
    return values[index] if isinstance(values, list) and index < len(values) else default


def machine_busy(status):
    machine = status.get('machine_state_manager') or {}
    return bool(machine.get('main_state') or machine.get('action_code'))


def printer_busy(status):
    return (status.get('webhooks') or {}).get('state', 'ready') != 'ready' or \
        (status.get('print_stats') or {}).get('state') in ('printing', 'paused') or machine_busy(status)


def slots(status):
    config = status.get('print_task_config')
    if not isinstance(config, dict) and not any(k.startswith('filament_feed ') for k in status): return []
    config = config or {}
    busy = printer_busy(status)
    rows = []
    for index in range(SLOTS):
        channel = feed_channel(status, index)
        rgba = _item(config.get('filament_color_rgba'), index)
        color = '#' + rgba[:6].upper() if isinstance(rgba, str) and re.fullmatch(r'[0-9A-Fa-f]{6,8}', rgba) else None
        exists = _item(config.get('filament_exist'), index, False) is True
        detected = channel.get('filament_detected') is True
        state = str(channel.get('channel_state') or '')
        error = channel.get('channel_error')
        ready = not busy and channel.get('module_exist') is True
        rows.append({
            'index': index, 'name': 'T%d' % (index + 1), 'color': color if exists else None,
            'material': (_item(config.get('filament_type'), index) or '') if exists else '',
            'sub_type': (_item(config.get('filament_sub_type'), index) or '') if exists else '',
            'vendor': (_item(config.get('filament_vendor'), index) or '') if exists else '',
            'detected': detected, 'module': channel.get('module_exist') is True, 'state': state,
            'label': STATE_LABELS.get(state, state.replace('_', ' ').capitalize() or 'Not reported'),
            'error': error if error not in (None, '', 'ok') else None,
            'can_color': not busy,
            'can_load': ready and detected and state in PARKED,
            # Unknown states are allowed so a slot left in an unexpected state can still be cleared.
            'can_unload': ready and (state == 'load_finish' or bool(state) and state not in STATE_LABELS),
        })
    return rows


def scripts(mode, index, status):
    """G-code steps for one load/unload, matching the U1 touchscreen. Raises ValueError when unsafe."""
    if mode not in ('load', 'unload'): raise ValueError('Choose load or unload')
    if type(index) is not int or not 0 <= index < SLOTS: raise ValueError('Choose extruder T1 to T4')
    if (status.get('webhooks') or {}).get('state', 'ready') != 'ready': raise ValueError('Klipper is not ready')
    if (status.get('print_stats') or {}).get('state') in ('printing', 'paused'):
        raise ValueError('Wait until the print is finished before changing filament')
    if machine_busy(status): raise ValueError('The printer is busy with another task. Wait until it finishes.')
    row = slots(status)[index]
    if not row['module']: raise ValueError('No filament feeder found for %s' % row['name'])
    if mode == 'load' and not row['can_load']: raise ValueError('Insert filament into the %s feeder first' % row['name'])
    if mode == 'unload' and not row['can_unload']: raise ValueError('%s has no filament loaded' % row['name'])
    if mode == 'load':
        steps = ['AUTO_FEEDING_BATCH ACTION=DOING EXTRUDER=%d LOAD=1' % index]
    else:
        steps = ['AUTO_FEEDING_BATCH ACTION=DOING STAGE=prepare EXTRUDER=%d UNLOAD=1' % index,
                 'AUTO_FEEDING_BATCH ACTION=DOING STAGE=doing EXTRUDER=%d UNLOAD=1' % index]
    return ['AUTO_FEEDING_BATCH ACTION=START', *steps]


def color_script(index, color, status):
    """Set a slot's filament colour the way the U1 touchscreen does."""
    if type(index) is not int or not 0 <= index < SLOTS: raise ValueError('Choose extruder T1 to T4')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', color): raise ValueError('Choose a colour like #D81B60')
    if printer_busy(status): raise ValueError('Wait until the printer is idle before changing the filament colour')
    return 'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_COLOR_RGBA=%sFF' % (index, color[1:].upper())


STOP = 'AUTO_FEEDING_BATCH ACTION=STOP'
