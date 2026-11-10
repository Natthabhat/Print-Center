"""PostgreSQL-backed model records with bounded files in persistent local storage."""
import base64,contextlib, hashlib, json, os, pathlib, re, secrets, shutil, tempfile, threading, uuid, zipfile, io
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg=None

LIMIT=200*1024*1024
EXTENSIONS=('.stl','.obj','.step','.stp','.3mf','.gcode','.gco','.g')
SCHEMA='''
CREATE TABLE IF NOT EXISTS library_folders (id uuid PRIMARY KEY, name varchar(80) NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE library_folders ADD COLUMN IF NOT EXISTS sku varchar(64) NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS library_folder_name_idx ON library_folders(lower(name));
CREATE TABLE IF NOT EXISTS library_models (
 id uuid PRIMARY KEY, title varchar(160) NOT NULL, notes text NOT NULL DEFAULT '',
 tags text[] NOT NULL DEFAULT '{}', printer_profile varchar(32) NOT NULL DEFAULT '',
 favorite boolean NOT NULL DEFAULT false, archived boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS library_files (
 id uuid PRIMARY KEY, model_id uuid NOT NULL REFERENCES library_models(id),
 filename varchar(200) NOT NULL, kind varchar(16) NOT NULL, bytes bigint NOT NULL CHECK(bytes>0),
 sha256 char(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE library_files ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='library_files' AND column_name='printer_profile') THEN
  ALTER TABLE library_files ADD COLUMN printer_profile varchar(32) NOT NULL DEFAULT '';
  UPDATE library_files f SET printer_profile=m.printer_profile FROM library_models m WHERE f.model_id=m.id;
 END IF;
END $$;
ALTER TABLE library_models ADD COLUMN IF NOT EXISTS folder_id uuid REFERENCES library_folders(id);
CREATE INDEX IF NOT EXISTS library_models_folder_idx ON library_models(folder_id);
CREATE INDEX IF NOT EXISTS library_files_model_idx ON library_files(model_id);
CREATE INDEX IF NOT EXISTS library_models_updated_idx ON library_models(updated_at DESC);
'''

def identifier(value):
    try:return str(uuid.UUID(str(value)))
    except (ValueError,TypeError,AttributeError):raise ValueError('Select a valid library item') from None

def filename(value):
    if not isinstance(value,str) or not value or len(value.encode('utf-8'))>200 or value.startswith('.') or any(ord(c)<32 for c in value) or any(c in value for c in ('/','\\','"')) or not value.lower().endswith(EXTENSIONS):
        raise ValueError('Use an STL, OBJ, STEP, 3MF, or sliced G-code file (filename up to 200 bytes)')
    return value

def fields(data):
    title=str(data.get('title','')).strip();notes=str(data.get('notes','')).strip()
    tags=data.get('tags',[]);profile=data.get('printer_profile','')
    if not 1<=len(title)<=160 or len(notes)>10000:raise ValueError('Enter a title up to 160 characters and notes up to 10,000 characters')
    if not isinstance(tags,list) or len(tags)>20 or any(not isinstance(t,str) or not 1<=len(t.strip())<=40 for t in tags):raise ValueError('Use up to 20 tags, each up to 40 characters')
    if profile not in ('','qidi-q2','snapmaker-u1','bambu-x2d','generic'):raise ValueError('Choose a valid printer profile')
    return title,notes,list(dict.fromkeys(t.strip() for t in tags)),profile

class ModelLibrary:
    def __init__(self,root,dsn=None):
        self.root=pathlib.Path(root)/'model-library';self.dsn=dsn if dsn is not None else os.environ.get('PRINT_CENTER_DATABASE_URL','')
        self.gate=threading.BoundedSemaphore(4);self.schema_lock=threading.Lock();self.ready=False
    @contextlib.contextmanager
    def db(self):
        if not self.dsn or psycopg is None:raise ValueError('Model library database is not configured')
        if not self.gate.acquire(timeout=5):raise ValueError('Model library is busy. Try again shortly.')
        try:
            try:
                with psycopg.connect(self.dsn,connect_timeout=5,row_factory=dict_row,options='-c statement_timeout=15000') as conn:
                    if not self.ready:
                        with self.schema_lock:
                            if not self.ready:
                                conn.execute(SCHEMA);conn.commit();self.ready=True
                    yield conn
            except psycopg.Error:raise ValueError('Model library database is unavailable. Check its server connection.') from None
        finally:self.gate.release()
    def row(self,row):
        result=dict(row)
        for key,value in result.items():
            if isinstance(value,uuid.UUID):result[key]=str(value)
            elif hasattr(value,'isoformat'):result[key]=value.isoformat()
        return result
    def list(self,query='',archived=False,favorites=False,offset=0,folder='all'):
        query=str(query)[:200];offset=max(0,min(int(offset),1000000))
        where="archived=%s AND (%s='' OR concat_ws(' ',title,notes,array_to_string(tags,' ')) ILIKE %s) AND (%s=false OR favorite=true) AND (%s='' OR library_models.id IN (SELECT model_id FROM library_files WHERE deleted=false AND filename ILIKE %s) OR concat_ws(' ',title,notes,array_to_string(tags,' ')) ILIKE %s)"
        args=(archived,'','%',favorites,query,'%'+query+'%','%'+query+'%')
        if folder=='unfiled':where+=' AND folder_id IS NULL'
        elif folder!='all':
            where+=' AND folder_id=%s';args+=(identifier(folder),)
        with self.db() as db:
            total=db.execute('SELECT count(*) AS n FROM library_models WHERE '+where,args).fetchone()['n']
            rows=db.execute('SELECT * FROM library_models WHERE '+where+' ORDER BY updated_at DESC,id LIMIT 40 OFFSET %s',args+(offset,)).fetchall()
            total_files=db.execute('SELECT count(*) AS n FROM library_files f JOIN library_models ON library_models.id=f.model_id WHERE f.deleted=false AND '+where,args).fetchone()['n']
            unfiled=db.execute('SELECT count(*) AS n FROM library_models m WHERE m.archived=false AND m.folder_id IS NULL').fetchone()['n']
            results=[]
            for row in rows:
                item=self.row(row);item['files']=[self.row(f) for f in db.execute('SELECT * FROM library_files WHERE model_id=%s AND deleted=false ORDER BY created_at,id',(row['id'],)).fetchall()];results.append(item)
        return {'models':results,'total':total,'offset':offset,'has_more':offset+len(results)<total,'storage':'PostgreSQL','folders':self.folders(),'total_files':total_files,'unfiled_files':unfiled}
    def create(self,data):
        title,notes,tags,profile=fields(data);key=str(uuid.uuid4())
        with self.db() as db:
            folder=self.folder_id(db,data.get('folder_id'))
            row=db.execute('INSERT INTO library_models(id,title,notes,tags,printer_profile,folder_id) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(key,title,notes,tags,profile,folder)).fetchone()
        return self.row(row)
    def update(self,key,data):
        key=identifier(key)
        with self.db() as db:
            if data.get('action') in ('archive','restore','favorite'):
                action=data['action']
                if action=='favorite':
                    if type(data.get('favorite')) is not bool:raise ValueError('Choose favorite on or off')
                    row=db.execute('UPDATE library_models SET favorite=%s,updated_at=now() WHERE id=%s RETURNING *',(data['favorite'],key)).fetchone()
                else:row=db.execute('UPDATE library_models SET archived=%s,updated_at=now() WHERE id=%s RETURNING *',(action=='archive',key)).fetchone()
            else:
                title,notes,tags,profile=fields(data)
                folder=self.folder_id(db,data.get('folder_id'))
                row=db.execute('UPDATE library_models SET title=%s,notes=%s,tags=%s,printer_profile=%s,folder_id=%s,updated_at=now() WHERE id=%s RETURNING *',(title,notes,tags,profile,folder,key)).fetchone()
            if not row:raise ValueError('Model not found')
        return self.row(row)
    def folders(self):
        with self.db() as db:
            rows=db.execute('SELECT folder.id,folder.name,folder.sku,count(m.id) AS files FROM library_folders folder LEFT JOIN library_models m ON m.folder_id=folder.id AND m.archived=false GROUP BY folder.id ORDER BY (length(folder.sku)=0),lower(folder.sku),lower(folder.name),folder.id').fetchall()
        return [self.row(row) for row in rows]
    def folder_id(self,db,value):
        if value in (None,''):return None
        value=identifier(value)
        if not db.execute('SELECT id FROM library_folders WHERE id=%s',(value,)).fetchone():raise ValueError('Folder not found')
        return value
    def delete_folder(self,key):
        key=identifier(key)
        with self.db() as db:
            if not db.execute('SELECT id FROM library_folders WHERE id=%s FOR UPDATE',(key,)).fetchone():raise ValueError('Folder not found')
            db.execute('UPDATE library_models SET folder_id=NULL,updated_at=now() WHERE folder_id=%s',(key,))
            db.execute('DELETE FROM library_folders WHERE id=%s',(key,))
        return {'ok':True}
    def save_folder(self,data):
        name=str(data.get('name','')).strip()
        if not 1<=len(name)<=80 or any(ord(c)<32 for c in name):raise ValueError('Use a folder name between 1 and 80 characters')
        sku=str(data.get('sku','')).strip().upper()
        if len(sku)>64 or (sku and not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]*',sku)):raise ValueError('Use a SKU up to 64 characters: letters, numbers, dots, hyphens or underscores')
        key=identifier(data['id']) if data.get('id') else str(uuid.uuid4())
        with self.db() as db:
            db.execute('SELECT pg_advisory_xact_lock(78236451)')
            if sku and db.execute('SELECT id FROM library_folders WHERE upper(sku)=%s AND id<>%s',(sku,key)).fetchone():raise ValueError('A folder with this SKU already exists')
            if db.execute('SELECT id FROM library_folders WHERE lower(name)=lower(%s) AND id<>%s',(name,key)).fetchone():raise ValueError('A folder with this name already exists')
            if data.get('id'):
                row=db.execute('UPDATE library_folders SET name=%s,sku=%s WHERE id=%s RETURNING *',(name,sku,key)).fetchone()
                if not row:raise ValueError('Folder not found')
            else:row=db.execute('INSERT INTO library_folders(id,name,sku) VALUES(%s,%s,%s) RETURNING *',(key,name,sku)).fetchone()
        return self.row(row)
    def file(self,key):
        key=identifier(key)
        with self.db() as db:
            row=db.execute('SELECT f.*,m.archived FROM library_files f JOIN library_models m ON m.id=f.model_id WHERE f.id=%s AND f.deleted=false',(key,)).fetchone()
        if not row:raise ValueError('Library file not found')
        path=self.root/(key+'.bin')
        if not path.is_file() or path.stat().st_size!=row['bytes']:raise ValueError('Stored file is missing or incomplete')
        return self.row(row),path
    def variant(self,model,profile):
        model=identifier(model)
        if profile not in ('qidi-q2','snapmaker-u1','bambu-x2d','generic'):raise ValueError('Choose a supported printer profile')
        with self.db() as db:
            row=db.execute("SELECT f.id FROM library_files f JOIN library_models m ON m.id=f.model_id WHERE m.id=%s AND m.archived=false AND f.deleted=false AND f.kind='sliced' AND f.printer_profile=%s ORDER BY f.created_at DESC,f.id DESC LIMIT 1",(model,profile)).fetchone()
        if not row:raise ValueError('Upload a sliced file for this printer in Edit model first')
        return self.file(row['id'])
    def assign(self,key,profile):
        key=identifier(key)
        if profile not in ('','qidi-q2','snapmaker-u1','bambu-x2d','generic'):raise ValueError('Choose a valid printer profile')
        with self.db() as db:
            row=db.execute('UPDATE library_files SET printer_profile=%s WHERE id=%s AND deleted=false RETURNING model_id',(profile,key)).fetchone()
            if not row:raise ValueError('Library file not found')
            db.execute('UPDATE library_models SET updated_at=now() WHERE id=%s',(row['model_id'],))
        return {'ok':True}
    def upload(self,model,name,source,length,profile=''):
        model=identifier(model);name=filename(name)
        if not 0<length<=LIMIT:raise ValueError('Library upload limit is 200 MB per file')
        if profile not in ('','qidi-q2','snapmaker-u1','bambu-x2d','generic'):raise ValueError('Choose a valid printer profile')
        with self.db() as db:
            row=db.execute('SELECT archived FROM library_models WHERE id=%s',(model,)).fetchone()
            if not row or row['archived']:raise ValueError('Choose an active model before uploading')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if shutil.disk_usage(self.root).free<length+512*1024*1024:raise ValueError('Not enough server storage')
        key=str(uuid.uuid4());dest=self.root/(key+'.bin');digest=hashlib.sha256();total=0
        fd,temp=tempfile.mkstemp(prefix='upload-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as target:
                while total<length:
                    chunk=source.read(min(65536,length-total))
                    if not chunk:raise ValueError('Incomplete library upload')
                    total+=len(chunk);target.write(chunk);digest.update(chunk)
            kind='sliced' if name.lower().endswith(('.gcode','.gco','.g')) else 'model'
            if name.lower().endswith('.3mf'):
                try:
                    with zipfile.ZipFile(temp) as archive:
                        entries=archive.infolist()
                        if len(entries)>10000 or sum(i.file_size for i in entries)>512*1024*1024:raise ValueError('3MF archive is too large')
                        if any(re.fullmatch(r'Metadata/plate_[1-9][0-9]*[.]gcode',i.filename) for i in entries):kind='sliced'
                except zipfile.BadZipFile:raise ValueError('Invalid 3MF archive') from None
            if profile and kind!='sliced':raise ValueError('Upload pre-sliced G-code or a sliced 3MF for a printer variant')
            os.replace(temp,dest)
            try:
                with self.db() as db:
                    db.execute('SELECT id FROM library_models WHERE id=%s AND archived=false FOR UPDATE',(model,))
                    if not db.execute('SELECT id FROM library_models WHERE id=%s AND archived=false',(model,)).fetchone():raise ValueError('Model was archived during upload')
                    row=db.execute('INSERT INTO library_files(id,model_id,filename,kind,bytes,sha256,printer_profile) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',(key,model,name,kind,total,digest.hexdigest(),profile)).fetchone()
                    db.execute('UPDATE library_models SET updated_at=now() WHERE id=%s',(model,))
                return self.row(row)
            except Exception:
                dest.unlink(missing_ok=True);raise
        finally:pathlib.Path(temp).unlink(missing_ok=True)

    def delete_file(self,key):
        key=identifier(key)
        with self.db() as db:
            row=db.execute('UPDATE library_files SET deleted=true WHERE id=%s AND deleted=false RETURNING model_id',(key,)).fetchone()
            if not row:raise ValueError('Library file not found')
            db.execute('UPDATE library_models SET updated_at=now() WHERE id=%s',(row['model_id'],))
        return {'ok':True}
    def preview(self,key):
        row,path=self.file(key);cache=self.root/(identifier(key)+'.webp')
        if cache.is_file():return cache.read_bytes()
        raw=None
        if row['filename'].lower().endswith('.3mf'):
            with zipfile.ZipFile(path) as archive:
                images=[i for i in archive.infolist() if re.fullmatch(r'Metadata/(?:plate_[0-9]+|top_[0-9]+|thumbnail)[.](?:png|jpg|jpeg)',i.filename,re.I) and 0<i.file_size<=5*1024*1024]
                images.sort(key=lambda i:(i.filename.lower()!='metadata/plate_1.png',i.filename))
                if images:raw=archive.read(images[0])
        elif row['kind']=='sliced':
            with path.open('rb') as source:text=source.read(8*1024*1024).decode('ascii',errors='ignore')
            matches=re.findall(r';\s*thumbnail(?:_PNG|_JPG)? begin[^\r\n]*[\r\n]+(.*?);\s*thumbnail(?:_PNG|_JPG)? end',text,re.S|re.I)
            for block in reversed(matches):
                try:
                    raw=base64.b64decode(''.join(line.lstrip(';').strip() for line in block.splitlines()),validate=True)
                    if len(raw)>5*1024*1024:raw=None
                    if raw:break
                except ValueError:continue
        if not raw:raise ValueError('No embedded print preview. Enable thumbnails in your slicer.')
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>16000000:raise ValueError('Preview is too large')
            image.thumbnail((640,480));out=io.BytesIO();image.save(out,'WEBP',quality=85);result=out.getvalue()
        fd,temp=tempfile.mkstemp(prefix='preview-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as target:target.write(result)
            os.replace(temp,cache)
        finally:pathlib.Path(temp).unlink(missing_ok=True)
        return result
