import copy
import threading
import unittest
from unittest.mock import patch
import app
import u1_filament

# Captured from a real U1 at idle with four PETG spools loaded.
STATUS = {
    'webhooks': {'state': 'ready'}, 'print_stats': {'state': 'complete'},
    'machine_state_manager': {'main_state': 0, 'action_code': 0},
    'print_task_config': {'filament_vendor': ['Snapmaker'] * 4, 'filament_type': ['PETG'] * 4, 'filament_sub_type': [''] * 4,
                          'filament_color_rgba': ['000000FF', 'FFFFFFFF', '8C9099FF', 'D81B60FF'], 'filament_exist': [True] * 4},
    'filament_feed left': {'extruder0': {'module_exist': True, 'filament_detected': True, 'channel_state': 'load_finish', 'channel_error': 'ok'},
                           'extruder1': {'module_exist': True, 'filament_detected': True, 'channel_state': 'load_finish', 'channel_error': 'ok'}},
    'filament_feed right': {'extruder2': {'module_exist': True, 'filament_detected': True, 'channel_state': 'preload_finish', 'channel_error': 'ok'},
                            'extruder3': {'module_exist': True, 'filament_detected': False, 'channel_state': 'wait_insert', 'channel_error': 'ok'}},
}


def status(**changes):
    value = copy.deepcopy(STATUS)
    for key, fields in changes.items(): value[key].update(fields)
    return value


class SlotTests(unittest.TestCase):
    def test_colours_materials_and_actions(self):
        rows = u1_filament.slots(STATUS)
        self.assertEqual([r['color'] for r in rows], ['#000000', '#FFFFFF', '#8C9099', '#D81B60'])
        self.assertEqual([r['name'] for r in rows], ['T1', 'T2', 'T3', 'T4'])
        self.assertEqual(rows[0]['material'], 'PETG')
        self.assertEqual([(r['can_load'], r['can_unload']) for r in rows], [(False, True), (False, True), (True, False), (False, False)])
        self.assertEqual(rows[3]['label'], 'Empty · insert filament')

    def test_busy_printer_disables_everything(self):
        for busy in (status(print_stats={'state': 'printing'}), status(machine_state_manager={'main_state': 10, 'action_code': 640}),
                     status(webhooks={'state': 'shutdown'})):
            self.assertFalse(any(r['can_load'] or r['can_unload'] for r in u1_filament.slots(busy)))

    def test_other_printers_have_no_slots(self):
        self.assertEqual(u1_filament.slots({'print_stats': {}}), [])

    def test_missing_profile_has_no_colour(self):
        value = copy.deepcopy(STATUS); value['print_task_config']['filament_exist'][1] = False
        self.assertIsNone(u1_filament.slots(value)[1]['color'])


class ScriptTests(unittest.TestCase):
    def test_matches_touchscreen_sequence(self):
        self.assertEqual(u1_filament.scripts('unload', 0, STATUS), [
            'AUTO_FEEDING_BATCH ACTION=START',
            'AUTO_FEEDING_BATCH ACTION=DOING STAGE=prepare EXTRUDER=0 UNLOAD=1',
            'AUTO_FEEDING_BATCH ACTION=DOING STAGE=doing EXTRUDER=0 UNLOAD=1'])
        self.assertEqual(u1_filament.scripts('load', 2, STATUS), ['AUTO_FEEDING_BATCH ACTION=START', 'AUTO_FEEDING_BATCH ACTION=DOING EXTRUDER=2 LOAD=1'])

    def test_rejects_unsafe_requests(self):
        for args in (('load', 0, STATUS), ('unload', 2, STATUS), ('load', 3, STATUS), ('eject', 0, STATUS), ('load', 4, STATUS),
                     ('load', True, STATUS), ('load', '2', STATUS), ('unload', 0, status(print_stats={'state': 'paused'})),
                     ('unload', 0, status(machine_state_manager={'main_state': 10}))):
            with self.assertRaises(ValueError): u1_filament.scripts(*args)


class ActionTests(unittest.TestCase):
    def setUp(self):
        app.U1_FILAMENT_JOBS.clear()
        app.CURRENT.printer = {'id': 'u1', 'url': 'http://192.0.2.1:7125', 'profile': 'snapmaker-u1'}
        self.addCleanup(app.U1_FILAMENT_JOBS.clear)

    def run_job(self, api):
        done = threading.Event()
        feed = type('Feed', (), {'touch': lambda self: done.set()})()
        with patch.object(app, 'api', side_effect=api), patch.object(app, 'feed_for', return_value=feed):
            result = app.start_u1_filament({'mode': 'unload', 'extruder': 0})
            self.assertTrue(done.wait(2))
        return result

    def test_runs_steps_in_background_then_stops(self):
        sent = []
        def api(path, data=None, **kw):
            if path.startswith('/printer/objects/query'): return {'status': STATUS}
            sent.append(data['script'])
        self.assertEqual(self.run_job(api), {'started': True, 'mode': 'unload', 'extruder': 0})
        self.assertEqual(sent[-1], u1_filament.STOP)
        self.assertEqual(len(sent), 4)
        self.assertEqual(app.U1_FILAMENT_JOBS['u1'], {'mode': 'unload', 'extruder': 0, 'running': False, 'error': None})

    def test_klipper_error_is_reported_and_heaters_restored(self):
        sent = []
        def api(path, data=None, **kw):
            if path.startswith('/printer/objects/query'): return {'status': STATUS}
            sent.append(data['script'])
            if 'prepare' in data['script']: raise ValueError('Filament cut failed')
        self.run_job(api)
        self.assertEqual(app.U1_FILAMENT_JOBS['u1']['error'], 'Filament cut failed')
        self.assertEqual(sent[-1], u1_filament.STOP)

    def test_lost_connection_leaves_printer_alone(self):
        sent = []
        def api(path, data=None, **kw):
            if path.startswith('/printer/objects/query'): return {'status': STATUS}
            sent.append(data['script'])
            if 'prepare' in data['script']: raise OSError('timed out')
        self.run_job(api)
        self.assertNotIn(u1_filament.STOP, sent)
        self.assertIn('Lost contact', app.U1_FILAMENT_JOBS['u1']['error'])

    def test_only_one_change_at_a_time_and_only_on_u1(self):
        app.U1_FILAMENT_JOBS['u1'] = {'running': True}
        with patch.object(app, 'api', return_value={'status': STATUS}):
            with self.assertRaisesRegex(ValueError, 'already running'): app.start_u1_filament({'mode': 'unload', 'extruder': 0})
        app.CURRENT.printer = {'id': 'q2', 'url': 'http://192.0.2.2:7125', 'profile': 'qidi-q2'}
        with self.assertRaisesRegex(ValueError, 'only available'): app.start_u1_filament({'mode': 'unload', 'extruder': 0})


if __name__ == '__main__':
    unittest.main()
