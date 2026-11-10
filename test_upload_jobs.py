import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import app
import upload_jobs
from upload_jobs import UploadJobs, UploadBusy


def wait_done(jobs, uid):
    for _ in range(200):
        state = jobs.status(uid)
        if state['state'] != 'saving': return state
        time.sleep(0.01)
    raise AssertionError('save did not finish')


class UploadJobsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = pathlib.Path(folder.name) / 'uploads'
        self.jobs = UploadJobs(self.root, 100)

    def test_chunks_assemble_and_save_in_background(self):
        uid = self.jobs.start('printer', 'part.gcode', 10)['upload']
        self.jobs.chunk(uid, 0, io.BytesIO(b'hello'), 5)
        self.jobs.chunk(uid, 5, io.BytesIO(b'world'), 5)
        seen = {}
        def save(job, source):
            seen.update(job=job['printer'], data=source.read())
            return {'filename': 'part.gcode'}
        self.assertIn(self.jobs.finish(uid, save)['state'], ('saving', 'done'))
        state = wait_done(self.jobs, uid)
        self.assertEqual(state['state'], 'done')
        self.assertEqual(state['result'], {'filename': 'part.gcode'})
        self.assertEqual(seen, {'job': 'printer', 'data': b'helloworld'})
        self.assertFalse(list(self.root.glob('*.part')))

    def test_retried_chunk_overwrites_instead_of_duplicating(self):
        uid = self.jobs.start('p', 'a.gcode', 6)['upload']
        self.jobs.chunk(uid, 0, io.BytesIO(b'abc'), 3)
        self.jobs.chunk(uid, 0, io.BytesIO(b'abc'), 3)
        self.jobs.chunk(uid, 3, io.BytesIO(b'def'), 3)
        self.assertEqual((self.root / (uid + '.part')).read_bytes(), b'abcdef')

    def test_rejects_gaps_oversize_incomplete_and_unknown_uploads(self):
        with self.assertRaises(ValueError): self.jobs.start('p', 'a.gcode', 101)
        with self.assertRaises(ValueError): self.jobs.start('p', 'a.gcode', True)
        uid = self.jobs.start('p', 'a.gcode', 6)['upload']
        with self.assertRaises(ValueError): self.jobs.chunk(uid, 3, io.BytesIO(b'def'), 3)
        with self.assertRaises(ValueError): self.jobs.chunk(uid, 0, io.BytesIO(b'abcdefg'), 7)
        with self.assertRaises(ValueError): self.jobs.chunk(uid, 0, io.BytesIO(b'ab'), 3)
        with self.assertRaises(ValueError): self.jobs.finish(uid, Mock())
        with self.assertRaises(ValueError): self.jobs.status('../../etc/passwd')
        with self.assertRaises(ValueError): self.jobs.chunk('0' * 32, 0, io.BytesIO(b'a'), 1)

    def test_concurrent_chunk_reports_busy(self):
        uid = self.jobs.start('p', 'a.gcode', 2)['upload']
        gate, entered = threading.Event(), threading.Event()
        class Slow(io.BytesIO):
            def read(self, n=-1):
                entered.set(); gate.wait(2); return super().read(n)
        worker = threading.Thread(target=self.jobs.chunk, args=(uid, 0, Slow(b'ab'), 2))
        worker.start(); entered.wait(2)
        with self.assertRaises(UploadBusy): self.jobs.chunk(uid, 0, io.BytesIO(b'ab'), 2)
        gate.set(); worker.join()
        self.assertEqual(self.jobs.status(uid)['received'], 2)

    def test_save_error_is_reported(self):
        uid = self.jobs.start('p', 'a.gcode', 1)['upload']
        self.jobs.chunk(uid, 0, io.BytesIO(b'a'), 1)
        self.jobs.finish(uid, Mock(side_effect=ValueError('Printer rejected upload: busy')))
        state = wait_done(self.jobs, uid)
        self.assertEqual((state['state'], state['error']), ('error', 'Printer rejected upload: busy'))
        with self.assertRaises(ValueError): self.jobs.finish(uid, Mock())

    def test_stale_uploads_and_leftovers_are_removed(self):
        uid = self.jobs.start('p', 'a.gcode', 5)['upload']
        self.jobs.jobs[uid]['touched'] -= upload_jobs.STALE + 1
        self.jobs.cleanup()
        with self.assertRaises(ValueError): self.jobs.status(uid)
        (self.root / ('f' * 32 + '.part')).write_bytes(b'old')
        UploadJobs(self.root, 100)
        self.assertFalse(list(self.root.glob('*.part')))


class UploadEndpointTests(unittest.TestCase):
    def handler(self, path, body):
        h = object.__new__(app.Handler)
        h.path = path
        h.headers = {'Host': f'127.0.0.1:{app.PORT}', 'X-Control-Token': app.TOKEN, 'Content-Length': str(len(body))}
        h.rfile = io.BytesIO(body)
        h.connection = Mock()
        h.client_address = ('127.0.0.1', 1)
        h.reply = lambda value, status=200, **kw: setattr(h, 'result', (status, value))
        h.do_POST()
        return h.result

    def test_start_chunk_finish_flow_saves_to_printer(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        jobs = UploadJobs(folder.name, app.UPLOAD_LIMIT)
        printer = {'id': 'qidi', 'url': 'http://192.0.2.1:7125'}
        saved = {}
        def fake_upload(source, length, filename):
            saved.update(data=source.read(), length=length, filename=filename)
            return {'filename': filename}
        with patch.object(app, 'UPLOADS', jobs), patch.object(app, 'selected', return_value=printer), \
             patch.object(app, 'upload_print_file', side_effect=fake_upload):
            status, value = self.handler('/api/upload/start', json.dumps({'printer': 'qidi', 'filename': 'part.gcode', 'size': 4}).encode())
            self.assertEqual(status, 200)
            uid = value['upload']
            self.assertEqual(self.handler(f'/api/upload/chunk?upload={uid}&offset=0', b'G1X1')[0], 200)
            self.assertEqual(self.handler('/api/upload/finish', json.dumps({'upload': uid}).encode())[0], 200)
            state = wait_done(jobs, uid)
        self.assertEqual(state['state'], 'done')
        self.assertEqual(saved, {'data': b'G1X1', 'length': 4, 'filename': 'part.gcode'})

    def test_start_rejects_folder_names(self):
        with patch.object(app, 'selected', return_value={'id': 'qidi', 'url': 'http://192.0.2.1:7125'}):
            status, _ = self.handler('/api/upload/start', json.dumps({'printer': 'qidi', 'filename': '../x.gcode', 'size': 4}).encode())
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
