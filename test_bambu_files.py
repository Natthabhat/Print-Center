import unittest
from unittest.mock import patch,MagicMock
from bambu_files import file_path, parse_listing, BambuFiles
from bambu import control_message


class BambuFileTests(unittest.TestCase):
    def test_filename_scope(self):
        self.assertEqual(file_path('cache/my file.gcode.3mf'),'/cache/my file.gcode.3mf')
        for name in ('../file.gcode','cache/../../file.3mf','cache\\file.3mf','cache/file.3mf\r\nDELE x','config.json','.hidden.gcode'):
            with self.assertRaises(ValueError):file_path(name)
        with self.assertRaises(ValueError):file_path('cache/.print-center-trash/abc--file.3mf')
        self.assertEqual(file_path('cache/.print-center-trash/abc--file.3mf',trash=True),'/cache/.print-center-trash/abc--file.3mf')

    def test_unix_listing_handles_spaces_and_directories(self):
        rows=parse_listing(['drwxr-xr-x    3 103      107         65536 Oct 20 10:29 cache',
                            '-rwxr-xr-x    1 103      107         12345 Oct 19 15:34 my model.gcode.3mf'],'/')
        self.assertTrue(rows[0]['directory'])
        self.assertEqual(rows[1]['path'],'my model.gcode.3mf')
        self.assertEqual(rows[1]['size'],12345)

    def test_movement_guard_and_bounded_commands(self):
        self.assertEqual(control_message({'action':'home','axis':'all'},'standby')[1]['param'],'G28\n')
        self.assertEqual(control_message({'action':'jog','axis':'X','amount':1},'standby','xyz')[1]['param'],'G91\nG1 X1 F1800\nG90\n')
        self.assertEqual(control_message({'action':'jog','axis':'Z','amount':-.1},'complete','xyz')[1]['param'],'G91\nG1 Z-0.1 F300\nG90\n')
        for data,state,homed in [({'action':'home','axis':'all'},'printing',''),
                                  ({'action':'jog','axis':'X','amount':1},'paused','xyz'),
                                  ({'action':'jog','axis':'Y','amount':1},'standby','x'),
                                  ({'action':'jog','axis':'Z','amount':10},'standby','xyz'),
                                  ({'action':'jog','axis':'X','amount':float('nan')},'standby','xyz')]:
            with self.assertRaises(ValueError):control_message(data,state,homed)

    def test_trash_renames_without_deleting_original_content(self):
        from contextlib import contextmanager
        f=BambuFiles({'id':'test'},'/tmp/cache');ftp=MagicMock()
        @contextmanager
        def connection():yield ftp
        with patch.object(f,'connection',connection):
            result=f.trash('cache/part.3mf')
        ftp.delete.assert_not_called()
        source,target=ftp.rename.call_args.args
        self.assertEqual(source,'/cache/part.3mf')
        self.assertIn('/cache/.print-center-trash/',target)
        self.assertTrue(result['filename'].endswith('--part.3mf'))
