"""Passkey-only credential recovery. Verification stays in py_webauthn.

A pre-enrolled recovery credential cannot log in or authorize trading. Replacing
lost login credentials requires a fresh UV assertion and UV registration, then
revokes all sessions/grants. No passwords, recovery codes or OS auth bypass.
"""
import json
from webauthn import generate_registration_options, generate_authentication_options, options_to_json, base64url_to_bytes, verify_registration_response
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement, PublicKeyCredentialDescriptor
from mortificatio_models import ApplicationError
from mortificatio_store import digest, secret


class CredentialRecovery:
    def _recovery_schema(self):
        def schema(db,now):
            db.execute("CREATE TABLE IF NOT EXISTS credential_roles (credential_id TEXT PRIMARY KEY,role TEXT NOT NULL CHECK(role IN ('LOGIN','RECOVERY')))")
            db.execute('CREATE TABLE IF NOT EXISTS credential_epochs (user_id TEXT PRIMARY KEY,revision INTEGER NOT NULL)')
        self.store.transact(schema)

    @staticmethod
    def _role(db,credential_id):
        row=db.execute('SELECT role FROM credential_roles WHERE credential_id=?',(credential_id,)).fetchone()
        return row[0] if row else 'LOGIN'

    @staticmethod
    def _epoch(db,user):
        db.execute('INSERT OR IGNORE INTO credential_epochs VALUES (?,0)',(user,))
        return db.execute('SELECT revision FROM credential_epochs WHERE user_id=?',(user,)).fetchone()[0]

    def credentials(self,session):
        def read(db,now):
            user=self.require_session(db,now,session)['user_id']
            return {'credentials':[dict(credential_id=r['credential_id'],role=self._role(db,r['credential_id']))
                for r in db.execute('SELECT credential_id FROM credentials WHERE user_id=? AND revoked=0',(user,))],
                'submission_enabled':False}
        return self.store.transact(read)

    def begin_key_change(self,session,csrf,peer,action,target=None):
        def begin(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            row=self.require_session(db,now,session,csrf,write=True);user=row['user_id']
            if action not in ('ADD_LOGIN','ADD_RECOVERY','REVOKE') or (action=='REVOKE')!=(target is not None):
                raise ApplicationError('INVALID_CREDENTIAL_OPERATION',400)
            if target is not None and not db.execute('SELECT 1 FROM credentials WHERE credential_id=? AND user_id=? AND revoked=0',(target,user)).fetchone():
                raise ApplicationError('CREDENTIAL_NOT_FOUND',404)
            keys=[r[0] for r in db.execute('SELECT credential_id FROM credentials WHERE user_id=? AND revoked=0',(user,))
                  if self._role(db,r[0])=='LOGIN' and r[0]!=target]
            if not keys:raise ApplicationError('DISTINCT_SURVIVING_LOGIN_KEY_REQUIRED')
            opts=generate_authentication_options(rp_id=self.policy.rp_id,
                allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k)) for k in keys],
                user_verification=UserVerificationRequirement.REQUIRED)
            context=dict(action=action,target=target,epoch=self._epoch(db,user))
            token=self._flow(db,now,'KEY_CHANGE',user,opts.challenge,row['session_hash'],context)
            return {'ceremony_token':token,'public_key':json.loads(options_to_json(opts))}
        return self.store.transact(begin)

    def _check_key_context(self,db,flow):
        context=json.loads(flow['context'])
        if context['epoch']!=self._epoch(db,flow['user_id']):raise ApplicationError('CREDENTIAL_CONTEXT_CHANGED')
        return context

    def _revoke_sessions(self,db,user):
        db.execute('UPDATE sessions SET revoked=1 WHERE user_id=?',(user,))
        db.execute('UPDATE grants SET revoked=1 WHERE user_id=?',(user,))
        db.execute('UPDATE challenges SET used=1 WHERE user_id=?',(user,))
        db.execute('UPDATE credential_epochs SET revision=revision+1 WHERE user_id=?',(user,))

    def _registration(self,db,now,user,purpose,session_hash,context):
        owner=db.execute('SELECT handle FROM users WHERE user_id=? AND active=1',(user,)).fetchone()
        if owner is None:raise ApplicationError('ACTIVE_OWNER_REQUIRED')
        opts=generate_registration_options(rp_id=self.policy.rp_id,rp_name='Mortificatio',user_name='Mortificatio operator',
            user_id=base64url_to_bytes(owner[0]),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r[0]))
                for r in db.execute('SELECT credential_id FROM credentials WHERE user_id=?',(user,))],
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED))
        token=self._flow(db,now,purpose,user,opts.challenge,session_hash,context)
        return {'ceremony_token':token,'public_key':json.loads(options_to_json(opts))}

    def finish_key_change(self,session,csrf,flow_token,response,peer):
        def finish(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            row=self.require_session(db,now,session,csrf,write=True)
            flow=self._take_flow(db,now,flow_token,'KEY_CHANGE',row['session_hash'])
            context=self._check_key_context(db,flow)
            self._verify_assertion(db,now,flow,response,peer,row['user_id'])
            if response['id']==context['target']:raise ApplicationError('DISTINCT_SURVIVING_LOGIN_KEY_REQUIRED')
            if context['action']=='REVOKE':
                db.execute('UPDATE credentials SET revoked=1 WHERE credential_id=? AND user_id=?',(context['target'],row['user_id']))
                self._revoke_sessions(db,row['user_id'])
                self.store.audit(db,'CREDENTIAL_REVOKED',user_id=row['user_id'])
                return {'result':'CREDENTIAL_REVOKED_LOGIN_REQUIRED','submission_enabled':False}
            context['authorizer']=response['id']
            return self._registration(db,now,row['user_id'],'KEY_REGISTER',row['session_hash'],context)
        return self.store.transact(finish)

    def begin_recovery(self,peer):
        def begin(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            keys=list(db.execute("SELECT c.credential_id,c.user_id FROM credentials c JOIN credential_roles r USING(credential_id) JOIN users u ON u.user_id=c.user_id WHERE r.role='RECOVERY' AND c.revoked=0 AND u.active=1"))
            if not keys or len({r['user_id'] for r in keys})!=1:raise ApplicationError('RECOVERY_AUTHENTICATOR_UNAVAILABLE',401)
            user=keys[0]['user_id']
            opts=generate_authentication_options(rp_id=self.policy.rp_id,
                allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r['credential_id'])) for r in keys],
                user_verification=UserVerificationRequirement.REQUIRED)
            token=self._flow(db,now,'RECOVERY_PROOF',user,opts.challenge,context={'epoch':self._epoch(db,user)})
            self.store.audit(db,'RECOVERY_STARTED',user_id=user)
            return {'ceremony_token':token,'public_key':json.loads(options_to_json(opts))}
        return self.store.transact(begin)

    def finish_recovery_proof(self,flow_token,response,peer):
        def proof(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            flow=self._take_flow(db,now,flow_token,'RECOVERY_PROOF')
            context=self._check_key_context(db,flow)
            self._verify_assertion(db,now,flow,response,peer,flow['user_id'],role='RECOVERY')
            context.update(action='RECOVER',authorizer=response['id'])
            return self._registration(db,now,flow['user_id'],'RECOVERY_REGISTER',None,context)
        return self.store.transact(proof)

    def finish_key_registration(self,flow_token,response,peer,session=None,csrf=None,recovery=False):
        def finish(db,now):
            self._rate(db,now,peer);self._rate(db,now,peer,failed=True)
            session_hash=None
            if not recovery:session_hash=self.require_session(db,now,session,csrf,write=True)['session_hash']
            flow=self._take_flow(db,now,flow_token,'RECOVERY_REGISTER' if recovery else 'KEY_REGISTER',session_hash)
            context=self._check_key_context(db,flow)
            actor=db.execute('SELECT * FROM credentials WHERE credential_id=? AND user_id=? AND revoked=0',
                (context['authorizer'],flow['user_id'])).fetchone()
            if actor is None or self._role(db,actor['credential_id'])!=('RECOVERY' if recovery else 'LOGIN'):
                raise ApplicationError('CREDENTIAL_AUTHORITY_REVOKED')
            try:
                self._client_policy(response)
                verified=verify_registration_response(credential=response,expected_challenge=base64url_to_bytes(flow['challenge']),
                    expected_rp_id=self.policy.rp_id,expected_origin=self.policy.origin,require_user_presence=True,require_user_verification=True)
                identity=bytes_to_base64url(verified.credential_id)
                if db.execute('SELECT 1 FROM credentials WHERE credential_id=?',(identity,)).fetchone():raise ValueError
            except Exception:self._auth_failure(db,now,peer)
            role='RECOVERY' if context['action']=='ADD_RECOVERY' else 'LOGIN'
            if recovery:db.execute("UPDATE credentials SET revoked=1 WHERE user_id=? AND credential_id NOT IN (SELECT credential_id FROM credential_roles WHERE role='RECOVERY')",(flow['user_id'],))
            db.execute('INSERT INTO credentials VALUES (?,?,?,?,0,?,?)',(identity,flow['user_id'],bytes_to_base64url(verified.credential_public_key),
                verified.sign_count,verified.credential_device_type.value,int(verified.credential_backed_up)))
            db.execute('INSERT INTO credential_roles VALUES (?,?)',(identity,role))
            self._revoke_sessions(db,flow['user_id'])
            self.store.audit(db,'ACCOUNT_RECOVERED' if recovery else 'CREDENTIAL_ADDED',user_id=flow['user_id'],role=role)
            return {'result':'RECOVERED_LOGIN_REQUIRED' if recovery else 'CREDENTIAL_ADDED_LOGIN_REQUIRED',
                'trading_authorized':False,'submission_enabled':False}
        return self.store.transact(finish)

    def renew_unenrolled_bootstrap(self):
        """Local-only retry BEFORE any credential/active owner/trade exists."""
        def renew(db,now):
            users=list(db.execute('SELECT * FROM users'))
            if len(users)!=1 or users[0]['active'] or db.execute('SELECT 1 FROM credentials').fetchone() or db.execute('SELECT 1 FROM commands').fetchone():
                raise ApplicationError('ESTABLISHED_ACCOUNT_CANNOT_RESET_BOOTSTRAP')
            db.execute('UPDATE bootstrap SET used=1');db.execute('UPDATE challenges SET used=1')
            token=secret()
            db.execute('INSERT INTO bootstrap VALUES (?,?,?,0)',(digest('bootstrap',token),users[0]['user_id'],now+self.policy.challenge_ttl))
            self.store.audit(db,'UNENROLLED_BOOTSTRAP_RENEWED',user_id=users[0]['user_id'])
            return token
        return self.store.transact(renew)
