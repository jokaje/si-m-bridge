"""Standalone SI-M connector; no import-time database or network activity."""
import asyncio
import base64
import hashlib
import inspect
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
if __package__:
    from .audio_bridge import AudioUnavailable, frames
else:  # standalone CLI and local tests
    from audio_bridge import AudioUnavailable, frames

IDENT = re.compile(r'^[A-Za-z0-9_.-]{1,128}$')

def canonical_store(path):
    """Reject aliases and paths outside the selected private profile."""
    home = Path(os.environ['SIM_CONNECTOR_HERMES_HOME']).resolve(strict=True)
    candidate = Path(path).absolute()
    if candidate.is_symlink() or candidate.resolve() != home / 'sim-inbox.sqlite':
        raise ValueError('inbox store must be the profile canonical path')
    if not home.is_dir() or (home.stat().st_mode & 0o077):
        raise ValueError('profile home must be private')
    return candidate

def valid(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError('invalid identifier')
    return value

def digest(domain, value):
    return hashlib.sha256((domain + ':' + value).encode()).hexdigest()

def safe_url(url):
    if not isinstance(url,str) or any(c in url for c in ('\\','%','\r','\n','\t')): raise ValueError('invalid URL')
    try:
        u = urlsplit(url)
        port=u.port
        if (':' in u.netloc.rsplit(']',1)[-1] and port is None) or '@' in u.netloc or u.fragment or u.query or not u.hostname or u.path not in ('', '/'):
            raise ValueError('invalid URL')
    except ValueError as exc: raise ValueError('invalid URL') from exc
    try: loop = ipaddress.ip_address(u.hostname).is_loopback
    except ValueError: loop = u.hostname == 'localhost'
    if u.scheme != 'https' and not (u.scheme == 'http' and loop): raise ValueError('HTTPS or loopback required')
    return url.rstrip('/')

class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS pairing(code_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL, device TEXT NOT NULL, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS devices(token_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL, device TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, binding TEXT NOT NULL, UNIQUE(owner,profile,device));
CREATE TABLE IF NOT EXISTS audio_sessions(owner TEXT NOT NULL, profile TEXT NOT NULL, device TEXT NOT NULL, session_id TEXT NOT NULL, PRIMARY KEY(owner,profile,device));
CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,owner TEXT NOT NULL,profile TEXT NOT NULL,event_id TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,created INTEGER NOT NULL,expires INTEGER NOT NULL,opened INTEGER,UNIQUE(owner,profile,event_id));''')
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(devices)')}
        if 'binding' not in columns:
            self.db.execute('ALTER TABLE devices ADD COLUMN binding TEXT')
            self.db.execute('UPDATE devices SET revoked=1 WHERE binding IS NULL')
        path.chmod(0o600)

    def pair(self, owner, profile, device, now=None):
        for v in (owner, profile, device): valid(v)
        now = int(time.time()) if now is None else now
        code = secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO pairing VALUES(?,?,?,?,?,0)', (digest('pair',code),owner,profile,device,now+120))
        return code

    def confirm(self, code, profile, device, now=None):
        now = int(time.time()) if now is None else now
        valid(profile); valid(device)
        if not isinstance(code,str) or len(code)>128: return None
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT * FROM pairing WHERE code_hash=? AND profile=? AND device=? AND expires>? AND used=0',(digest('pair',code),profile,device,now)).fetchone()
            if row is None: self.db.execute('ROLLBACK'); return None
            token=secrets.token_urlsafe(32)
            self.db.execute('INSERT INTO devices(token_hash,owner,profile,device,binding) VALUES(?,?,?,?,?) ON CONFLICT(owner,profile,device) DO UPDATE SET token_hash=excluded.token_hash,binding=excluded.binding,revoked=0',(digest('device',token),row['owner'],profile,device,secrets.token_hex(16)))
            self.db.execute('DELETE FROM audio_sessions WHERE owner=? AND profile=? AND device=?',(row['owner'],profile,device))
            self.db.execute('UPDATE pairing SET used=1 WHERE code_hash=?',(row['code_hash'],))
            self.db.execute('COMMIT')
            return token
        except Exception:
            self.db.execute('ROLLBACK'); raise

    def auth(self, token):
        if not isinstance(token,str) or len(token)>128: return None
        return self.db.execute('SELECT owner,profile,device,binding FROM devices WHERE token_hash=? AND revoked=0',(digest('device',token),)).fetchone()

    def revoke(self, owner, profile, device):
        for v in (owner,profile,device): valid(v)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed=self.db.execute('UPDATE devices SET revoked=1 WHERE owner=? AND profile=? AND device=?',(owner,profile,device)).rowcount == 1
            self.db.execute('DELETE FROM audio_sessions WHERE owner=? AND profile=? AND device=?',(owner,profile,device))
            self.db.execute('COMMIT')
            return changed
        except Exception:
            self.db.execute('ROLLBACK'); raise

    def session(self, scope):
        owner, profile, device = (scope[k] for k in ('owner','profile','device'))
        self.db.execute('INSERT OR IGNORE INTO audio_sessions VALUES(?,?,?,?)',
                        (owner, profile, device, 'sim-' + secrets.token_hex(20)))
        return self.db.execute('SELECT session_id FROM audio_sessions WHERE owner=? AND profile=? AND device=?',
                               (owner, profile, device)).fetchone()[0]

    def put(self, owner, profile, event_id, kind, title, body, now=None):
        for v in (owner,profile,event_id): valid(v)
        if kind not in ('task_done','question') or not isinstance(title,str) or not isinstance(body,str) or not 1<=len(title)<=80 or not 1<=len(body)<=4096: raise ValueError('invalid message')
        now=int(time.time()) if now is None else now
        self.db.execute('INSERT OR IGNORE INTO messages(id,owner,profile,event_id,kind,title,body,created,expires) VALUES(?,?,?,?,?,?,?,?,?)',(secrets.token_hex(16),owner,profile,event_id,kind,title,body,now,now+30*86400))
        return self.db.execute('SELECT id FROM messages WHERE owner=? AND profile=? AND event_id=?',(owner,profile,event_id)).fetchone()[0]

    def meta(self, scope, now):
        owner,profile=scope['owner'],scope['profile']
        count=self.db.execute('SELECT count(*) FROM messages WHERE owner=? AND profile=? AND expires>? AND opened IS NULL',(owner,profile,now)).fetchone()[0]
        cursor=self.db.execute('SELECT coalesce(max(seq),0) FROM messages WHERE owner=? AND profile=?',(owner,profile)).fetchone()[0]
        return {'version':1,'unread':count,'cursor':str(cursor),'binding':scope['binding']}

    def listing(self, scope, now, cursor, limit):
        rows=self.db.execute('SELECT seq,id,kind,title,body,created,opened FROM messages WHERE owner=? AND profile=? AND expires>? AND seq>? ORDER BY seq LIMIT ?',(scope['owner'],scope['profile'],now,cursor,limit)).fetchall()
        return {'version':1,'items':[{k:r[k] for k in ('id','kind','title','body','created','opened')} | {'opened':r['opened'] is not None,'openedAt':r['opened']} for r in rows],'cursor':str(rows[-1]['seq'] if rows else cursor),'binding':scope['binding']}

    def open(self, scope, ident, now):
        if not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident): return False
        return self.db.execute('UPDATE messages SET opened=coalesce(opened,?) WHERE id=? AND owner=? AND profile=? AND expires>?',(now,ident,scope['owner'],scope['profile'],now)).rowcount==1

async def hermes_turn(url, key, text, session_id):
    safe_url(url)
    if not key: raise ValueError('Hermes API key required')
    async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=3),follow_redirects=False,trust_env=False) as client:
        response=await client.post(url+'/v1/chat/completions',headers={'Authorization':'Bearer '+key,'X-Hermes-Session-Id':session_id,'X-Hermes-Session-Key':session_id},json={'model':'hermes','messages':[{'role':'user','content':text}],'stream':False,'max_tokens':512})
        response.raise_for_status()
        if len(response.content)>65536: raise ValueError('upstream oversized')
        reply=response.json()['choices'][0]['message']['content']
        if not isinstance(reply,str): raise ValueError('invalid upstream reply')
        return reply[:4096]

def _is_async_callable(fn):
    """True for async functions and for objects with an async __call__.

    `inspect.iscoroutinefunction` returns False for a callable instance whose
    `__call__` is async -- such an adapter was silently run in a worker thread,
    which returns a coroutine object instead of audio and surfaces as a 503.
    """
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, '__call__', None)
    return inspect.iscoroutinefunction(call)


async def _call(fn, *args, timeout=None):
    """Await an async adapter, or run a sync one off the event loop."""
    if _is_async_callable(fn):
        coro = fn(*args)
    else:
        coro = asyncio.to_thread(fn, *args)
    return await asyncio.wait_for(coro, timeout=timeout) if timeout else await coro


def create_app(store, hermes_url=None, hermes_key=None, transcribe=None, hermes_profile=None, synthesize=None, gateway_attestation=None, turn_runner=None):
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.middleware('http')
    async def bounds(request:Request, call_next):
        if request.method=='POST':
            maximum=256000 if request.url.path=='/v1/turn-stream' and request.headers.get('content-type','').startswith('application/vnd.kim.pcm;') else 90000
            length=request.headers.get('content-length')
            if length and (not length.isdigit() or int(length)>maximum): return JSONResponse({'error':'too large'},status_code=413)
            body=await request.body()
            if len(body)>maximum: return JSONResponse({'error':'too large'},status_code=413)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response
    def scope(request):
        header=request.headers.get('authorization','')
        row=store.auth(header[7:]) if header.startswith('Bearer ') else None
        if row is None: raise HTTPException(401,'unauthorized')
        if request.url.path in ('/v1/turn','/v1/turn-stream') and (not hermes_profile or row['profile']!=hermes_profile or (turn_runner is None and (gateway_attestation is None or not gateway_attestation(hermes_profile, hermes_url)))): raise HTTPException(403,'profile unavailable')
        return row
    @app.post('/v1/pair/confirm')
    async def confirm(request:Request):
        try:
            data=await request.json()
            if set(data)!={'version','code','profile','device'} or data['version']!=1: raise ValueError()
            token=store.confirm(data['code'],data['profile'],data['device'])
        except (ValueError,KeyError,TypeError): raise HTTPException(400,'invalid pairing')
        if token is None: raise HTTPException(403,'invalid pairing')
        return {'version':1,'token':token}
    @app.get('/v1/inbox/meta')
    async def meta(request:Request): return store.meta(scope(request),int(time.time()))
    @app.get('/v1/inbox')
    async def listing(request:Request,cursor:str='0',limit:int=20):
        s=scope(request)
        if not re.fullmatch(r'(0|[1-9][0-9]{0,18})',cursor) or int(cursor)>2**63-1 or not 1<=limit<=50: raise HTTPException(400,'invalid page')
        return store.listing(s,int(time.time()),int(cursor),limit)
    @app.post('/v1/inbox/{ident}/open')
    async def open_item(ident:str,request:Request):
        if not store.open(scope(request),ident,int(time.time())): raise HTTPException(404,'not found')
        return {'version':1,'opened':True}
    @app.post('/v1/turn')
    async def turn(request:Request):
        s=scope(request)
        try:
            data=await request.json()
            if set(data)!={'version','text'} or data['version']!=1 or not isinstance(data['text'],str) or not 1<=len(data['text'])<=4096: raise ValueError()
            reply=await _call(turn_runner,data['text'],store.session(s)) if turn_runner else await hermes_turn(hermes_url,hermes_key,data['text'],store.session(s))
        except (ValueError,KeyError,TypeError,httpx.HTTPError): raise HTTPException(503,'turn unavailable')
        return {'version':1,'reply':reply}
    @app.post('/v1/turn-stream')
    async def audio(request:Request):
        s=scope(request)
        try:
            if request.headers.get('content-type','').split(';')[0].strip() == 'application/vnd.kim.pcm':
                if request.headers.get('content-type','').strip() != 'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le': raise ValueError()
                pcm=await request.body()
            else:
                data=await request.json()
                if set(data)!={'version','pcm16le_b64','sample_rate'} or data['version']!=1 or data['sample_rate']!=16000 or not isinstance(data['pcm16le_b64'],str) or len(data['pcm16le_b64'])>90000: raise ValueError()
                pcm=base64.b64decode(data['pcm16le_b64'],validate=True)
            if not pcm or len(pcm)>(256000 if request.headers.get('content-type','').startswith('application/vnd.kim.pcm;') else 64000) or len(pcm)%2 or transcribe is None or synthesize is None: raise ValueError()
            result=await _call(transcribe,pcm,timeout=15)
            text=result.decode('utf-8') if isinstance(result,bytes) else result
            if not isinstance(text,str) or not 1<=len(text)<=4096: raise ValueError()
            reply=await _call(turn_runner,text,store.session(s)) if turn_runner else await hermes_turn(hermes_url,hermes_key,text,store.session(s))
            pcm_out=await _call(synthesize,reply,timeout=30)
            chunks=frames(pcm_out)
        except (ValueError,KeyError,TypeError,UnicodeError,httpx.HTTPError,asyncio.TimeoutError,AudioUnavailable,OSError): raise HTTPException(503,'audio unavailable')
        return StreamingResponse(iter(chunks),media_type='application/vnd.kim.stream-v1')
    return app
