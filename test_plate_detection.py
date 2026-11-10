import io
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from PIL import Image, ImageDraw, ImageChops
from plate_detection import PlateDetection, classify, settings, model_assessment, moved, motion_frame

def sample(dark=False):
    image=Image.new('RGB',(640,480),'black' if dark else '#999999')
    if not dark:
        draw=ImageDraw.Draw(image)
        draw.rectangle((80,100,560,400),fill='#444444',outline='white',width=12)
    output=io.BytesIO();image.save(output,format='JPEG');return output.getvalue()

class PlateDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.printer={'id':'one','url':'http://192.168.1.10:7125','profile':'qidi-q2'}
        self.infer=Mock(return_value={'plate_present':'present','surface':'clear','visibility':'good','evidence':'Plate edges visible; no objects seen.'})
        self.detector=PlateDetection(self.temp.name,self.infer)
        self.detector.configure(self.printer,{'enabled':True,'roi':[0,0,1,1]})

    def calibrated(self,label='clear'):
        self.detector.reference(self.printer,label,sample(),True)

    def run_check(self,frame=None,state=None):
        self.detector.start(self.printer,lambda:sample() if frame is None else frame,lambda:{'state':state or 'standby'})
        for _ in range(200):
            view=self.detector.view(self.printer)
            if view['job']['state']!='running':return view
            time.sleep(.01)
        self.fail('Worker did not finish')

    def test_requires_calibration_and_physical_confirmation(self):
        with self.assertRaises(ValueError):self.detector.start(self.printer,sample,lambda:{'state':'standby'})
        with self.assertRaises(ValueError):self.detector.reference(self.printer,'clear',sample(),False)
        self.calibrated()
        self.assertEqual(self.run_check()['result']['status'],'clear')
        self.assertEqual(self.infer.call_args.args[1],['clear'])

    def test_missing_needs_missing_reference_and_poor_visibility_is_unknown(self):
        value={'plate_present':'absent','surface':'uncertain','visibility':'good','evidence':'No plate seen'}
        self.assertEqual(classify(value,{'clear':{}})['status'],'uncertain')
        self.assertEqual(classify(value,{'clear':{},'missing':{}})['status'],'missing')
        value.update(plate_present='present',surface='clear',visibility='poor')
        self.assertEqual(classify(value,{'clear':{}})['status'],'uncertain')

    def test_dark_frame_and_failed_model_never_leave_clear_result(self):
        self.calibrated()
        self.assertEqual(self.run_check()['result']['status'],'clear')
        self.assertEqual(self.run_check(sample(True))['result']['status'],'uncertain')
        self.infer.side_effect=TimeoutError('Model timed out')
        self.assertEqual(self.run_check()['result']['status'],'uncertain')
        self.assertEqual(self.detector.view(self.printer)['job']['state'],'error')

    def test_roi_or_camera_change_requires_new_references(self):
        self.calibrated()
        self.detector.configure(self.printer,{'enabled':True,'roi':[.1,.1,.8,.8]})
        self.assertEqual(self.detector.view(self.printer)['references'],{})
        other={**self.printer,'id':'two'}
        self.assertEqual(self.detector.view(other)['references'],{})
        with self.assertRaises(ValueError):settings({'roi':[.9,0,.5,1]})
        with self.assertRaises(ValueError):self.detector.folder('../escape')

    def test_active_or_unknown_printer_cannot_be_checked(self):
        self.calibrated()
        for state in ('printing','paused','unknown'):
            view=self.run_check(state=state)
            self.assertEqual(view['result']['status'],'uncertain')
        self.infer.assert_not_called()

    def test_worker_is_serialized_and_results_expire(self):
        self.calibrated()
        self.detector.worker.acquire()
        with self.assertRaises(ValueError):self.detector.start(self.printer,sample,lambda:{'state':'standby'})
        self.detector.worker.release()
        self.run_check()
        result=self.detector.read('one','result.json',{})
        result['captured_at']=time.time()-181
        self.detector.write('one','result.json',result)
        self.assertTrue(self.detector.view(self.printer)['result']['stale'])

    def test_malformed_model_output_is_rejected(self):
        with self.assertRaises(ValueError):classify({'plate_present':'yes'}, {'clear':{}})

    def test_model_response_compatibility_accepts_only_complete_json(self):
        value=self.infer.return_value
        self.assertEqual(model_assessment({'done':True,'message':{'content':json.dumps(value)}}),value)
        self.assertEqual(model_assessment({'done':True,'message':{'content':'','thinking':json.dumps(value)}}),value)
        with self.assertRaises(ValueError):model_assessment({'done':True,'message':{'thinking':'Reasoning text '+json.dumps(value)}})
        with self.assertRaises(ValueError):model_assessment({'done':True,'done_reason':'length','message':{'content':json.dumps(value)}})

class PlateAPITests(unittest.TestCase):
    def handler(self,path,body=b''):
        import app
        h=object.__new__(app.Handler)
        h.path=path
        h.headers={'X-Control-Token':app.TOKEN,'Content-Length':str(len(body))}
        h.rfile=io.BytesIO(body)
        h.host_ok=lambda:True
        h.authenticated=lambda:True
        h.origin_ok=lambda:True
        h.reply=lambda value,status=200,**kw:setattr(h,'result',(status,value))
        return h

    def test_enabled_detector_requires_explicit_confirmation_before_any_command(self):
        import app
        printer={'id':'one','profile':'qidi-q2','url':'http://192.168.1.1:7125'}
        for confirmation in (False,None,'true',1):
            body=json.dumps({'action':'start','printer':'one','filename':'part.gcode','plate_confirmed':confirmation}).encode()
            h=self.handler('/api/control',body)
            with patch.object(app,'selected',return_value=printer),patch.object(app.PLATES,'config',return_value={'enabled':True}),patch.object(app,'api') as api:
                h.do_POST()
            self.assertEqual(h.result[0],400)
            self.assertIn('Confirm',h.result[1]['error'])
            api.assert_not_called()

    def test_bambu_can_read_detection_status_without_inference(self):
        import app
        printer={'id':'one','profile':'bambu-x2d','url':'mqtts://192.168.1.1:8883'}
        h=self.handler('/api/plate-status?printer=one')
        with patch.object(app,'selected',return_value=printer),patch.object(app.PLATES,'view',return_value={'model':'qwen3-vl:2b'}) as view:
            h.do_GET()
        self.assertEqual(h.result[0],200)
        view.assert_called_once_with(printer)

class PlateScheduleTests(unittest.TestCase):
    calibrated=PlateDetectionTests.calibrated
    run_check=PlateDetectionTests.run_check
    def setUp(self):
        PlateDetectionTests.setUp(self)
        self.detector.configure(self.printer,{'enabled':True,'automatic':True,'roi':[0,0,1,1]})
        self.calibrated()

    def seed(self,status='occupied',finished=1000):
        config=self.detector.config('one')
        self.detector.write('one','result.json',{'key':self.detector.key(self.printer,config),'status':status,'captured_at':finished,'finished_at':finished,'evidence':'test'})
        self.detector.write('one','schedule.json',{'key':self.detector.key(self.printer,config),'ready':False,'last_print_state':'standby'})

    def tick(self,now,snapshot,state=None):
        return self.detector.automatic_tick(self.printer,state or {'state':'standby'},snapshot,lambda:{'state':'standby'},now=now)

    def test_ready_stops_all_automatic_camera_and_ai_checks_across_restart(self):
        self.assertEqual(self.run_check()['result']['status'],'clear')
        snapshot=Mock(return_value=sample())
        self.assertEqual(self.tick(time.time()+20000,snapshot),'ready')
        snapshot.assert_not_called()

    def test_completed_status_does_not_rearm_the_same_finished_job_forever(self):
        self.run_check(state='complete')
        snapshot=Mock(return_value=sample())
        self.assertEqual(self.tick(time.time()+20000,snapshot,{'state':'complete'}),'ready')
        snapshot.assert_not_called()
        restored=PlateDetection(self.temp.name,self.infer)
        self.assertTrue(restored.view(self.printer)['monitoring']['ready'])
        self.assertEqual(restored.automatic_tick(self.printer,{'state':'standby'},snapshot,lambda:{'state':'standby'}),'ready')
        snapshot.assert_not_called()

    def test_occupied_missing_and_uncertain_recheck_at_ten_minutes(self):
        for status in ('occupied','missing','uncertain'):
            self.detector.motion.clear()
            self.seed(status)
            snapshot=Mock(return_value=sample())
            with patch.object(self.detector,'start') as start:
                self.assertEqual(self.tick(1000,snapshot),'waiting')
                self.assertEqual(self.tick(1590,snapshot),'waiting')
                start.assert_not_called()
                self.assertEqual(self.tick(1600,snapshot),'started')
                start.assert_called_once()

    def test_movement_runs_ai_only_after_settling(self):
        self.seed()
        image=Image.open(io.BytesIO(sample())).convert('RGB')
        ImageDraw.Draw(image).rectangle((200,180,420,380),fill='white')
        out=io.BytesIO();image.save(out,format='JPEG')
        snapshot=Mock(side_effect=[sample(),out.getvalue(),out.getvalue(),out.getvalue()])
        with patch.object(self.detector,'start') as start:
            self.assertEqual(self.tick(1010,snapshot),'waiting')
            self.assertEqual(self.tick(1020,snapshot),'movement')
            self.assertEqual(self.tick(1030,snapshot),'settling')
            start.assert_not_called()
            self.assertEqual(self.tick(1040,snapshot),'started')
            start.assert_called_once()

    def test_new_print_rearms_and_completion_checks_immediately(self):
        self.run_check()
        snapshot=Mock(return_value=sample())
        self.assertEqual(self.tick(time.time(),snapshot,{'state':'printing','filename':'new.gcode'}),'printing')
        snapshot.assert_not_called()
        self.assertFalse(self.detector.view(self.printer)['monitoring']['ready'])
        with patch.object(self.detector,'start') as start:
            self.assertEqual(self.tick(time.time(),snapshot,{'state':'complete','filename':'new.gcode','print_duration':100}),'started')
            start.assert_called_once()

    def test_completed_job_changed_while_server_was_offline_rearms(self):
        self.run_check()
        with patch.object(self.detector,'start') as start:
            self.assertEqual(self.tick(time.time(),Mock(return_value=sample()),{'state':'complete','filename':'different.gcode','print_duration':180}),'started')
            start.assert_called_once()

    def test_uniform_exposure_change_is_not_movement(self):
        image=Image.new('RGB',(640,480),(20,20,20))
        source=Image.open(io.BytesIO(sample())).convert('RGB')
        out=io.BytesIO();ImageChops.add(source,image).save(out,format='JPEG')
        self.assertFalse(moved(motion_frame(sample(),[0,0,1,1]),motion_frame(out.getvalue(),[0,0,1,1])))

if __name__=='__main__':unittest.main()
