"""WebAuthn-backed security foundation; no test bypass or broker credentials.

All signatures/attestations are verified by maintained py_webauthn, never by
application cryptography. Local OS-controlled bootstrap is not an HTTP route.
"""
from dataclasses import asdict, dataclass, field
import hmac
import json
from urllib.parse import urlsplit
import uuid

from webauthn import (generate_registration_options, verify_registration_response,
    generate_authentication_options, verify_authentication_response, options_to_json, base64url_to_bytes)
from webauthn.helpers import bytes_to_base64url,parse_cbor
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
    UserVerificationRequirement, PublicKeyCredentialDescriptor)

from mortificatio_models import ApplicationError, ApplicationPolicy
from mortificatio_store import FoundationStore, digest, secret, encode
from mortificatio_credential_recovery import CredentialRecovery


@dataclass(frozen=True)
class SecurityPolicy:
    rp_id: str
    origin: str
    session_ttl: int = 900
    idle_ttl: int = 300
    grant_ttl: int = 180
    challenge_ttl: int = 60
    requests_per_minute: int = 30
    failures_per_minute: int = 5

    def validate(self):
        url=urlsplit(self.origin)
        if (url.scheme!='https' or url.hostname!=self.rp_id or url.username or url.password
                or url.path or url.query or url.fragment or not self.rp_id or self.rp_id.startswith('.')):
            raise ApplicationError('EXACT_HTTPS_ORIGIN_AND_RP_REQUIRED')
        bounds={'session_ttl':3600,'idle_ttl':900,'grant_ttl':300,'challenge_ttl':120,
                'requests_per_minute':100,'failures_per_minute':10}
        for name,maximum in bounds.items():
            if type(getattr(self,name)) is not int or not 1<=getattr(self,name)<=maximum:
                raise ApplicationError('INVALID_SECURITY_POLICY')
        if self.idle_ttl>self.session_ttl or self.grant_ttl>self.session_ttl:
            raise ApplicationError('SECURITY_TTL_ORDER_INVALID')


@dataclass(frozen=True)
class SessionIssue:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: float
    user_id: str


class MortificatioSecurity(CredentialRecovery):
    def __init__(self,path,security_policy,application_policy,clock=None):
        if not isinstance(security_policy,SecurityPolicy) or not isinstance(application_policy,ApplicationPolicy):
            raise ApplicationError('EXPLICIT_SECURITY_AND_APPLICATION_POLICY_REQUIRED')
        security_policy.validate();application_policy.validate()
        self.policy,self.application_policy=security_policy,application_policy
        self.store=FoundationStore(path,{'version':1,'security':asdict(security_policy),'application':asdict(application_policy)},clock)
        self._recovery_schema()

    def _rate(self,db,now,peer,failed=False):
        bucket=digest('peer','trusted-peer-identity:'+peer)
        for scope,limit in ((('failure:' if failed else 'request:')+bucket,self.policy.failures_per_minute if failed else self.policy.requests_per_minute),
                            (('global-failure' if failed else 'global-request'),self.policy.failures_per_minute*10 if failed else self.policy.requests_per_minute*10)):
            count=db.execute('SELECT count(*) FROM rate_events WHERE bucket=? AND stamp>?',(scope,now-60)).fetchone()[0]
            if count>=limit:raise ApplicationError('AUTHENTICATION_RATE_LIMITED',429)
        if not failed:
            db.execute('INSERT INTO rate_events VALUES (?,?)',('request:'+bucket,now))
            db.execute('INSERT INTO rate_events VALUES (?,?)',('global-request',now))
            db.execute('DELETE FROM rate_events WHERE stamp<=?',(now-60,))

    def _auth_failure(self,db,now,peer):
        db.execute('INSERT INTO rate_events VALUES (?,?)',('failure:'+digest('peer','trusted-peer-identity:'+peer),now))
        db.execute('INSERT INTO rate_events VALUES (?,?)',('global-failure',now))
        self.store.audit(db,'AUTHENTICATION_FAILED')
        raise ApplicationError('AUTHENTICATION_FAILED',401)

    def provision_owner_bootstrap(self):
        """Local administrator operation; deliberately absent from HTTP adapter."""
        def operation(db,now):
            if db.execute('SELECT 1 FROM users').fetchone():raise ApplicationError('OWNER_BOOTSTRAP_ALREADY_BOUND')
            user=str(uuid.uuid4());handle=bytes_to_base64url(__import__('secrets').token_bytes(32));token=secret()
            db.execute('INSERT INTO users VALUES (?,?,0)',(user,handle))
            db.execute('INSERT INTO bootstrap VALUES (?,?,?,0)',(digest('bootstrap',token),user,now+self.policy.challenge_ttl))
            self.store.audit(db,'OWNER_BOOTSTRAP_CREATED',user_id=user)
            return token
        return self.store.transact(operation)

    def _flow(self,db,now,purpose,user,challenge,session_hash=None,context=None):
        token=secret()
        db.execute('INSERT INTO challenges VALUES (?,?,?,?,?,?,?,0)',
            (digest('flow',token),user,purpose,bytes_to_base64url(challenge),session_hash,encode(context or {}),now+self.policy.challenge_ttl))
        return token

    def begin_registration(self,bootstrap_token,peer):
        def operation(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            key=digest('bootstrap',bootstrap_token)
            row=db.execute('SELECT * FROM bootstrap WHERE token_hash=?',(key,)).fetchone()
            if row is None or row['used'] or row['expires']<=now:self._auth_failure(db,now,peer)
            db.execute('UPDATE bootstrap SET used=1 WHERE token_hash=?',(key,))
            user=db.execute('SELECT * FROM users WHERE user_id=?',(row['user_id'],)).fetchone()
            opts=generate_registration_options(rp_id=self.policy.rp_id,rp_name='Mortificatio',user_name='Mortificatio operator',
                user_id=base64url_to_bytes(user['handle']),timeout=self.policy.challenge_ttl*1000,
                authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                    user_verification=UserVerificationRequirement.REQUIRED))
            flow=self._flow(db,now,'REGISTER',row['user_id'],opts.challenge)
            return {'ceremony_token':flow,'public_key':json.loads(options_to_json(opts))}
        return self.store.transact(operation)

    def _take_flow(self,db,now,flow_token,purpose,session_hash=None):
        key=digest('flow',flow_token)
        row=db.execute('SELECT * FROM challenges WHERE flow_hash=?',(key,)).fetchone()
        if row is None or row['used'] or row['expires']<=now or row['purpose']!=purpose or row['session_hash']!=session_hash:
            raise ApplicationError('CEREMONY_EXPIRED_USED_OR_UNBOUND',401)
        db.execute('UPDATE challenges SET used=1 WHERE flow_hash=?',(key,))
        return row

    @staticmethod
    def _client_policy(response):
        if not isinstance(response,dict) or len(encode(response))>65536:raise ValueError
        client=json.loads(base64url_to_bytes(response['response']['clientDataJSON']))
        # This application does not support embedded/cross-origin ceremonies.
        if client.get('crossOrigin',False) is not False or 'topOrigin' in client:raise ValueError
        if response['type']!='public-key' or bytes_to_base64url(base64url_to_bytes(response['rawId']))!=response['id']:raise ValueError
        if 'attestationObject' in response['response']:
            attestation=parse_cbor(base64url_to_bytes(response['response']['attestationObject']))
            if not isinstance(attestation,dict) or type(attestation.get('fmt')) is not str:raise ValueError

    def complete_registration(self,flow_token,response,peer):
        def operation(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            flow=self._take_flow(db,now,flow_token,'REGISTER')
            try:
                self._client_policy(response)
                result=verify_registration_response(credential=response,expected_challenge=base64url_to_bytes(flow['challenge']),
                    expected_rp_id=self.policy.rp_id,expected_origin=self.policy.origin,require_user_presence=True,require_user_verification=True)
                credential_id=bytes_to_base64url(result.credential_id)
                if db.execute('SELECT 1 FROM credentials WHERE credential_id=?',(credential_id,)).fetchone():raise ValueError
            except Exception:self._auth_failure(db,now,peer)
            db.execute('INSERT INTO credentials VALUES (?,?,?,?,0,?,?)',(credential_id,flow['user_id'],
                bytes_to_base64url(result.credential_public_key),result.sign_count,str(result.credential_device_type.value),int(result.credential_backed_up)))
            db.execute('UPDATE users SET active=1 WHERE user_id=?',(flow['user_id'],))
            self.store.audit(db,'OWNER_ENROLLED',user_id=flow['user_id'])
            return {'result':'ENROLLED','trading_authorized':False}
        return self.store.transact(operation)

    def begin_login(self,peer):
        def operation(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            rows=[r for r in db.execute('SELECT credential_id FROM credentials WHERE revoked=0') if self._role(db,r[0])=='LOGIN']
            if not rows:raise ApplicationError('AUTHENTICATION_NOT_ENROLLED',401)
            opts=generate_authentication_options(rp_id=self.policy.rp_id,timeout=self.policy.challenge_ttl*1000,
                allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r[0])) for r in rows],
                user_verification=UserVerificationRequirement.REQUIRED)
            flow=self._flow(db,now,'LOGIN',None,opts.challenge)
            return {'ceremony_token':flow,'public_key':json.loads(options_to_json(opts))}
        return self.store.transact(operation)

    def _verify_assertion(self,db,now,flow,response,peer,expected_user=None,role='LOGIN'):
        try:
            self._client_policy(response)
            credential=db.execute('SELECT * FROM credentials WHERE credential_id=? AND revoked=0',(response['id'],)).fetchone()
            if credential is None or expected_user and credential['user_id']!=expected_user:raise ValueError
            if self._role(db,credential['credential_id'])!=role:raise ValueError
            user=db.execute('SELECT * FROM users WHERE user_id=? AND active=1',(credential['user_id'],)).fetchone()
            if user is None:raise ValueError
            handle=response['response'].get('userHandle')
            if handle is not None and base64url_to_bytes(handle)!=base64url_to_bytes(user['handle']):raise ValueError
            result=verify_authentication_response(credential=response,expected_challenge=base64url_to_bytes(flow['challenge']),
                expected_rp_id=self.policy.rp_id,expected_origin=self.policy.origin,
                credential_public_key=base64url_to_bytes(credential['public_key']),credential_current_sign_count=credential['sign_count'],
                require_user_verification=True)
        except Exception:self._auth_failure(db,now,peer)
        db.execute('UPDATE credentials SET sign_count=?,device_type=?,backed_up=? WHERE credential_id=?',
                   (result.new_sign_count,result.credential_device_type.value,int(result.credential_backed_up),credential['credential_id']))
        return user

    def complete_login(self,flow_token,response,peer,old_session_token=None):
        def operation(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            flow=self._take_flow(db,now,flow_token,'LOGIN')
            user=self._verify_assertion(db,now,flow,response,peer)
            if old_session_token:
                old=digest('session',old_session_token)
                db.execute('UPDATE sessions SET revoked=1 WHERE session_hash=? AND user_id=?',(old,user['user_id']))
                db.execute('UPDATE grants SET revoked=1 WHERE session_hash=?',(old,))
            token,csrf=secret(),secret();expires=now+self.policy.session_ttl
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,0)',(digest('session',token),user['user_id'],digest('csrf',csrf),now,now,expires))
            self.store.audit(db,'DASHBOARD_LOGIN',user_id=user['user_id'])
            return SessionIssue(token,csrf,expires,user['user_id'])
        return self.store.transact(operation)

    def require_session(self,db,now,session_token,csrf_token=None,write=False,touch=True):
        key=digest('session',session_token)
        row=db.execute('SELECT * FROM sessions WHERE session_hash=?',(key,)).fetchone()
        if row is None or row['revoked']:raise ApplicationError('DASHBOARD_AUTHENTICATION_REQUIRED',401)
        if now>=row['expires'] or now-row['last_seen']>=self.policy.idle_ttl:
            db.execute('UPDATE sessions SET revoked=1 WHERE session_hash=?',(key,))
            db.execute('UPDATE grants SET revoked=1 WHERE session_hash=?',(key,))
            self.store.audit(db,'SESSION_EXPIRED',user_id=row['user_id'])
            raise ApplicationError('DASHBOARD_SESSION_EXPIRED',401)
        if write and csrf_token is None:raise ApplicationError('CSRF_VERIFICATION_FAILED')
        if csrf_token is not None:
            csrf_hash=digest('csrf',csrf_token)
            if not hmac.compare_digest(row['csrf_hash'],csrf_hash) and db.execute(
                    'SELECT 1 FROM session_csrf_tokens WHERE csrf_hash=? AND session_hash=? AND expires>?',
                    (csrf_hash,key,now)).fetchone() is None:
                raise ApplicationError('CSRF_VERIFICATION_FAILED')
        if touch:db.execute('UPDATE sessions SET last_seen=? WHERE session_hash=?',(now,key))
        return row

    def session_status(self,session_token):
        def operation(db,now):
            row=self.require_session(db,now,session_token)
            return {'authenticated':True,'user_id':row['user_id'],'expires_at':row['expires'],'submission_enabled':False}
        return self.store.transact(operation)

    def dashboard_session(self,session_token,broker_generation=None):
        """Issue browser-memory CSRF without creating a login or trading grant.

        HTTP callers must pass the dedicated same-origin bootstrap guard. A
        Existing valid page/service tokens remain bound to the same expiring,
        revocable session. Trade commands are never auto-retried.
        """
        def operation(db,now):
            row=self.require_session(db,now,session_token)
            csrf=secret()
            db.execute('DELETE FROM session_csrf_tokens WHERE expires<=?',(now,))
            if db.execute('SELECT count(*) FROM session_csrf_tokens WHERE session_hash=?',(row['session_hash'],)).fetchone()[0]>=1024:
                raise ApplicationError('SESSION_CONTEXT_LIMIT',429)
            db.execute('INSERT INTO session_csrf_tokens VALUES (?,?,?)',(digest('csrf',csrf),row['session_hash'],row['expires']))
            db.execute('CREATE TABLE IF NOT EXISTS dashboard_scopes (user_id TEXT NOT NULL,account TEXT NOT NULL,environment TEXT NOT NULL,scope TEXT UNIQUE NOT NULL,PRIMARY KEY(user_id,account,environment))')
            binding=self.application_policy.binding
            db.execute('INSERT OR IGNORE INTO dashboard_scopes VALUES (?,?,?,?)',(row['user_id'],binding.account,binding.environment,str(uuid.uuid4())))
            scope=db.execute('SELECT scope FROM dashboard_scopes WHERE user_id=? AND account=? AND environment=?',(row['user_id'],binding.account,binding.environment)).fetchone()[0]
            grant=None
            if broker_generation is not None:
                try:grant=self.require_grant(db,now,row,broker_generation)
                except ApplicationError:pass
            return {'authenticated':True,'csrf_token':csrf,'expires_at':row['expires'],'command_scope':scope,'server_time':now,
                    'trading_authorization':{'active':grant is not None,'expires_at':grant['expires'] if grant else None},
                    'environment':binding.environment,'submission_enabled':False}
        return self.store.transact(operation)

    def begin_step_up(self,session_token,csrf_token,peer,purpose,generation=None):
        def operation(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            row=self.require_session(db,now,session_token,csrf_token,write=True)
            if purpose not in {'TRADING_AUTHORIZE','REVOKE_SESSIONS'}:raise ApplicationError('SECURITY_OPERATION_NOT_AVAILABLE')
            if purpose=='TRADING_AUTHORIZE' and (type(generation) is not int or generation<1):raise ApplicationError('VERIFIED_BROKER_GENERATION_REQUIRED')
            credentials=[r for r in db.execute('SELECT credential_id FROM credentials WHERE user_id=? AND revoked=0',(row['user_id'],)) if self._role(db,r[0])=='LOGIN']
            opts=generate_authentication_options(rp_id=self.policy.rp_id,timeout=self.policy.challenge_ttl*1000,
                allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c[0])) for c in credentials],
                user_verification=UserVerificationRequirement.REQUIRED)
            context={'binding':asdict(self.application_policy.binding),'generation':generation}
            flow=self._flow(db,now,purpose,row['user_id'],opts.challenge,row['session_hash'],context)
            return {'ceremony_token':flow,'public_key':json.loads(options_to_json(opts))}
        return self.store.transact(operation)

    def complete_step_up(self,session_token,csrf_token,flow_token,response,peer,purpose,generation=None):
        def operation(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            row=self.require_session(db,now,session_token,csrf_token,write=True)
            flow=self._take_flow(db,now,flow_token,purpose,row['session_hash'])
            context=json.loads(flow['context'])
            if context!={'binding':asdict(self.application_policy.binding),'generation':generation}:
                raise ApplicationError('STEP_UP_CONTEXT_CHANGED')
            self._verify_assertion(db,now,flow,response,peer,row['user_id'])
            if purpose=='REVOKE_SESSIONS':
                db.execute('UPDATE sessions SET revoked=1 WHERE user_id=?',(row['user_id'],))
                db.execute('UPDATE grants SET revoked=1 WHERE user_id=?',(row['user_id'],))
                self.store.audit(db,'SESSION_REVOKED',user_id=row['user_id'])
                return {'result':'ALL_SESSIONS_REVOKED'}
            if purpose!='TRADING_AUTHORIZE':raise ApplicationError('SECURITY_OPERATION_NOT_AVAILABLE')
            grant=str(uuid.uuid4());expires=min(row['expires'],now+self.policy.grant_ttl)
            db.execute('UPDATE grants SET revoked=1 WHERE session_hash=?',(row['session_hash'],))
            db.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,0)',(grant,row['session_hash'],row['user_id'],encode(asdict(self.application_policy.binding)),generation,expires))
            self.store.audit(db,'TRADING_AUTHORIZATION_ISSUED',user_id=row['user_id'])
            return {'result':'OFFLINE_TRADING_AUTHORIZATION_ACTIVE','expires_at':expires,'submission_enabled':False}
        return self.store.transact(operation)

    def require_grant(self,db,now,session,generation):
        row=db.execute('SELECT * FROM grants WHERE session_hash=? AND revoked=0 AND expires>? ORDER BY expires DESC LIMIT 1',
                       (session['session_hash'],now)).fetchone()
        if (row is None or row['user_id']!=session['user_id'] or row['generation']!=generation
                or row['binding']!=encode(asdict(self.application_policy.binding))):
            raise ApplicationError('SHORT_LIVED_TRADING_AUTHORIZATION_REQUIRED')
        return row

    def logout(self,session_token,csrf_token):
        def operation(db,now):
            row=self.require_session(db,now,session_token,csrf_token,write=True)
            db.execute('UPDATE sessions SET revoked=1 WHERE session_hash=?',(row['session_hash'],))
            db.execute('UPDATE grants SET revoked=1 WHERE session_hash=?',(row['session_hash'],))
            self.store.audit(db,'SESSION_REVOKED',user_id=row['user_id'])
            return {'result':'LOGGED_OUT'}
        return self.store.transact(operation)
