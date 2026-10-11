import unittest
from unittest.mock import patch
from email.utils import formatdate
import app
from camera_monitor import fresh_jpeg


class CameraTests(unittest.TestCase):
    def test_stale_or_truncated_frames_are_rejected(self):
        jpeg = b'\xff\xd8\xff' + b'x' * 100 + b'\xff\xd9'
        self.assertTrue(fresh_jpeg(jpeg, formatdate(100, usegmt=True), 105))
        self.assertFalse(fresh_jpeg(jpeg, formatdate(100, usegmt=True), 200))
        self.assertFalse(fresh_jpeg(jpeg[:-2], formatdate(100, usegmt=True), 105))
        self.assertFalse(fresh_jpeg(jpeg, None, 105))

    def test_stock_snapmaker_discovery_preserves_existing_webcams(self):
        app.CURRENT.printer = {'id': 'camera-test', 'url': 'http://192.168.1.2:7125'}
        info = {'components': ['snapmakercloud'], 'registered_directories': ['camera']}
        with patch.object(app, 'CAMERA_CONFIG', {}), patch.object(app, 'api', side_effect=[{'webcams': []}, info]):
            self.assertEqual(app.camera()['service'], 'snapmaker-lan')
        standard = {'enabled': True, 'service': 'mjpegstreamer', 'stream_url': '/webcam'}
        with patch.object(app, 'CAMERA_CONFIG', {}), patch.object(app, 'api', return_value={'webcams': [standard]}) as read:
            self.assertEqual(app.camera(), standard)
            self.assertEqual(read.call_count, 1)


if __name__ == '__main__':
    unittest.main()
