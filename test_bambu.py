import json
import ssl
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import app
from bambu import BambuFeed, certificate_serial, normalize, validate_credentials, control_message, merge_report


class BambuTests(unittest.TestCase):
    def test_dual_nozzle_chamber_ams_and_speed(self):
        s=normalize({'spd_mag':100,'device':{'ctc':{'info':{'temp':36}},'extruder':{'info':[{'id':0,'temp':(220<<16)|38},{'id':1,'temp':37}]}},
                     'ams':{'ams':[{'id':'0','humidity_raw':'66','temp':'37.2','tray':[{'id':'0','tray_type':'PETG','tray_color':'898989FF','remain':-1}]}]}})
        self.assertEqual(s['temperature_sensor nozzle_1'],{'temperature':38,'target':220})
        self.assertEqual(s['temperature_sensor nozzle_2']['temperature'],37)
        self.assertEqual(s['temperature_sensor chamber']['temperature'],36)
        self.assertEqual(s['bambu']['ams'][0]['slots'][0]['color'],'#898989')
        self.assertEqual(s['bambu']['ams'][0]['humidity'],66)
        self.assertEqual(s['gcode_move']['speed_factor'],1)

    def test_nested_delta_retains_other_nozzle_and_slots(self):
        value={'device':{'extruder':{'info':[{'id':0,'temp':40},{'id':1,'temp':41}]}}}
        merge_report(value,{'device':{'extruder':{'info':[{'id':1,'temp':42}]}}})
        self.assertEqual(value['device']['extruder']['info'],[{'id':0,'temp':40},{'id':1,'temp':42}])

    def test_control_allowlist_and_limits(self):
        self.assertEqual(control_message({'action':'pause'},'printing')[1]['command'],'pause')
        self.assertEqual(control_message({'action':'cancel'},'paused')[1]['command'],'stop')
        self.assertEqual(control_message({'action':'heat','heater':'heater_bed','target':60},'standby')[1]['param'],'M140 S60\n')
        self.assertEqual(control_message({'action':'bambu_light','enabled':True},'standby')[0],'system')
        for data,state in [({'action':'pause'},'standby'),({'action':'resume'},'printing'),({'action':'home'},'standby'),
                           ({'action':'heat','heater':'extruder','target':301},'standby'),
                           ({'action':'bambu_speed','level':9},'standby'),
                           ({'action':'bambu_fan','fan':'cooling_fan_speed','percent':101},'standby')]:
            with self.assertRaises(ValueError): control_message(data,state)

    def test_command_requires_matching_success_ack(self):
        with patch('bambu.threading.Thread'):
            feed=BambuFeed({'id':'test','url':'mqtts://192.168.1.5:8883','access_code':'ABC12345'})
        feed.error=None
        client=SimpleNamespace(printer_serial='ABC123456789',is_connected=lambda:True)
        feed.client=client
        def publish(topic,payload,qos):
            section,body=next(iter(json.loads(payload).items()))
            ack={section:{'command':body['command'],'sequence_id':body['sequence_id'],'result':'success'}}
            feed.message(client,None,SimpleNamespace(topic='device/ABC123456789/report',payload=json.dumps(ack).encode()))
            return SimpleNamespace(rc=0)
        client.publish=publish
        self.assertEqual(feed.command({'action':'bambu_light','enabled':True}),{'confirmed':True})
        self.assertEqual(feed.pending,{})

    def test_progress_and_temperature_mapping_does_not_invent_elapsed_or_filament(self):
        s = normalize({'gcode_state':'RUNNING','mc_percent':42,'layer_num':12,'total_layer_num':50,
                       'nozzle_temper':212.5,'nozzle_target_temper':220,'bed_temper':60,
                       'mc_remaining_time':8,'subtask_name':'part.3mf'})
        self.assertEqual(s['print_stats']['state'],'printing')
        self.assertEqual(s['virtual_sdcard']['progress'],.42)
        self.assertEqual(s['print_stats']['remaining_time'],480)
        self.assertEqual(s['print_stats']['info']['current_layer'],12)
        self.assertNotIn('filament_used',s['print_stats'])
        self.assertNotIn('print_duration',s['print_stats'])
        rows = app.temperature_rows(s,{})
        self.assertTrue(all(not row['editable'] for row in rows))
        self.assertEqual(next(r for r in rows if r['id']=='extruder')['target'],220)

    def test_missing_values_and_print_error(self):
        self.assertNotIn('extruder',normalize({}))
        self.assertEqual(normalize({'gcode_state':'PAUSE'})['print_stats']['state'],'paused')
        self.assertEqual(normalize({'gcode_state':'RUNNING','print_error':5})['print_stats']['state'],'error')

    def test_credentials_retained_and_never_returned(self):
        value = validate_credentials({'access_code':'','serial':''},{'access_code':'ABC12345'})
        self.assertEqual(value['access_code'],'ABC12345')
        public = app.public_printer({'profile':'bambu-x2d',**value})
        self.assertTrue(public['has_access_code'])
        self.assertNotIn('access_code',public)
        self.assertEqual(app.validate_printer_profile('bambu-x2d'),'bambu-x2d')
        for code in ('','a','1234567\n','123456789','a'*513):
            if code=='1234567\n': continue  # Surrounding whitespace is intentionally trimmed.
            with self.assertRaises(ValueError): validate_credentials({'access_code':code},{})
        with self.assertRaises(ValueError): validate_credentials({'access_code':'ABC12345','serial':'../bad'},{})

    def test_certificate_identity_checked_before_authentication(self):
        cert = {'subject':((('commonName','ABC123456789'),),)}
        self.assertEqual(certificate_serial(cert),'ABC123456789')
        self.assertEqual(certificate_serial(cert,'ABC123456789'),'ABC123456789')
        with self.assertRaises(ssl.CertificateError): certificate_serial(cert,'DIFFERENT123')
        with self.assertRaises(ssl.CertificateError): certificate_serial({})

    def test_delta_reports_merge_and_unrelated_topics_ignored(self):
        with patch('bambu.threading.Thread'):
            feed=BambuFeed({'id':'test','url':'mqtts://192.168.1.5:8883','access_code':'ABC12345'})
        client=SimpleNamespace(printer_serial='ABC123456789')
        def send(topic,report):
            feed.message(client,None,SimpleNamespace(topic=topic,payload=json.dumps({'print':report}).encode()))
        send('device/OTHER/report',{'gcode_state':'RUNNING'})
        self.assertEqual(feed.version,0)
        send('device/ABC123456789/report',{'gcode_state':'RUNNING','nozzle_temper':200})
        send('device/ABC123456789/report',{'layer_num':3})
        _,s,_,error,mode=feed.snapshot()
        self.assertIsNone(error)
        self.assertEqual(mode,'bambu-mqtt')
        self.assertEqual(s['extruder']['temperature'],200)
        self.assertEqual(s['print_stats']['info']['current_layer'],3)

    def test_feed_switch_uses_bambu_adapter_and_credentials_in_cache_identity(self):
        p={'id':'test','profile':'bambu-x2d','url':'mqtts://192.168.1.5:8883','access_code':'ABC12345'}
        with patch.object(app,'FEEDS',{}), patch.object(app,'BambuFeed') as adapter:
            app.feed_for(p)
            adapter.assert_called_once_with(p)
            p2={**p,'access_code':'NEW12345'}
            app.feed_for(p2)
            self.assertEqual(adapter.call_count,2)
            adapter.return_value.close.assert_called_once()


if __name__=='__main__': unittest.main()
