import unittest
from unittest.mock import patch
from live_status import merge_status, wanted_objects, PrinterFeed

class LiveTests(unittest.TestCase):
 def test_partial_box_update_preserves_materials_and_settings(self):
  status={'multi_color_controller':{'config':{'enable_box':1,'vendor_slot0':0,'slot1':1},'slots':{'materials':{'slot0':{'color':'#ffffff','filament':{'type':'PLA'}},'slot1':{'color':'#000000'}},'last_loaded':'slot0'}}}
  delta={'multi_color_controller':{'config':{'slot1':3},'slots':{'materials':{'slot0':{'color':'#ff0000'}}}}}
  merge_status(status,delta)
  box=status['multi_color_controller']
  self.assertEqual(box['config'],{'enable_box':1,'vendor_slot0':0,'slot1':3})
  self.assertEqual(box['slots']['materials']['slot0'],{'color':'#ff0000','filament':{'type':'PLA'}})
  self.assertEqual(box['slots']['materials']['slot1']['color'],'#000000')
  self.assertEqual(box['slots']['last_loaded'],'slot0')
  delta['multi_color_controller']['slots']['materials']['slot0']['color']='changed'
  self.assertEqual(box['slots']['materials']['slot0']['color'],'#ff0000')
 def test_explicit_null_and_lists_replace_previous_values(self):
  status={'object':{'nested':{'old':1},'items':[1,2]}}
  merge_status(status,{'object':{'nested':None,'items':[]}})
  self.assertEqual(status,{'object':{'nested':None,'items':[]}})
  merge_status(status,{'object':{'nested':{'new':2}}})
  self.assertEqual(status['object']['nested'],{'new':2})
 def test_delta_preserves_unchanged_fields_and_is_detached(self):
  status={'extruder':{'temperature':30,'target':200},'print_stats':{'state':'printing','info':{'total_layer':20}}}
  delta={'extruder':{'temperature':31},'print_stats':{'filament_used':100}}
  merge_status(status,delta)
  delta['extruder']['temperature']=99
  self.assertEqual(status['extruder'],{'temperature':31,'target':200})
  self.assertEqual(status['print_stats']['state'],'printing')
 def test_snapshot_does_not_mutate_shared_printer_state(self):
  with patch('live_status.threading.Thread'):
   feed=PrinterFeed({'id':'one'},None,None,None)
  feed.publish({'extruder':{'temperature':30}},mode='websocket')
  version,state,config,error,mode=feed.snapshot()
  state['extruder']['temperature']=90
  self.assertEqual(feed.snapshot()[1]['extruder']['temperature'],30)
  self.assertEqual(mode,'websocket')
  feed.publish(error='Disconnected',mode='offline')
  self.assertEqual(feed.snapshot()[3],'Disconnected')
 def test_subscription_avoids_configuration_and_unrelated_objects(self):
  query=wanted_objects(['extruder','extruder3','fan_generic cooling','print_stats','configfile','gcode_macro SECRET'])
  self.assertEqual(set(query),{'extruder','extruder3','fan_generic cooling','print_stats'})
 def test_http_fallback_when_websocket_unavailable(self):
  def api(path):
   if path == '/printer/objects/list': return {'objects':['extruder','print_stats']}
   return {'status':{'extruder':{'temperature':35},'print_stats':{'state':'standby'}}}
  with patch('live_status.threading.Thread'):
   feed=PrinterFeed({'id':'one','url':'http://192.168.1.2:7125'},api,lambda:{},lambda **kwargs:None)
  with patch('live_status.websocket',None),patch.object(feed.stop,'wait',side_effect=lambda timeout:feed.stop.set()):
   feed.run()
  self.assertEqual(feed.snapshot()[4],'polling')
  self.assertEqual(feed.snapshot()[1]['extruder']['temperature'],35)
  self.assertIsNone(feed.snapshot()[3])
if __name__=='__main__':unittest.main()
