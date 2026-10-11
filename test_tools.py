import unittest
import app

class ToolTests(unittest.TestCase):
 def setUp(self):
  self.s={'toolhead':{'homed_axes':'xyz','position':[10,10,10,0],'axis_minimum':[0,0,0],'axis_maximum':[100,100,100],'extruder':'extruder'},'gcode_move':{},'extruder':{'temperature':200,'pressure_advance':.05,'smooth_time':.03},'print_stats':{'state':'standby'},'fan_generic cooling_fan':{'speed':0},'output_pin caselight':{'value':0},'output_pin e0_heat_sw':{'value':1}}
  self.c={'extruder':{'min_extrude_temp':170},'output_pin caselight':{'pwm':False},'output_pin e0_heat_sw':{'pwm':False}}
 def runscript(self,**d):return app.tool_script(d,self.s,self.c)
 def test_motion_bounds_homing_and_active_print(self):
  self.assertIn('G1 X1 F3000',self.runscript(action='jog',axis='X',amount=1))
  for d in [{'action':'jog','axis':'X','amount':-11},{'action':'home','axis':'X\nRESTART'},{'action':'jog','axis':'X','amount':float('nan')}]:
   with self.assertRaises(ValueError):self.runscript(**d)
  self.s['toolhead']['homed_axes']=''
  with self.assertRaises(ValueError):self.runscript(action='jog',axis='X',amount=1)
  self.s['print_stats']['state']='printing'
  with self.assertRaises(ValueError):self.runscript(action='home',axis='all')
 def test_cold_extrusion_and_restoration(self):
  self.assertIn('RESTORE_GCODE_STATE',self.runscript(action='extrude',length=10,speed=5))
  self.s['extruder']['temperature']=100
  with self.assertRaises(ValueError):self.runscript(action='extrude',length=10,speed=5)
 def test_fans_outputs_and_tuning(self):
  self.assertEqual(self.runscript(action='output',id='fan_generic cooling_fan',value=.5),'SET_FAN_SPEED FAN=cooling_fan SPEED=0.5')
  self.assertEqual(self.runscript(action='output',id='output_pin caselight',value=1),'SET_PIN PIN=caselight VALUE=1')
  with self.assertRaises(ValueError):self.runscript(action='output',id='output_pin e0_heat_sw',value=0)
  with self.assertRaises(ValueError):self.runscript(action='tune',parameter='speed',value=201)
  self.assertEqual(self.runscript(action='tune',parameter='pressure_advance',value=.05),'SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.05')
 def test_snapmaker_caselight(self):
  self.s['led cavity_led']={'color_data':[[0,0,0,1]]}
  self.c['led cavity_led']={'white_pin':'PA10'}
  row=next(r for r in app.control_rows(self.s,self.c) if r['id']=='led cavity_led')
  self.assertEqual((row['name'],row['value'],row['binary']),('Caselight',1,True))
  self.assertEqual(self.runscript(action='output',id='led cavity_led',value=0),'SET_LED LED=cavity_led WHITE=0 SYNC=0 TRANSMIT=1')
  with self.assertRaises(ValueError):self.runscript(action='output',id='led cavity_led',value=.5)
  del self.c['led cavity_led']['white_pin']
  with self.assertRaises(ValueError):self.runscript(action='output',id='led cavity_led',value=1)
if __name__=='__main__':unittest.main()
