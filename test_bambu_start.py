import unittest
from bambu import control_message

class BambuStartTests(unittest.TestCase):
    def data(self):
        return dict(action='start', filename='cache/model with spaces.3mf', plate_confirmed=True, use_ams=True,
                    bambu_plate={'param':'Metadata/plate_2.gcode','md5':'a'*32,'filaments':[{'id':1,'type':'PLA'},{'id':3,'type':'PETG'}]}, ams_mapping={'1':0,'3':5})
    def test_payload_preserves_filament_ids_and_firmware_mapping(self):
        section,b=control_message(self.data(),'complete')
        self.assertEqual(section,'print')
        self.assertEqual(b['command'],'project_file')
        self.assertEqual(b['url'],'ftp:///cache/model%20with%20spaces.3mf')
        self.assertEqual(b['param'],'Metadata/plate_2.gcode')
        self.assertEqual(b['ams_mapping'],[0,-1,5])
        self.assertEqual(b['ams_mapping2'],[{'ams_id':0,'slot_id':0},{'ams_id':255,'slot_id':255},{'ams_id':1,'slot_id':1}])
        self.assertIs(type(b['bed_leveling']),int)
    def test_rejects_active_unknown_unconfirmed_and_unmapped(self):
        for state in ('printing','paused','error','unknown',None):
            with self.assertRaises(ValueError):control_message(self.data(),state)
        for change in ({'plate_confirmed':False},{'use_ams':False},{'ams_mapping':{'1':0}}, {'filename':'../model.3mf'}, {'bambu_plate':{'param':'../../bad','md5':'a'*32}}):
            data=self.data();data.update(change)
            with self.assertRaises(ValueError):control_message(data,'standby')

if __name__=='__main__':unittest.main()
