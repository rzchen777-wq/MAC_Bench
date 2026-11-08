"""Episode-local Messages transport for the native DSH model adapter.

DeepSeek Messages pass through unchanged. OpenRouter calls use LiteLLM's
Messages adapter. Both routes record each provider attempt in the shared ledger.
"""
from __future__ import annotations

import hmac
import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import httpx

from mac_bench.evaluation.episodes.budget import BudgetExceeded


class ProviderResponseError(RuntimeError):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class MessagesUsage:
    """Observe cumulative Messages usage without consuming or changing the stream."""

    def __init__(self):
        self.pending = b''
        self.data = []
        self.tokens = {}
        self.stopped = False
        self.stop_reason = None
        self.error = None

    def feed(self, chunk):
        self.pending += chunk
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            line = line.removesuffix(b'\r')
            if not line:
                self._event()
            elif line.startswith(b'data:'):
                self.data.append(line[5:].removeprefix(b' '))

    def end(self):
        if self.pending:
            self.feed(b'\n')
        self._event()
        if not self.stopped:
            raise ProviderResponseError('Messages stream ended before message_stop')

    def _event(self):
        if not self.data:
            return
        data, self.data = self.data, []
        event = json.loads(b'\n'.join(data))
        kind = event.get('type')
        usage = (event.get('message', {}).get('usage') if kind == 'message_start'
                 else event.get('usage') if kind == 'message_delta' else None)
        if usage is not None:
            for field in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                          'cache_creation_input_tokens'):
                if field in usage:
                    value = usage[field]
                    if type(value) is not int or value < 0:
                        raise ProviderResponseError('Invalid Messages usage: ' + field)
                    self.tokens[field] = value
        if kind == 'message_delta':
            self.stop_reason = event.get('delta', {}).get('stop_reason', self.stop_reason)
        if kind == 'message_stop':
            self.stopped = True
        if kind == 'error':
            self.error = event.get('error', {}).get('type', 'provider_error')

    def usage(self):
        if not self.tokens:
            return None
        # Messages input_tokens excludes cache reads/writes. Match DSH's own
        # totalTokens calculation rather than charging only uncached input.
        prompt = sum(self.tokens.get(field, 0) for field in
                     ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        completion = self.tokens.get('output_tokens', 0)
        return {'prompt_tokens': prompt, 'completion_tokens': completion,
                'total_tokens': prompt + completion, **self.tokens}


def messages_endpoint(base_url):
    """Use DeepSeek's Messages route; custom routes must specify their own root."""
    url = urlsplit(base_url)
    root = base_url.rstrip('/')
    if url.hostname == 'api.deepseek.com' and url.path.rstrip('/') in {'', '/v1'}:
        root = 'https://api.deepseek.com/anthropic'
    if not root.endswith('/v1'):
        root += '/v1'
    return root + '/messages'


class ModelRelay:
    def __init__(self, recording, *, endpoint=None, transport=None, proxy=None):
        self.recording = recording
        self.config = recording.config
        self.endpoint = endpoint or messages_endpoint(self.config.base_url)
        self.token = secrets.token_urlsafe(32)
        self.stop_error = None
        self._active = ContextVar('superagent_provider_attempt', default=None)
        self._litellm = None
        if urlsplit(self.config.base_url).hostname == 'openrouter.ai':
            import litellm
            from litellm.llms.custom_httpx.http_handler import HTTPHandler
            self._litellm = litellm
        self.http = httpx.Client(timeout=self.config.timeout_seconds,
                                 follow_redirects=False, transport=transport,
                                 proxy=proxy,
                                 trust_env=transport is None and proxy is None,
                                 event_hooks={'request': [self._record_request],
                                              'response': [self._record_response]})
        if self._litellm:
            self._http_handler = HTTPHandler(client=self.http)
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                relay._handle(self)

        # Each worker/child can stream independently. server_close joins request
        # threads before the runner seals and evaluates the candidate world.
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = False
        self.thread = Thread(target=self.server.serve_forever, name='superagent-model-relay')
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.http.close()

    def _record_request(self, request):
        active = self._active.get()
        if active is not None:
            attempt, _ = active
            attempt.record['provider_request'] = json.loads(request.content)

    def _record_response(self, response):
        active = self._active.get()
        if active is not None:
            attempt, responses = active
            attempt.record['http_status'] = response.status_code
            responses.append(response)

    @contextmanager
    def _response(self, body, headers, request, attempt):
        if self._litellm is None:
            with self.http.stream('POST', self.endpoint, content=body, headers=headers) as response:
                yield response.status_code, response.headers, response.iter_bytes()
            return

        responses = []
        active = self._active.set((attempt, responses))
        stream = None
        try:
            attempt.record['transport'] = 'litellm_openrouter'
            attempt.record['provider_endpoint'] = self.config.base_url.rstrip('/') + '/chat/completions'
            options = dict(request)
            options.update(model='openrouter/' + self.config.model.removeprefix('openrouter/'),
                api_base=self.config.base_url, api_key=self.config.api_key,
                client=self._http_handler, timeout=self.config.timeout_seconds,
                num_retries=0, max_retries=0, caching=False,
                thinking={'type': self.config.thinking_mode or 'disabled'},
                extra_body={'reasoning': {'effort': 'none'} if self.config.thinking_mode != 'enabled'
                            else {'enabled': True}})
            stream = self._litellm.anthropic.messages.create(**options)
            yield 200, {'content-type': 'text/event-stream'}, stream
        except (BrokenPipeError, ConnectionResetError, BudgetExceeded, ProviderResponseError):
            raise
        except Exception as exc:
            status = getattr(exc, 'status_code', 502)
            if not isinstance(status, int) or not 400 <= status <= 599:
                status = 502
            attempt.record['provider_error_type'] = type(exc).__name__
            raise ProviderResponseError('OpenRouter transport failed', status) from exc
        finally:
            if stream is not None and hasattr(stream, 'close'):
                stream.close()
            for response in responses:
                response.close()
            self._active.reset(active)

    @staticmethod
    def _error(handler, code, message):
        data = json.dumps({'type': 'error', 'error': {
            'type': 'invalid_request_error', 'message': message}}).encode()
        handler.send_response(code)
        handler.send_header('content-type', 'application/json')
        handler.send_header('content-length', str(len(data)))
        handler.send_header('connection', 'close')
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True

    def _handle(self, handler):
        if not hmac.compare_digest(handler.headers.get('x-api-key', ''), self.token):
            return self._error(handler, 401, 'Invalid episode credential')
        if handler.path != '/v1/messages':
            return self._error(handler, 404, 'Use /v1/messages')
        try:
            length = int(handler.headers.get('content-length', '0'))
            body = handler.rfile.read(length)
            request = json.loads(body)
            if request.get('model') != self.config.model or request.get('stream') is not True:
                return self._error(handler, 400, 'Use the episode model with stream=true')
        except (ValueError, TypeError, AttributeError):
            return self._error(handler, 400, 'Invalid Messages request')
        identity = {'agent_id': handler.headers.get('x-deepseek-harness-session-id'),
                    'purpose': 'compaction' if handler.headers.get('x-deepseek-harness-compact') == '1'
                    else 'generation'}
        headers = {'x-api-key': self.config.api_key, 'content-type': 'application/json',
                   'accept': 'text/event-stream', 'accept-encoding': 'identity',
                   'anthropic-version': handler.headers.get('anthropic-version', '2023-06-01')}
        if handler.headers.get('anthropic-beta'):
            headers['anthropic-beta'] = handler.headers['anthropic-beta']
        sent_headers = False
        observer = MessagesUsage()
        try:
            with self.recording.native_attempt(request, protocol='messages', identity=identity) as attempt:
                try:
                    with self._response(body, headers, request, attempt) as (status, response_headers, chunks):
                        attempt.record['http_status'] = status
                        handler.send_response(status)
                        for key in ('content-type', 'retry-after', 'request-id'):
                            if key in response_headers:
                                handler.send_header(key, response_headers[key])
                        handler.send_header('connection', 'close')
                        handler.end_headers()
                        sent_headers = True
                        with attempt.response_path.open('wb') as receipt:
                            for chunk in chunks:
                                receipt.write(chunk)
                                if 200 <= status < 300:
                                    observer.feed(chunk)
                                handler.wfile.write(chunk)
                                handler.wfile.flush()
                        if not 200 <= status < 300:
                            raise ProviderResponseError(f'Provider HTTP {status}', status)
                        observer.end()
                        if observer.error:
                            raise ProviderResponseError(observer.error)
                finally:
                    attempt.usage = observer.usage()
                    attempt.record.update(response_complete=observer.stopped,
                                          finish_reason=observer.stop_reason)
        except BudgetExceeded as exc:
            self.stop_error = exc
            if not sent_headers:
                self._error(handler, 400, 'Episode budget exhausted')
        except (httpx.HTTPError, ProviderResponseError, ValueError, OSError) as exc:
            # DSH sees the original provider status/stream and owns its retry
            # decision. Do not inject another response into an open SSE stream.
            if not sent_headers:
                self._error(handler, getattr(exc, 'status', 502), 'Provider transport failed')
        finally:
            handler.close_connection = True
