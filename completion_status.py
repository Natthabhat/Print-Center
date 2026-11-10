"""Persist the UI completion grace period without changing printer telemetry."""
import json, pathlib, threading, time

class CompletionStatus:
    def __init__(self,path):
        self.path=pathlib.Path(path);self.lock=threading.Lock()
        try:self.records=json.loads(self.path.read_text())
        except (OSError,ValueError):self.records={}
        if not isinstance(self.records,dict):self.records={}

    def display(self,printer,job,now=None):
        now=time.time() if now is None else now
        state=job.get('state','unknown')
        with self.lock:
            old=self.records.get(printer)
            if state=='complete':
                filename=job.get('filename','')
                if not isinstance(old,dict) or old.get('filename')!=filename:
                    self.records[printer]={'filename':filename,'at':now}
                record=self.records[printer]
                display='ready' if now-record['at']>=1800 else 'complete'
            else:
                self.records.pop(printer,None)
                display=state
            if self.records.get(printer)!=old:
                temp=self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(self.records));temp.replace(self.path)
        return display
