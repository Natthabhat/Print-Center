"""Bounded Bambu implicit-FTPS file access; existing files use recoverable trash."""
import contextlib
import datetime
import ftplib
import io
import hashlib
import json
import math
import pathlib
import posixpath
import re
import secrets
import shutil
import socket
import ssl
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from bambu import certificate_serial

LIMIT=200*1024*1024


def file_path(value, trash=False):
    if not isinstance(value,str) or not value or len(value)>512 or any(c in value for c in ('\r','\n','\\','\0')):
        raise ValueError('Invalid printer filename')
    path=pathlib.PurePosixPath('/'+value.lstrip('/'))
    if '..' in path.parts or not str(path).lower().endswith(('.3mf','.gcode','.gco','.g')):
        raise ValueError('Select a printer G-code or 3MF file')
    parts=path.parts[1:]
    if any(part.startswith('.') and part!='.print-center-trash' for part in parts): raise ValueError('Invalid file path')
    if '.print-center-trash' in parts and not trash: raise ValueError('Restore this file from trash first')
    if len(parts)>4: raise ValueError('File path is too deep')
    return str(path)


class ImplicitFTP(ftplib.FTP_TLS):
    def __init__(self, printer):
        ctx=ssl.create_default_context(cafile=str(pathlib.Path(__file__).with_name('bambu_ca.pem')))
        ctx.check_hostname=False;ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        super().__init__(context=ctx,timeout=20)
        self.printer=printer;self.identity=printer.get('serial','')

    def connect(self,host='',port=990,timeout=20):
        self.host=host;self.port=port;self.timeout=timeout
        raw=socket.create_connection((host,port),timeout)
        try:
            self.sock=self.context.wrap_socket(raw,server_hostname=self.identity or host)
            self.identity=certificate_serial(self.sock.getpeercert(),self.identity)
        except Exception:
            raw.close();raise
        self.af=self.sock.family
        self.file=self.sock.makefile('r',encoding=self.encoding)
        return self.getresp()

    def ntransfercmd(self,cmd,rest=None):
        conn,size=ftplib.FTP.ntransfercmd(self,cmd,rest)
        try:
            conn=self.context.wrap_socket(conn,server_hostname=self.identity,session=self.sock.session)
            certificate_serial(conn.getpeercert(),self.identity)
            return conn,size
        except Exception:
            conn.close();raise

    def lines(self,command):
        self.voidcmd('TYPE A')
        result=bytearray()
        with self.transfercmd(command) as conn:
            while chunk:=conn.recv(16384):
                result.extend(chunk)
                if len(result)>4*1024*1024: raise ValueError('Printer directory is too large')
        self.voidresp()
        return bytes(result).decode('utf-8',errors='replace').splitlines()

    def read_into(self,path,output,expected=None,active=lambda:True):
        self.voidcmd('TYPE I');total=0
        with self.transfercmd('RETR '+path) as conn:
            while active() and (chunk:=conn.recv(65536)):
                total+=len(chunk)
                if total>LIMIT: raise ValueError('File exceeds 200 MB')
                output.write(chunk)
        self.voidresp()
        if expected is not None and total!=expected: raise ValueError('Printer file transfer was incomplete')
        return total

    def write_from(self,path,source,length):
        self.voidcmd('TYPE I');total=0
        with self.transfercmd('STOR '+path) as conn:
            while chunk:=source.read(65536):
                total+=len(chunk)
                if total>length: raise ValueError('Upload exceeds declared length')
                conn.sendall(chunk)
            # STOR requires close_notify so the printer can finish the upload.
            try: conn.unwrap()
            except (ssl.SSLError,TimeoutError): pass
        self.voidresp()
        if total!=length: raise ValueError('Incomplete file upload')


def parse_listing(lines, directory):
    rows=[]
    for line in lines:
        match=re.match(r'^([d-])[rwxstST-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3})\s+(\d+)\s+([\d:]+)\s+(.+)$',line)
        if not match: continue
        kind,size,month,day,clock,name=match.groups()
        if name in ('.','..') or '/' in name or '\\' in name: continue
        year=str(datetime.datetime.now().year) if ':' in clock else clock
        stamp=month+' '+day+' '+year+(' '+clock if ':' in clock else ' 00:00')
        try: modified=datetime.datetime.strptime(stamp,'%b %d %Y %H:%M').timestamp()
        except ValueError: modified=0
        rows.append({'path':posixpath.join(directory,name).lstrip('/'),'size':int(size),'modified':modified,'directory':kind=='d'})
    return rows


class BambuFiles:
    def __init__(self,printer,cache_root):
        self.printer=dict(printer);self.cache_root=pathlib.Path(cache_root)
        self.gate=threading.BoundedSemaphore(2);self.lock=threading.RLock()
        self.files_cache=None;self.previews={};self.preview_gate=threading.Lock()

    @contextlib.contextmanager
    def connection(self):
        with self.gate:
            ftp=ImplicitFTP(self.printer)
            try:
                ftp.connect(urllib.parse.urlsplit(self.printer['url']).hostname)
                ftp.login('bblp',self.printer['access_code']);ftp.prot_p()
                yield ftp
            finally: ftp.close()

    def listing(self,trash=False,refresh=False):
        with self.lock:
            if not trash and not refresh and self.files_cache and time.monotonic()-self.files_cache[0]<15:
                return list(self.files_cache[1])
        rows=[]
        with self.connection() as ftp:
            pending=[('/',0)];visited=set()
            while pending and len(rows)<1000 and len(visited)<64:
                directory,depth=pending.pop(0)
                if directory in visited:continue
                visited.add(directory)
                # The printer's LIST omits dot directories, including our trash folder.
                if trash and '.print-center-trash' not in pathlib.PurePosixPath(directory).parts and depth<=2:
                    pending.append((posixpath.join(directory,'.print-center-trash'),depth+1))
                try: entries=parse_listing(ftp.lines('LIST '+directory),directory)
                except ftplib.error_perm: continue
                for item in entries:
                    name=posixpath.basename(item['path'])
                    is_trash='.print-center-trash' in pathlib.PurePosixPath(item['path']).parts
                    if item['directory']:
                        if depth<(3 if trash else 2) and (name in ('cache','model','models','gcodes','.print-center-trash') or directory not in ('/',)):
                            if name=='.print-center-trash' and not trash: continue
                            if not name.startswith('.') or name=='.print-center-trash': pending.append(('/'+item['path'],depth+1))
                    elif is_trash==trash and name.lower().endswith(('.3mf','.gcode','.gco','.g')):
                        try:file_path(item['path'],trash=trash)
                        except ValueError:continue
                        rows.append({k:item[k] for k in ('path','size','modified')})
        rows.sort(key=lambda x:x['modified'],reverse=True)
        if not trash:
            with self.lock:self.files_cache=(time.monotonic(),rows)
        return rows

    def invalidate(self):
        with self.lock:self.files_cache=None;self.previews.clear()

    def upload(self,source,length,filename):
        name=posixpath.basename(file_path(filename))
        if not 0<length<=LIMIT:raise ValueError('Upload limit is 200 MB')
        target='/cache/'+name;temporary='/cache/.pc-upload-'+secrets.token_hex(8)
        with self.connection() as ftp:
            try:ftp.mkd('/cache')
            except ftplib.error_perm:pass
            if any(posixpath.basename(x['path'])==name for x in parse_listing(ftp.lines('LIST /cache'),'/cache')):
                raise ValueError('A file with that name already exists. Rename your upload first.')
            try:
                ftp.write_from(temporary,source,length)
                ftp.rename(temporary,target)
            except Exception:
                try:ftp.delete(temporary)
                except ftplib.all_errors:pass
                raise
        self.invalidate()
        return {'filename':target.lstrip('/')}

    def trash(self,filename,restore=False):
        path=file_path(filename,trash=restore)
        parent=posixpath.dirname(path);name=posixpath.basename(path)
        with self.connection() as ftp:
            if restore:
                if posixpath.basename(parent)!='.print-center-trash' or '--' not in name:raise ValueError('Select a trashed file')
                target=posixpath.join(posixpath.dirname(parent),name.split('--',1)[1])
                try:
                    ftp.voidcmd('TYPE I');ftp.size(target)
                    raise ValueError('A file with the original name already exists')
                except ftplib.error_perm:pass
            else:
                folder=posixpath.join(parent,'.print-center-trash')
                try:ftp.mkd(folder)
                except ftplib.error_perm:pass
                target=posixpath.join(folder,secrets.token_hex(8)+'--'+name)
            ftp.rename(path,target)
        self.invalidate()
        return {'filename':target.lstrip('/')}

    @contextlib.contextmanager
    def download(self,filename):
        path=file_path(filename)
        with self.connection() as ftp:
            ftp.voidcmd('TYPE I');size=ftp.size(path)
            if size is None or not 0<size<=LIMIT:raise ValueError('File must be between 1 byte and 200 MB')
            yield ftp,path,size

    def archive_info(self,filename,plate=None):
        path=file_path(filename)
        rows=self.listing();row=next((x for x in rows if '/'+x['path']==path),None)
        if row is None:raise ValueError('Select an existing printer file')
        if plate is not None and (not str(plate).isdigit() or not 1<=int(plate)<=1000):raise ValueError('Invalid plate number')
        match=re.search(r'_plate_([1-9][0-9]{0,2})(?=\.gcode\.3mf$)',path,re.I)
        preferred=int(plate) if plate is not None else int(match[1]) if match else None
        key=(path,row['modified'],row['size'],preferred)
        with self.preview_gate:
            if key in self.previews:return self.previews[key]
            meta,image={},None
            self.cache_root.mkdir(parents=True,exist_ok=True,mode=0o700)
            if shutil.disk_usage(self.cache_root).free<max(512*1024*1024,row['size']*2):raise ValueError('Not enough server storage to read this file')
            if path.lower().endswith('.3mf'):
                with tempfile.TemporaryFile(dir=self.cache_root) as temp:
                    with self.download(path) as (ftp,p,size):ftp.read_into(p,temp,size)
                    temp.seek(0)
                    with zipfile.ZipFile(temp) as z:
                        if len(z.infolist())>10000:raise ValueError('Archive has too many entries')
                        meta['plates']=[]
                        gcode_budget=LIMIT
                        for item in z.infolist():
                            match=re.fullmatch(r'Metadata/plate_([1-9][0-9]{0,2})[.]gcode',item.filename)
                            if match and 0<item.file_size<=LIMIT:
                                gcode_budget-=item.file_size
                                if gcode_budget<0:raise ValueError('Sliced plate data exceeds 200 MB')
                                digest=hashlib.md5()
                                with z.open(item) as gcode:
                                    for chunk in iter(lambda:gcode.read(65536),b''):digest.update(chunk)
                                meta['plates'].append({'id':int(match[1]),'param':item.filename,'md5':digest.hexdigest(),'filaments':[]})
                        xml_budget=4*1024*1024
                        for entry in z.infolist():
                            if entry.filename.lower().endswith('slice_info.config') and entry.file_size<=min(2*1024*1024,xml_budget):
                                xml=z.read(entry)
                                xml_budget-=len(xml)
                                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():continue
                                try:root=ET.fromstring(xml)
                                except ET.ParseError:continue
                                xmlplates=root.findall('.//plate')
                                for source in xmlplates:
                                    number=next((m.get('value') for m in source.findall('metadata') if m.get('key')=='index'),'1')
                                    target=next((p for p in meta['plates'] if str(p['id'])==number),None)
                                    if target is not None:
                                        for f in source.findall('filament'):
                                            fid=f.get('id','')
                                            if fid.isdigit() and 1<=int(fid)<=64:
                                                target['filaments'].append({'id':int(fid),'type':f.get('type','Unknown'),'color':f.get('color','#808080')})
                                selected_id=preferred or min((p['id'] for p in meta['plates']),default=1)
                                plate=next((x for x in xmlplates if next((m.get('value') for m in x.findall('metadata') if m.get('key')=='index'),'1')==str(selected_id)),None)
                                if plate is None and len(xmlplates)==1:plate=xmlplates[0]
                                if plate is not None:
                                    for item in plate.findall('metadata'):
                                        if item.get('key')=='prediction':
                                            try:
                                                value=float(item.get('value','0'))
                                                if math.isfinite(value) and 0<=value<=31536000:meta['estimated_time']=value
                                            except ValueError:pass
                                    weights=[]
                                    for f in plate.findall('filament'):
                                        try:
                                            value=float(f.get('used_g','0'))
                                            if math.isfinite(value) and 0<=value<=100000:weights.append(value)
                                        except ValueError:pass
                                    if weights:meta['filament_weight_total']=sum(weights)
                        candidates=[i for i in z.infolist() if re.fullmatch(r'Metadata/(plate_\d+|top_\d+|thumbnail)\.png',i.filename,re.I) and i.file_size<=5*1024*1024]
                        if candidates:
                            selected_id=preferred or min((p['id'] for p in meta['plates']),default=1)
                            names=[f'Metadata/plate_{selected_id}.png',f'Metadata/top_{selected_id}.png']
                            if len(meta['plates'])==1:
                                actual=meta['plates'][0]['id']
                                names.extend([f'Metadata/plate_{actual}.png',f'Metadata/top_{actual}.png'])
                            names.append('Metadata/thumbnail.png')
                            chosen=next((i for name in names for i in candidates if i.filename.lower()==name.lower()),None)
                            if chosen is None:raise ValueError('No thumbnail for the selected plate')
                            raw=z.read(chosen)
                            if raw.startswith(b'\x89PNG\r\n\x1a\n'):image=raw
            while self.previews and (len(self.previews)>=30 or sum(len(v[1] or b'') for v in self.previews.values())+(len(image) if image else 0)>32*1024*1024):
                self.previews.pop(next(iter(self.previews)))
            self.previews[key]=(meta,image)
            return meta,image
