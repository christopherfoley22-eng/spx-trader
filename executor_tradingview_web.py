"""Loopback-only TradingView research ingress; no Executor/control routes.

A separately validated HTTPS ingress must authenticate TradingView and inject
this private relay Bearer token. Never expose this HTTP listener publicly.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3

from executor_tradingview import MAX_BODY, TradingViewError, TradingViewInbox


def make_handler(inbox):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'MortificatioResearchIngress'

        def setup(self):
            super().setup()
            self.connection.settimeout(1)

        def log_message(self, *_):
            pass  # Never log auth headers, request URLs, or alert bodies.

        def send_json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if (self.client_address[0] != '127.0.0.1' or len(self.headers.get_all('Host', [])) != 1
                    or self.headers.get('Host') != '127.0.0.1:{}'.format(self.server.server_port)):
                raise TradingViewError('LOOPBACK_HOST_REQUIRED', 403)
            if len(self.headers.get_all('Authorization', [])) != 1:
                raise TradingViewError('AUTHENTICATION_REQUIRED', 401)
            inbox.authenticate(self.headers.get('Authorization'))

        def do_GET(self):
            try:
                self.authorized()
                if self.path != '/status':
                    raise TradingViewError('NOT_FOUND', 404)
                self.send_json(200, inbox.status())
            except TradingViewError as exc:
                self.send_json(exc.status, {'error': exc.code, 'executable': False})
            except Exception:
                self.send_json(503, {'error': 'INBOX_UNAVAILABLE', 'executable': False})

        def do_POST(self):
            try:
                self.authorized()
                if self.path != '/webhooks/tradingview':
                    raise TradingViewError('NOT_FOUND', 404)
                if self.headers.get('Transfer-Encoding') is not None:
                    raise TradingViewError('TRANSFER_ENCODING_NOT_SUPPORTED')
                types = self.headers.get_all('Content-Type', [])
                if len(types) != 1 or types[0].lower() not in ('application/json', 'application/json; charset=utf-8'):
                    raise TradingViewError('JSON_REQUIRED', 415)
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_BODY:
                    raise TradingViewError('INVALID_BODY_SIZE', 413)
                data = self.rfile.read(int(lengths[0]))
                if len(data) != int(lengths[0]):
                    raise TradingViewError('INCOMPLETE_BODY')
                result = inbox.ingest(data, self.headers.get('Authorization'))
                code = 201 if result['outcome'] == 'ACCEPTED' else 200 if result['outcome'] == 'DUPLICATE' else 409 if result['outcome'] == 'CONFLICT' else 202
                self.send_json(code, result)
            except TradingViewError as exc:
                self.send_json(exc.status, {'error': exc.code, 'executable': False})
            except Exception:
                self.send_json(503, {'error': 'INBOX_UNAVAILABLE_NO_DURABLE_ACK', 'executable': False})

    return Handler


def make_server(inbox, port=8766):
    return ThreadingHTTPServer(('127.0.0.1', port), make_handler(inbox))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--stream-id', default='spx-1m-close-v1')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    token = os.environ.get('MORTIFICATIO_TV_RELAY_TOKEN')
    inbox = TradingViewInbox(args.journal, token, stream_id=args.stream_id)
    server = make_server(inbox, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
