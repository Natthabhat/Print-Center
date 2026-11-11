"""Faster page loads: compressed responses, cache validators for static files, and slim home updates."""
import gzip, hashlib, re, threading

MIN_COMPRESS = 1024
YEAR = 'private, max-age=31536000, immutable'
REVALIDATE = 'private, no-cache'


def compressible(kind):
    return kind.startswith(('text/', 'application/json', 'image/svg+xml')) or 'javascript' in kind


def accepts_gzip(headers):
    for part in ((headers.get('Accept-Encoding') if headers else '') or '').lower().split(','):
        name, _, params = part.strip().partition(';')
        if name in ('gzip', '*') and params.replace(' ', '') not in ('q=0', 'q=0.0', 'q=0.00', 'q=0.000'):
            return True
    return False


def etag_matches(headers, etag):
    value = (headers.get('If-None-Match') if headers else '') or ''
    return value.strip() == '*' or etag in (t.strip().removeprefix('W/') for t in value.split(','))


class StaticFiles:
    """File bytes, a content hash and a gzipped copy, reloaded only when the file changes on disk."""
    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}

    def get(self, path):
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            item = self.files.get(path)
        if item and item['stamp'] == stamp:
            return item
        raw = path.read_bytes()
        item = {'stamp': stamp, 'raw': raw, 'hash': hashlib.sha256(raw).hexdigest()[:16], 'gzip': None}
        item['etag'] = '"' + item['hash'] + '"'
        with self.lock:
            self.files[path] = item
        return item

    def gzipped(self, item):
        if item['gzip'] is None:
            item['gzip'] = gzip.compress(item['raw'], 9, mtime=0)
        return item['gzip']

    def versioned_page(self, page, root):
        """Point the page's script tags at the scripts' content hashes, so browsers can cache them for a year."""
        def stamp(match):
            return match[1] + b'?v=' + self.get(root / match[2].decode())['hash'].encode()
        return re.sub(rb'(src="/((?:app|library)\.js))\?v=[^"]*', stamp, page)


def home_payload(data):
    """Just what a home-page printer card shows; position and temperature changes no longer resend everything."""
    if data.get('error'):
        return data
    status = data.get('status') or {}
    slim = {'print_stats': status.get('print_stats') or {}}
    for key in ('virtual_sdcard', 'display_status'):
        if isinstance(status.get(key), dict) and 'progress' in status[key]:
            slim[key] = {'progress': status[key]['progress']}
    if isinstance(status.get('bambu'), dict):
        slim['bambu'] = {'light': status['bambu'].get('light')}
    lights = [row for row in data.get('controls') or [] if row.get('id') in ('led cavity_led', 'output_pin caselight')]
    return {'status': slim, 'display_state': data.get('display_state'), 'controls': lights,
            'printer': data.get('printer'), 'connection_mode': data.get('connection_mode')}
