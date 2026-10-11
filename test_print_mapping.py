import unittest
from box_control import plate_filaments, mapping_script

class PrintMappingTests(unittest.TestCase):
    def setUp(self):
        self.f=[{'tool':0,'type':'PETG','color':'#808080','grams':1.9}]
        self.s={'webhooks':{'state':'ready'},'print_stats':{'state':'standby'},'box_extras':{'e_endstop_state':0,'b_endstop_state':1},'box_stepper slot0':{'runout_button':0},'multi_color_controller':{'hardware':{'connected':True,'box_count':1},'slots':{'materials':{'slot0':{'filament':{'type':'PETG'}}}}}}
    def test_actual_plate_filaments_only(self):
        xml='<config><plate><metadata key="index" value="1"/><filament id="2" type="PLA" used_g="2"/></plate><plate><metadata key="index" value="14"/><filament id="1" type="PETG" color="#808080" used_g="1.9"/><filament id="3" type="PETG" used_g="0" used_mm="0"/></plate></config>'
        self.assertEqual(plate_filaments(xml,14),self.f)
        with self.assertRaises(ValueError):plate_filaments(xml,2)
    def test_mapping_to_firmware_variable(self):
        self.assertIn('value_t0 VALUE="\'slot0\'"',mapping_script({'use_box':True,'mapping':{'0':0}},self.s,self.f))
    def test_empty_wrong_material_and_invalid_mapping(self):
        for mapping in ({},{'0':4},{'0':'0'},{'0':True},{'0':0,'1':0}):
            with self.assertRaises(ValueError):mapping_script({'use_box':True,'mapping':mapping},self.s,self.f)
        self.s['multi_color_controller']['slots']['materials']['slot0']['filament']['type']='PLA'
        with self.assertRaises(ValueError):mapping_script({'use_box':True,'mapping':{'0':0}},self.s,self.f)
    def test_external_spool_and_print_guard(self):
        with self.assertRaises(ValueError):mapping_script({'use_box':False},self.s,self.f)
        self.s['box_extras']['e_endstop_state']=1
        self.assertIn('enable_box VALUE=0',mapping_script({'use_box':False},self.s,self.f))
        self.s['print_stats']['state']='printing'
        with self.assertRaises(ValueError):mapping_script({'use_box':False},self.s,self.f)
    def test_no_shared_slot(self):
        with self.assertRaises(ValueError):mapping_script({'use_box':True,'mapping':{'0':0,'1':0}},self.s,self.f+[{'tool':1,'type':'PETG'}])
