"""Scoped Google Drive authorization and verified, resumable production archives."""
import base64
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZIP_STORED, ZipFile

from production import atomic, digest, read

SCOPE = "https://www.googleapis.com/auth/drive.file"
OAUTH = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
FIELDS = "id,name,parents,size,md5Checksum,appProperties,trashed,mimeType"
SERVICE = "AXDAR Studio Google Drive"
CHUNK = 8*1024*1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


class DriveError(RuntimeError):
    def __init__(self, code):
        super().__init__(f"Google Drive request failed (HTTP {code}).")
        self.code = code


def request(url, method="GET", data=None, headers=None, allowed=()):
    try:
        with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers or {}, method=method), timeout=50) as stream:
            return stream.status, stream.headers, json.loads(stream.read() or "{}")
    except HTTPError as exc:
        code, headers = exc.code, exc.headers
        exc.close()
        if code in allowed:
            return code, headers, {}
        raise DriveError(code) from None


def vault():
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def account():
    raw = vault().get_password(SERVICE,"oauth")
    return json.loads(raw) if raw else None


def authorization(document, redirect):
    installed=document.get("installed", {}) if isinstance(document,dict) else {}
    if not isinstance(installed,dict):
        installed={}
    client_id=installed.get("client_id","")
    secret=installed.get("client_secret","")
    if not isinstance(client_id,str) or not client_id.endswith(".apps.googleusercontent.com") or not isinstance(secret,str) or not secret:
        raise ValueError("Import an OAuth client JSON of type Desktop app.")
    state, verifier=secrets.token_urlsafe(28),secrets.token_urlsafe(55)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params=dict(client_id=client_id,redirect_uri=redirect,response_type="code",scope=SCOPE,
                state=state,code_challenge=challenge,code_challenge_method="S256",access_type="offline",prompt="consent")
    pending=dict(state=state,verifier=verifier,client_id=client_id,client_secret=secret,
                 redirect_uri=redirect,expires=time.time()+600)
    return pending,"https://accounts.google.com/o/oauth2/v2/auth?"+urlencode(params)


def token(values):
    _,_,data=request(OAUTH,"POST",urlencode(values).encode(),{"Content-Type":"application/x-www-form-urlencoded"})
    return data


def connect(pending, state, code):
    if not pending or pending["expires"] < time.time() or not secrets.compare_digest(state,pending["state"]) or not code:
        raise ValueError("Authorization expired or was declined. Start the connection again.")
    credentials={key:pending[key] for key in ("client_id","client_secret")}
    response=token(dict(credentials,redirect_uri=pending["redirect_uri"],code_verifier=pending["verifier"],
                        grant_type="authorization_code",code=code))
    if set(response.get("scope","").split())!={SCOPE} or not response.get("refresh_token"):
        raise ValueError("Drive did not grant offline access with the requested drive.file scope.")
    credentials["refresh_token"]=response["refresh_token"]
    vault().set_password(SERVICE,"oauth",json.dumps(credentials))


class Archive:
    def __init__(self, credentials, journal):
        self.credentials,self.journal=credentials,Path(journal)
        self.entries=read(self.journal,{})
        self.bearer,self.expires="",0

    def api(self,url,method="GET", data=None, headers=None, allowed=()):
        parts=urlsplit(url)
        if parts.scheme!="https" or parts.netloc!="www.googleapis.com":
            raise ValueError("Unexpected Google Drive upload address.")
        for _ in range(2):
            if self.expires <= time.time():
                auth=token(dict(self.credentials,grant_type="refresh_token"))
                scope=auth.get("scope")
                if scope and set(scope.split())!={SCOPE}:
                    raise ValueError("Reconnect with the drive.file scope.")
                self.bearer,self.expires=auth["access_token"],time.time()+int(auth.get("expires_in",3600))-60
            status,out,body=request(url,method,data,dict(headers or {},Authorization="Bearer "+self.bearer),(*allowed,401))
            if status!=401:
                return status,out,body
            self.expires=0
        raise DriveError(401)

    def save(self):
        atomic(self.journal,self.entries)

    def lookup(self,key):
        q="trashed = false and appProperties has { key='axdar' and value='"+key+"' }"
        _,_,data=self.api(API+"/files?"+urlencode({"q":q,"fields":"nextPageToken,files("+FIELDS+")","pageSize":2}))
        files=data.get("files",[])
        if len(files)>1 or data.get("nextPageToken"):
            raise ValueError("Drive has conflicting archive identities.")
        return files[0] if files else None

    def verify(self,meta,key,parent,*,size=None,md5=None):
        if (not meta or meta.get("trashed") or meta.get("appProperties",{}).get("axdar")!=key
                or parent!="root" and parent not in meta.get("parents",[])):
            raise ValueError("Drive archive identity or parent did not verify.")
        if size is not None and (int(meta.get("size",-1))!=size or meta.get("md5Checksum")!=md5):
            raise ValueError("Google Drive did not verify the archive checksum and size.")
        return meta["id"]

    def entry(self,key):
        if key not in self.entries:
            _,_,body=self.api(API+"/files/generateIds?count=1&space=drive")
            self.entries[key]={"id":body["ids"][0]}
            self.save()
        return self.entries[key]

    def get(self,file_id):
        _,_,metadata=self.api(API+"/files/"+quote(file_id,safe="")+"?fields="+quote(FIELDS),allowed=(404,))
        return metadata

    def folder(self, name, parent, prefix):
        key=key_for(prefix)
        saved=self.entries.get(key)
        metadata=self.get(saved["id"]) if saved else self.lookup(key)
        if not metadata:
            entry=self.entry(key)
            body=json.dumps(dict(id=entry["id"],name=name,mimeType=FOLDER,parents=[parent],appProperties={"axdar":key})).encode()
            self.api(API+"/files","POST",body,{"Content-Type":"application/json"},(409,))
            metadata=self.get(entry["id"])
        if metadata.get("mimeType")!=FOLDER:
            raise ValueError("Drive archive folder is missing.")
        return self.verify(metadata,key,parent)

    def upload(self, path, name, parent, logical):
        size=path.stat().st_size
        checksum=hashlib.md5(usedforsecurity=False)
        with path.open("rb") as stream:
            for chunk in iter(lambda:stream.read(CHUNK),b""):
                checksum.update(chunk)
        md5=checksum.hexdigest()
        key=key_for(logical+":"+md5)
        saved=self.entries.get(key)
        metadata=self.get(saved["id"]) if saved else self.lookup(key)
        if metadata:
            return self.verify(metadata,key,parent,size=size,md5=md5)
        entry=self.entry(key)
        def initiate():
            body=json.dumps(dict(id=entry["id"],name=name,parents=[parent],appProperties={"axdar":key})).encode()
            status,headers,_=self.api(UPLOAD+"?uploadType=resumable","POST",body,{
                "Content-Type":"application/json","X-Upload-Content-Type":"application/zip","X-Upload-Content-Length":str(size)},(409,))
            if status==409:
                return self.verify(self.get(entry["id"]),key,parent,size=size,md5=md5)
            entry["session"]=headers["Location"]
            self.save()
        offset=0
        if entry.get("session"):
            status,headers,_=self.api(entry["session"],"PUT",b"",{"Content-Range":f"bytes */{size}"},(308,404,410))
            if status in (200,201):
                return self.verify(self.get(entry["id"]),key,parent,size=size,md5=md5)
            if status==308:
                offset=next_offset(headers)
            else:
                entry.pop("session",None)
        if not entry.get("session"):
            existing=initiate()
            if existing:
                return existing
        with path.open("rb") as stream:
            while offset<size:
                stream.seek(offset)
                chunk=stream.read(CHUNK)
                end=offset+len(chunk)-1
                status,headers,_=self.api(entry["session"],"PUT",chunk,{
                    "Content-Type":"application/zip","Content-Range":f"bytes {offset}-{end}/{size}"},(308,))
                if status in (200,201):
                    break
                next_byte=next_offset(headers)
                if not offset < next_byte <= end+1:
                    raise ValueError("Drive did not confirm upload progress. Resume the archive.")
                offset=next_byte
        confirmed=self.verify(self.get(entry["id"]),key,parent,size=size,md5=md5)
        entry.pop("session",None)
        self.save()
        return confirmed


def next_offset(headers):
    value=headers.get("Range","")
    match=re.fullmatch(r"bytes=0-(\d+)",value)
    return int(match[1])+1 if match else 0


def key_for(name):
    return hashlib.sha256(name.encode()).hexdigest()


def zip_snapshot(paths, path):
    with ZipFile(path, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        for name, source in sorted(paths.items()):
            if source.is_file():
                archive.write(source, name)


def archive_run(run, spec, credentials, just_take=None):
    if not credentials:
        return {"state":"authorization required","verified":0}
    private=run/"archive-journal"
    private.mkdir(exist_ok=True,mode=0o700)
    client=Archive(credentials,private/"drive-progress.json")
    project, episode=spec["project"],spec["episode"]
    namespace="axdar:"+project+":"+episode
    root=client.folder("AXDAR Archive","root","axdar")
    project_folder=client.folder(project,root,"axdar:"+project)
    episode_folder=client.folder(episode,project_folder,namespace)
    temp=run/"archive-upload.zip"
    count=0
    records=[just_take/"job.json"] if just_take else sorted((run/"episodes").glob("*/*/*/job.json"))
    for jf in records:
        take=read(jf)
        folder=jf.parent
        scene=take.get("scene_id") or "legacy"
        sid=take["shot"]
        take_id=take.get("take_id") or folder.name
        parent=episode_folder
        tree=namespace
        for label in (scene,sid,take_id):
            tree+=":"+label
            parent=client.folder(label,parent,tree)
        members={p.name:p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".tmp")}
        zip_snapshot(members,temp)
        client.upload(temp,"take-"+digest(temp)[:16]+".zip",parent,tree)
        count+=1
    if not just_take:
        zip_snapshot({name:run/name for name in ("spec.json","status.json","worker.log","events.json","lifecycle.json","lifecycle-cancel.json")},temp)
        client.upload(temp,"run-"+spec["id"]+"-"+digest(temp)[:16]+".zip",episode_folder,namespace+":run:"+spec["id"])
    temp.unlink(missing_ok=True)
    return {"state":"verified","verified":count,"episodeFolder":episode_folder}
