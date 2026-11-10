"""Chunked browser uploads, staged on persistent storage and saved to the printer in the background.

Every request stays short, so proxies with a response timeout (Cloudflare waits 100 s) never cut
off a large upload while the server is still copying it to the printer.
"""
import pathlib, re, secrets, threading, time

CHUNK = 8 * 1024 * 1024
STALE = 60 * 60
ACTIVE = 8
ID = re.compile(r'[0-9a-f]{32}')


class UploadBusy(Exception):
    """A chunk for this upload is still being received; retry shortly."""


class UploadJobs:
    def __init__(self, root, limit):
        self.root = pathlib.Path(root)
        self.limit = limit
        self.lock = threading.Lock()
        self.jobs = {}
        for leftover in self.root.glob('*.part') if self.root.is_dir() else ():
            try: leftover.unlink()
            except OSError: pass

    def _path(self, uid):
        return self.root / (uid + '.part')

    def _job(self, uid):
        job = self.jobs.get(uid) if isinstance(uid, str) and ID.fullmatch(uid) else None
        if job is None: raise ValueError('Upload expired. Start the upload again.')
        return job

    def _drop(self, uid):
        self.jobs.pop(uid, None)
        try: self._path(uid).unlink()
        except FileNotFoundError: pass

    def cleanup(self):
        now = time.time()
        with self.lock:
            for uid, job in list(self.jobs.items()):
                if job['state'] != 'saving' and not job['busy'] and now - job['touched'] > STALE:
                    self._drop(uid)

    def start(self, printer, filename, size):
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= self.limit:
            raise ValueError('Upload limit is 200 MB per file')
        self.cleanup()
        with self.lock:
            if sum(job['state'] == 'receiving' for job in self.jobs.values()) >= ACTIVE:
                raise ValueError('Too many uploads in progress. Try again shortly.')
            self.root.mkdir(parents=True, exist_ok=True)
            uid = secrets.token_hex(16)
            self._path(uid).open('xb').close()
            self.jobs[uid] = {'printer': printer, 'filename': filename, 'size': size, 'received': 0, 'state': 'receiving',
                              'busy': False, 'error': None, 'result': None, 'touched': time.time()}
        return {'upload': uid, 'chunk': CHUNK}

    def chunk(self, uid, offset, source, length):
        """Write one chunk. Offsets at or before the received size are accepted, so a retried chunk is idempotent."""
        with self.lock:
            job = self._job(uid)
            if job['state'] != 'receiving': raise ValueError('Upload already finished')
            if job['busy']: raise UploadBusy('Upload chunk still in progress')
            if not isinstance(offset, int) or not 0 <= offset <= job['received']: raise ValueError('Upload chunk out of order')
            if not 0 < length <= CHUNK or offset + length > job['size']: raise ValueError('Invalid upload chunk size')
            job['busy'] = True
        try:
            with self._path(uid).open('r+b') as target:
                target.seek(offset)
                target.truncate()
                remaining = length
                while remaining:
                    data = source.read(min(65536, remaining))
                    if not data: raise ValueError('Incomplete upload chunk')
                    target.write(data)
                    remaining -= len(data)
            with self.lock:
                job['received'] = offset + length
        finally:
            with self.lock:
                job['busy'] = False
                job['touched'] = time.time()
        return {'received': job['received']}

    def finish(self, uid, save):
        """Start save(job, source) in the background; poll status() for its result."""
        with self.lock:
            job = self._job(uid)
            if job['state'] != 'receiving' or job['busy']: raise ValueError('Upload is not ready to save')
            if job['received'] != job['size']: raise ValueError('Upload is incomplete. Start the upload again.')
            job['state'] = 'saving'
            job['touched'] = time.time()
        threading.Thread(target=self._save, args=(uid, job, save), daemon=True).start()
        return self.status(uid)

    def _save(self, uid, job, save):
        try:
            with self._path(uid).open('rb') as source:
                result, error, state = save(job, source), None, 'done'
        except Exception as failure:
            result, error, state = None, str(failure) or 'Could not save the file to the printer', 'error'
        with self.lock:
            job.update(state=state, result=result, error=error, touched=time.time())
            try: self._path(uid).unlink()
            except FileNotFoundError: pass

    def status(self, uid):
        with self.lock:
            job = self._job(uid)
            return {'upload': uid, 'state': job['state'], 'received': job['received'], 'size': job['size'],
                    'error': job['error'], 'result': job['result']}
