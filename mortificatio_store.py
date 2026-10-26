"""Private SQLite state, atomic decisions and hash-chained security audit.

No broker data transport. Hashes detect local inconsistency, not whole-DB rollback
or a host administrator capable of rewriting the database and its audit chain.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from mortificatio_models import ApplicationError


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def secret(): return secrets.token_urlsafe(32)


def digest(namespace,value):
    if not isinstance(value,str) or not 20<=len(value)<=200:
        raise ApplicationError('INVALID_AUTHENTICATION',401)
    return hashlib.sha256((namespace+'\0'+value).encode()).hexdigest()


def private_database(path):
    """Private owned leaf directory and descriptor-checked, single-link state.

    Never chmod a shared parent. Canonicalize root-owned platform aliases, but
    refuse application-controlled ancestor symlinks. OS/same-UID compromise is
    outside this boundary; callers still need a dedicated service identity.
    """
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    original=path.absolute()
    for ancestor in (original.parent,*original.parent.parents):
        info=ancestor.lstat()
        if info.st_uid not in {0,os.geteuid()}:
            raise ApplicationError('UNTRUSTED_STATE_DIRECTORY_OWNER')
        if stat.S_ISLNK(info.st_mode) and not (str(ancestor) in {'/tmp','/var','/etc'} and info.st_uid==0):
            raise ApplicationError('SYMLINK_STATE_DIRECTORY_REFUSED')
        if stat.S_ISDIR(info.st_mode) and info.st_mode&0o022 and not (info.st_mode&stat.S_ISVTX and info.st_uid==0):
            raise ApplicationError('WRITABLE_STATE_ANCESTOR_REFUSED')
    parent=path.parent.resolve();info=parent.stat()
    if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ApplicationError('PRIVATE_OWNED_STATE_DIRECTORY_REQUIRED')
    path=parent/path.name
    directory=os.open(str(parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        opened=os.fstat(directory)
        if (opened.st_uid!=os.geteuid() or stat.S_IMODE(opened.st_mode)&0o077
                or (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino)):
            raise ApplicationError('STATE_DIRECTORY_CHANGED_DURING_OPEN')
        for name in (path.name,path.name+'-wal',path.name+'-shm'):
            try:descriptor=os.open(name,os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT if name==path.name else 0),0o600,dir_fd=directory)
            except FileNotFoundError:continue
            except OSError as exc:raise ApplicationError('UNSAFE_STATE_FILE_REFUSED') from exc
            try:
                file_info=os.fstat(descriptor)
                if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid!=os.geteuid() or file_info.st_nlink!=1:
                    raise ApplicationError('PRIVATE_REGULAR_SINGLE_LINK_STATE_REQUIRED')
                os.fchmod(descriptor,0o600)
            finally:os.close(descriptor)
    finally:os.close(directory)
    return path


class FoundationStore:
    def __init__(self,path,configuration,clock=None):
        self.path=private_database(path)
        if self.path.is_symlink():raise ApplicationError('SYMLINK_STATE_REFUSED')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.clock=clock or time.time
        binding=hashlib.sha256(encode(configuration).encode()).hexdigest()
        with self.database() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'foundation_meta' not in tables:raise ApplicationError('REFUSE_UNRELATED_DATABASE')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS foundation_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),binding TEXT NOT NULL,last_clock REAL NOT NULL,audit_sequence INTEGER NOT NULL,audit_head TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO foundation_meta VALUES (1,?,0,0,'')",(binding,))
            if db.execute('SELECT binding FROM foundation_meta').fetchone()[0]!=binding:
                raise ApplicationError('FOUNDATION_CONFIGURATION_MISMATCH')
            schemas=(
                'CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY,handle TEXT UNIQUE NOT NULL,active INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS bootstrap (token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS credentials (credential_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,public_key TEXT NOT NULL,sign_count INTEGER NOT NULL,revoked INTEGER NOT NULL,device_type TEXT NOT NULL,backed_up INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS sessions (session_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf_hash TEXT NOT NULL,created REAL NOT NULL,last_seen REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS session_csrf_tokens (csrf_hash TEXT PRIMARY KEY,session_hash TEXT NOT NULL,expires REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS challenges (flow_hash TEXT PRIMARY KEY,user_id TEXT,purpose TEXT NOT NULL,challenge TEXT NOT NULL,session_hash TEXT,context TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS grants (grant_id TEXT PRIMARY KEY,session_hash TEXT NOT NULL,user_id TEXT NOT NULL,binding TEXT NOT NULL,generation INTEGER NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS rate_events (bucket TEXT NOT NULL,stamp REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS security_audit (sequence INTEGER PRIMARY KEY,record TEXT NOT NULL,digest TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS security_alerts (sequence INTEGER PRIMARY KEY,kind TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0)',
                'CREATE TABLE IF NOT EXISTS account_state (account TEXT NOT NULL,environment TEXT NOT NULL,entry_epoch INTEGER NOT NULL,blocked INTEGER NOT NULL,PRIMARY KEY(account,environment))',
                'CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY,trade_id TEXT UNIQUE NOT NULL,user_id TEXT NOT NULL,account TEXT NOT NULL,environment TEXT NOT NULL,direction TEXT NOT NULL,entry_epoch INTEGER NOT NULL,grant_id TEXT NOT NULL,generation INTEGER NOT NULL,risk_version TEXT NOT NULL,created REAL NOT NULL,admission_day TEXT NOT NULL,state TEXT NOT NULL,ever_filled INTEGER NOT NULL,fill_day TEXT,first_fill_at TEXT,last_revision INTEGER NOT NULL,last_generation INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS lifecycle_events (evidence_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,payload TEXT NOT NULL)',
            )
            for schema in schemas:db.execute(schema)
            self.verify_audit(db)
            db.execute('COMMIT')
        self.path.chmod(0o600)

    @contextmanager
    def database(self):
        db=sqlite3.connect(str(self.path),timeout=1,isolation_level=None)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            if db.in_transaction:db.execute('ROLLBACK')
            db.close()

    def verify_audit(self,db):
        head='';count=0
        for row in db.execute('SELECT sequence,record,digest FROM security_audit ORDER BY sequence'):
            count+=1
            expected=hashlib.sha256((head+'|'+str(count)+'|'+row['record']).encode()).hexdigest()
            if row['sequence']!=count or row['digest']!=expected or encode(json.loads(row['record']))!=row['record']:
                raise ApplicationError('SECURITY_AUDIT_INTEGRITY_FAILURE',503)
            head=expected
        meta=db.execute('SELECT audit_sequence,audit_head FROM foundation_meta').fetchone()
        if tuple(meta)!=(count,head):raise ApplicationError('SECURITY_AUDIT_HEAD_MISMATCH',503)

    def audit(self,db,kind,**safe_details):
        # Callers supply stable IDs/codes only, never assertions, tokens, keys,
        # cookies, CSRF values, broker credentials, or arbitrary exception text.
        seq,head=tuple(db.execute('SELECT audit_sequence,audit_head FROM foundation_meta').fetchone())
        seq+=1
        raw=encode(dict(kind=kind,at=self.clock(),details=safe_details))
        hashed=hashlib.sha256((head+'|'+str(seq)+'|'+raw).encode()).hexdigest()
        db.execute('INSERT INTO security_audit VALUES (?,?,?)',(seq,raw,hashed))
        db.execute('UPDATE foundation_meta SET audit_sequence=?,audit_head=?',(seq,hashed))
        if kind in {'AUTHENTICATION_FAILED','SESSION_REVOKED','TRADING_AUTHORIZATION_ISSUED','OWNER_ENROLLED','LATE_EXPOSURE','CLOCK_ROLLBACK',
                    'CREDENTIAL_ADDED','CREDENTIAL_REVOKED','ACCOUNT_RECOVERED','RECOVERY_STARTED','UNENROLLED_BOOTSTRAP_RENEWED','PROTECTIVE_HEALTH_CHANGED'}:
            db.execute('INSERT INTO security_alerts(sequence,kind) VALUES (?,?)',(seq,kind))

    def transact(self,operation):
        error=None;result=None
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self.verify_audit(db)
            now=self.clock()
            import math
            if type(now) not in (int,float) or not math.isfinite(now) or now<0:
                raise ApplicationError('INVALID_SECURITY_CLOCK',503)
            last=db.execute('SELECT last_clock FROM foundation_meta').fetchone()[0]
            if now<last:
                self.audit(db,'CLOCK_ROLLBACK')
                error=ApplicationError('SECURITY_CLOCK_ROLLBACK',503)
            else:
                db.execute('UPDATE foundation_meta SET last_clock=?',(now,))
                try:result=operation(db,now)
                except ApplicationError as exc:
                    self.audit(db,'REQUEST_DENIED',code=exc.code)
                    error=exc
            db.execute('COMMIT')
        if error:raise error
        return result

    def security_events(self):
        def read(db,now):return [json.loads(r[0]) for r in db.execute('SELECT record FROM security_audit ORDER BY sequence')]
        return self.transact(read)
