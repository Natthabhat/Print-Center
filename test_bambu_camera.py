import hashlib
import unittest
from bambu_camera import rewrite_rtsp, rtsp_packet


class CameraProtocolTests(unittest.TestCase):
    def test_fragmented_rtsp_and_interleaved_video(self):
        data=b'RTSP/1.0 200 OK\r\nContent-Length: 3\r\n\r\nabc'
        self.assertIsNone(rtsp_packet(data[:-1]))
        self.assertEqual(rtsp_packet(data+b'$\x00\x00\x02ab'),(data,b'$\x00\x00\x02ab'))
        self.assertEqual(rtsp_packet(b'$\x00\x00\x02ab'),(b'$\x00\x00\x02ab',b''))
        self.assertIsNone(rtsp_packet(b'$\x00\x00'))

    def test_camera_auth_recomputed_for_remote_uri(self):
        local='rtsp://127.0.0.1:1234';remote='rtsps://192.168.1.5:322';code='TEST1234'
        head=('DESCRIBE '+local+'/streaming/live/1 RTSP/1.0\r\nAuthorization: Digest username="bblp", realm="camera", nonce="abc", uri="'+local+'/streaming/live/1", response="old"\r\n\r\n').encode()
        rewritten=rewrite_rtsp(head,local,remote,code,True)
        md5=lambda s:hashlib.md5(s.encode()).hexdigest()
        expected=md5(md5('bblp:camera:'+code)+':abc:'+md5('DESCRIBE:'+remote+'/streaming/live/1'))
        self.assertIn(('response="'+expected+'"').encode(),rewritten)
        self.assertNotIn(local.encode(),rewritten)
        self.assertNotIn(code.encode(),rewritten)

    def test_response_and_sdp_stay_on_loopback(self):
        local='rtsp://127.0.0.1:1234';remote='rtsps://192.168.1.5:322'
        body=('a=control:'+remote+'/track1\r\n').encode()
        packet=('RTSP/1.0 200 OK\r\nContent-Base: '+remote+'/\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body
        rewritten=rewrite_rtsp(packet,local,remote,'TEST1234',False)
        self.assertNotIn(remote.encode(),rewritten)
        self.assertEqual(rtsp_packet(rewritten),(rewritten,b''))
        self.assertEqual(rewrite_rtsp(b'$\x00\x00\x02ab',local,remote,'TEST1234',False),b'$\x00\x00\x02ab')

    def test_oversized_messages_rejected(self):
        with self.assertRaises(ValueError):rtsp_packet(b'RTSP/1.0 200 OK\r\nContent-Length: 9999999\r\n\r\n')
        with self.assertRaises(ValueError):rtsp_packet(b'a'*65537)
