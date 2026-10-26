"""Transport-neutral secure API contract for Phase 1. No HTTP listener or UI.

A future TLS adapter must derive secure_transport and peer_id from the actual
connection, never trust browser forwarding headers. All tests are in-memory.
"""
from dataclasses import dataclass
from http.cookies import SimpleCookie, CookieError
import json
from urllib.parse import urlsplit, parse_qsl
from executor_blind_presentation import dashboard_response

from mortificatio_models import ApplicationError, TradeCommand

SESSION_COOKIE='__Host-mortificatio'
FLOW_COOKIE='__Host-mortificatio-ceremony'


@dataclass(frozen=True)
class APIRequest:
    method: str
    path: str
    headers: dict
    body: bytes = b''
    secure_transport: bool = False
    peer_id: str = ''


@dataclass(frozen=True)
class APIResponse:
    status: int
    body: dict
    headers: tuple


def cookie(name,value,max_age):
    return '{}={}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={}'.format(name,value,max_age)


def cookies(request):
    raw=request.headers.get('Cookie','')
    if len(raw)>4096 or any(raw.count(name+'=')>1 for name in (SESSION_COOKIE,FLOW_COOKIE)):
        raise ApplicationError('INVALID_COOKIE',401)
    parsed=SimpleCookie()
    try:parsed.load(raw)
    except CookieError:raise ApplicationError('INVALID_COOKIE',401)
    return {key:item.value for key,item in parsed.items()}


class FoundationAPI:
    def __init__(self,application):self.application=application;self.security=application.security

    def dispatch(self,request):
        headers=[('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),
                 ('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'")]
        try:
            if not isinstance(request,APIRequest) or request.secure_transport is not True or not request.peer_id:
                raise ApplicationError('AUTHENTICATED_TLS_TRANSPORT_REQUIRED')
            expected=urlsplit(self.security.policy.origin).netloc
            if request.headers.get('Host')!=expected:raise ApplicationError('INVALID_HOST')
            origin=request.headers.get('Origin')
            if (request.method!='GET' and origin!=self.security.policy.origin or origin is not None and origin!=self.security.policy.origin
                    or request.headers.get('Sec-Fetch-Site','same-origin') not in {'same-origin','none'}):
                raise ApplicationError('ORIGIN_VERIFICATION_FAILED')
            jar=cookies(request);session=jar.get(SESSION_COOKIE);flow=jar.get(FLOW_COOKIE)
            csrf=request.headers.get('X-CSRF-Token')
            uri=urlsplit(request.path)
            if uri.scheme or uri.netloc or uri.fragment or not request.path.startswith('/'):
                raise ApplicationError('INVALID_API_PATH',400)
            path=uri.path
            if request.method=='GET' and path=='/auth/session' and not uri.query:
                if request.headers.get('Sec-Fetch-Site')!='same-origin' or request.headers.get('X-Mortificatio-Session')!='bootstrap':
                    raise ApplicationError('SAME_ORIGIN_SESSION_BOOTSTRAP_REQUIRED')
                result=self.application.dashboard_session(session)
            elif request.method=='GET' and path=='/auth/credentials' and not uri.query:
                result=self.security.credentials(session)
            elif request.method=='GET' and path.startswith('/api/commands/') and not uri.query:
                if len(path.split('/'))!=4:raise ApplicationError('INVALID_COMMAND_LOOKUP',400)
                result=self.application.command_status(session,path.split('/')[3])
            elif request.method=='GET' and path.startswith('/api/history'):
                self.security.session_status(session)
                history=getattr(self.application,'history',None)
                if history is None:raise ApplicationError('HISTORY_SERVICE_UNAVAILABLE',404)
                if len(uri.query)>2048:raise ApplicationError('INVALID_HISTORY_QUERY',400)
                try:pairs=parse_qsl(uri.query,keep_blank_values=True,strict_parsing=True,max_num_fields=8) if uri.query else []
                except ValueError:raise ApplicationError('INVALID_HISTORY_QUERY',400)
                if len(dict(pairs))!=len(pairs):raise ApplicationError('DUPLICATE_HISTORY_FILTER',400)
                parameters=dict(pairs)
                if path=='/api/history':result=history.list_trades(session,parameters)
                elif path=='/api/history/summary':result=history.summary(session,parameters)
                else:
                    pieces=path.split('/')
                    if parameters or len(pieces) not in (4,5) or len(pieces)==5 and pieces[4]!='chart':
                        raise ApplicationError('INVALID_HISTORY_PATH_OR_QUERY',400)
                    result=history.chart(session,pieces[3]) if len(pieces)==5 else history.detail(session,pieces[3])
            elif uri.query:
                raise ApplicationError('QUERY_NOT_ALLOWED',400)
            elif request.method=='GET' and request.path=='/api/status':
                result=self.application.status(session)
            elif request.method=='POST':
                if request.headers.get('Content-Type')!='application/json' or len(request.body)>65536:
                    raise ApplicationError('BOUNDED_JSON_REQUIRED',400)
                def unique(pairs):
                    result={}
                    for key,value in pairs:
                        if key in result:raise ApplicationError('DUPLICATE_JSON_FIELD',400)
                        result[key]=value
                    return result
                try:body=json.loads(request.body,object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError,UnicodeError,RecursionError):raise ApplicationError('INVALID_JSON',400)
                if not isinstance(body,dict):raise ApplicationError('JSON_OBJECT_REQUIRED',400)
                path=request.path
                if path=='/auth/register/options' and not body:
                    header=request.headers.get('Authorization','')
                    if not header.startswith('Bootstrap '):raise ApplicationError('LOCAL_BOOTSTRAP_REQUIRED',401)
                    result=self.security.begin_registration(header[10:],request.peer_id)
                elif path=='/auth/register/complete' and set(body)=={'credential'}:
                    result=self.security.complete_registration(flow,body['credential'],request.peer_id)
                    headers.append(('Set-Cookie',cookie(FLOW_COOKIE,'',0)))
                elif path=='/auth/login/options' and not body:
                    result=self.security.begin_login(request.peer_id)
                elif path=='/auth/login/complete' and set(body)=={'credential'}:
                    issue=self.security.complete_login(flow,body['credential'],request.peer_id,session)
                    headers.extend([('Set-Cookie',cookie(SESSION_COOKIE,issue.token,self.security.policy.session_ttl)),
                                    ('Set-Cookie',cookie(FLOW_COOKIE,'',0))])
                    result={'authenticated':True,'csrf_token':issue.csrf_token,'expires_at':issue.expires_at,'trading_authorized':False,'submission_enabled':False}
                elif path=='/auth/keys/options' and set(body) in ({'action'},{'action','target'}):
                    result=self.security.begin_key_change(session,csrf,request.peer_id,body['action'],body.get('target'))
                elif path=='/auth/keys/prove' and set(body)=={'credential'}:
                    result=self.security.finish_key_change(session,csrf,flow,body['credential'],request.peer_id)
                elif path=='/auth/keys/register' and set(body)=={'credential'}:
                    result=self.security.finish_key_registration(flow,body['credential'],request.peer_id,session,csrf)
                elif path=='/auth/recovery/options' and not body:
                    result=self.security.begin_recovery(request.peer_id)
                elif path=='/auth/recovery/prove' and set(body)=={'credential'}:
                    result=self.security.finish_recovery_proof(flow,body['credential'],request.peer_id)
                elif path=='/auth/recovery/register' and set(body)=={'credential'}:
                    result=self.security.finish_key_registration(flow,body['credential'],request.peer_id,recovery=True)
                elif path=='/auth/trading/options' and not body:
                    result=self.application.begin_trading_authorization(session,csrf,request.peer_id)
                elif path=='/auth/trading/complete' and set(body)=={'credential'}:
                    result=self.application.finish_trading_authorization(session,csrf,flow,body['credential'],request.peer_id)
                    headers.append(('Set-Cookie',cookie(FLOW_COOKIE,'',0)))
                elif path=='/auth/revoke-sessions/options' and not body:
                    result=self.security.begin_step_up(session,csrf,request.peer_id,'REVOKE_SESSIONS')
                elif path=='/auth/revoke-sessions/complete' and set(body)=={'credential'}:
                    result=self.security.complete_step_up(session,csrf,flow,body['credential'],request.peer_id,'REVOKE_SESSIONS')
                    headers.extend([('Set-Cookie',cookie(FLOW_COOKIE,'',0)),('Set-Cookie',cookie(SESSION_COOKIE,'',0))])
                elif path=='/auth/logout' and not body:
                    result=self.security.logout(session,csrf)
                    headers.append(('Set-Cookie',cookie(SESSION_COOKIE,'',0)))
                elif path=='/api/commands' and set(body)=={'command_id','direction','entry_epoch'}:
                    result=self.application.direction_command(session,csrf,TradeCommand(**body))
                elif path=='/api/exit-all' and set(body)=={'request_id'}:
                    result=self.application.emergency_exit_all(session,csrf,body['request_id'])
                elif path.startswith('/api/settings/'):
                    self.security.store.transact(lambda db,now:self.security.require_session(db,now,session,csrf,write=True))
                    raise ApplicationError('CONFIGURATION_CHANGES_DISABLED_PHASE_ONE')
                else:raise ApplicationError('OPERATION_OR_FIELDS_NOT_ALLOWED',404)
                if 'ceremony_token' in result:
                    result=dict(result)
                    token=result.pop('ceremony_token')
                    headers.append(('Set-Cookie',cookie(FLOW_COOKIE,token,self.security.policy.challenge_ttl)))
                elif result.get('result') in {'CREDENTIAL_ADDED_LOGIN_REQUIRED','CREDENTIAL_REVOKED_LOGIN_REQUIRED','RECOVERED_LOGIN_REQUIRED'}:
                    headers.extend([('Set-Cookie',cookie(FLOW_COOKIE,'',0)),('Set-Cookie',cookie(SESSION_COOKIE,'',0))])
            else:raise ApplicationError('OPERATION_NOT_ALLOWED',404)
            return APIResponse(200,dashboard_response(path,result),tuple(headers))
        except ApplicationError as exc:
            return APIResponse(exc.status,{'error':exc.code,'submission_enabled':False},tuple(headers))
        except Exception:
            return APIResponse(503,{'error':'SECURITY_OR_STATE_UNAVAILABLE','submission_enabled':False},tuple(headers))
