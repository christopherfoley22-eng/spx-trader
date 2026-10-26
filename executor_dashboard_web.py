"""Local HTTPS dashboard adapter over sanitized APIs. No broker transport.

No listener is started on import. Serving requires an explicit operator-owned TLS
context and a loopback bind matching the configured authentication origin. Never
trust X-Forwarded-* headers to assert TLS, host or peer identity.
"""
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import ssl
import socket
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from executor_application_api import APIRequest,FoundationAPI

ASSETS=Path(__file__).with_name('dashboard')
SECURITY_HEADERS=(('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),
    ('Referrer-Policy','no-referrer'),('Cross-Origin-Opener-Policy','same-origin'),
    ('Cross-Origin-Resource-Policy','same-origin'),
    ('Permissions-Policy','publickey-credentials-get=(self), publickey-credentials-create=(self), camera=(), microphone=(), geolocation=()'),
    ('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"))


@dataclass(frozen=True)
class WebResponse:
    status:int
    body:bytes
    headers:tuple


class DashboardWebApplication:
    def __init__(self,lifecycle):
        self.api=FoundationAPI(lifecycle)
        self.origin=urlsplit(lifecycle.security.policy.origin)

    def dispatch(self,request):
        import json
        if (not isinstance(request,APIRequest) or request.secure_transport is not True or not request.peer_id
                or request.headers.get('Host')!=self.origin.netloc
                or request.headers.get('Sec-Fetch-Site','none')=='cross-site'
                or request.headers.get('Origin',self.origin.geturl())!=self.origin.geturl()):
            return WebResponse(403,b'Forbidden',SECURITY_HEADERS+(('Content-Type','text/plain; charset=utf-8'),))
        path=urlsplit(request.path)
        if path.scheme or path.netloc or path.fragment:
            return WebResponse(400,b'Invalid path',SECURITY_HEADERS)
        static={'/':'index.html','/dashboard':'index.html','/history':'index.html','/security':'index.html',
            '/assets/dashboard.js':'dashboard.js','/assets/dashboard.css':'dashboard.css','/assets/mountains.svg':'mountains.svg'}
        if request.method=='GET' and request.path in static:
            name=static[request.path]
            mime={'html':'text/html','js':'text/javascript','css':'text/css','svg':'image/svg+xml'}[name.rsplit('.',1)[1]]
            return WebResponse(200,(ASSETS/name).read_bytes(),SECURITY_HEADERS+(('Content-Type',mime+'; charset=utf-8'),))
        if path.path.startswith(('/api/','/auth/')):
            result=self.api.dispatch(request)
            # Preserve secure cookie headers while supplying the dashboard CSP.
            base=tuple((key,value) for key,value in result.headers if key.lower() not in {h[0].lower() for h in SECURITY_HEADERS})
            return WebResponse(result.status,json.dumps(result.body,separators=(',',':'),allow_nan=False).encode(),
                SECURITY_HEADERS+base+(('Content-Type','application/json; charset=utf-8'),))
        return WebResponse(404,b'Not found',SECURITY_HEADERS+(('Content-Type','text/plain; charset=utf-8'),))


class BoundedLocalHTTPSServer(ThreadingHTTPServer):
    """TLS handshakes run in bounded workers, not the accept loop."""
    daemon_threads=True
    def __init__(self,address,handler,tls_context):
        self.tls_context=tls_context;self.capacity=BoundedSemaphore(16)
        self.address_family=socket.AF_INET6 if address[0]=='::1' else socket.AF_INET
        super().__init__(address,handler)

    def process_request(self,request,address):
        if not self.capacity.acquire(blocking=False):request.close();return
        try:super().process_request(request,address)
        except BaseException:
            self.capacity.release();request.close();raise

    def process_request_thread(self,request,address):
        wrapped=None
        try:
            request.settimeout(5)
            wrapped=self.tls_context.wrap_socket(request,server_side=True,do_handshake_on_connect=False)
            wrapped.settimeout(5);wrapped.do_handshake()
            self.finish_request(wrapped,address)
        except Exception:
            pass  # No client data, cookies or raw errors in transport logs.
        finally:
            try:self.shutdown_request(wrapped if wrapped is not None else request)
            finally:self.capacity.release()


def create_https_server(application,address,tls_context):
    """Explicit loopback-only factory; no built-in certificates or auth bypass."""
    if not isinstance(application,DashboardWebApplication) or not isinstance(tls_context,ssl.SSLContext):
        raise ValueError('Explicit dashboard and TLS context required')
    host,port=address
    if host not in {'127.0.0.1','::1'} or application.origin.hostname!='localhost' or application.origin.port!=port:
        raise ValueError('Pinned localhost HTTPS origin and loopback bind required')
    if tls_context.protocol!=ssl.PROTOCOL_TLS_SERVER or tls_context.minimum_version<ssl.TLSVersion.TLSv1_2:
        raise ValueError('TLS server context with modern minimum version required')
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def setup(self):
            self.request.settimeout(5)  # Bounds header parsing as well as body reads.
            super().setup()
        def log_message(self,*args):pass  # Never log cookies, bootstrap, assertions or CSRF.
        def handle_request(self):
            self.connection.settimeout(10)
            critical=('Host','Origin','Cookie','Content-Length','Content-Type','X-CSRF-Token','Sec-Fetch-Site','X-Mortificatio-Session','Authorization')
            if any(len(self.headers.get_all(key,[]))>1 for key in critical) or self.headers.get('Transfer-Encoding'):
                self.send_error(400);self.close_connection=True;return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>65536 or self.command not in {'GET','POST'} or self.command=='GET' and length:
                    raise ValueError
            except ValueError:
                self.send_error(400);self.close_connection=True;return
            # Actual TLS socket and TCP peer, never proxy assertions.
            request=APIRequest(self.command,self.path,{key:self.headers.get(key) for key in critical if self.headers.get(key) is not None},
                self.rfile.read(length),isinstance(self.connection,ssl.SSLSocket),self.client_address[0])
            try:response=application.dispatch(request)
            except Exception:response=WebResponse(503,b'Unavailable',SECURITY_HEADERS)
            self.send_response(response.status)
            for key,value in response.headers:self.send_header(key,value)
            self.send_header('Content-Length',str(len(response.body)));self.end_headers();self.wfile.write(response.body)
        do_GET=handle_request
        do_POST=handle_request
    server=BoundedLocalHTTPSServer(address,Handler,tls_context)
    return server
