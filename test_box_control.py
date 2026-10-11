import unittest, copy
from box_control import box_script, parse_catalog

class BoxTests(unittest.TestCase):
    def setUp(self):
        self.catalog={'vendors':{'0':'Generic'},'filaments':{'41':{'filament':'PETG'}},'colors':{'1':'#FAFAFA'}}
        self.status={'webhooks':{'state':'ready'},'print_stats':{'state':'standby'},'multi_color_controller':{'hardware':{'connected':True,'box_count':1},'slots':{'last_loaded':'slot16'}},'box_extras':{'b_endstop_state':1},'box_stepper slot0':{'runout_button':0}}
    def test_material_catalog_validation(self):
        data={'mode':'material','slot':0,'vendor':'0','filament':'41','color':'1'}
        self.assertIn('filament_slot0 VALUE=41',box_script(data,self.status,self.catalog))
        data['filament']='41\nM112'
        with self.assertRaises(ValueError): box_script(data,self.status,self.catalog)
    def test_slot_and_busy_guards(self):
        self.assertEqual(box_script({'mode':'load','slot':0},self.status,self.catalog),'E_LOAD slot=0')
        for slot in (4,-1,17,True):
            with self.assertRaises(ValueError): box_script({'mode':'load','slot':slot},self.status,self.catalog)
        self.status['print_stats']['state']='printing'
        with self.assertRaises(ValueError): box_script({'mode':'load','slot':0},self.status,self.catalog)
    def test_unload_requires_loaded_slot(self):
        with self.assertRaises(ValueError): box_script({'mode':'unload','slot':0},self.status,self.catalog)
        self.status['multi_color_controller']['slots']['last_loaded']='slot0'
        self.status['box_extras']['b_endstop_state']=0
        self.assertEqual(box_script({'mode':'unload','slot':0},self.status,self.catalog),'E_UNLOAD slot=0')
    def test_setting_allowlist(self):
        self.assertEqual(box_script({'mode':'setting','parameter':'auto_read_rfid','value':1},self.status,self.catalog),'SAVE_VARIABLE VARIABLE=auto_read_rfid VALUE=1')
        with self.assertRaises(ValueError): box_script({'mode':'setting','parameter':'box_count','value':1},self.status,self.catalog)
    def test_catalog_hash_colors(self):
        c=parse_catalog('[fila1]\nfilament=PLA\n[colordict]\n1=#FAFAFA\n[vendor_list]\n0=Generic')
        self.assertEqual(c['colors']['1'],'#FAFAFA')
