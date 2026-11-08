"""Shared HTTP transport with native request journaling and no implicit retries."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from mac_bench.environments.errors import CompileFailure, NativeHTTPFailure


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class RestResponse:
    status: int
    body: object
    journal_id: str | None = None


class RestTransport:
    def __init__(self, base_url, *, headers, timeout=30, journal=None, offline=False,
                 min_request_interval=0):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('invalid REST base URL')
        self.base = base_url.rstrip('/') + '/'
        self.headers = dict(headers)
        self.timeout = timeout
        self.journal = journal
        self.offline = offline
        if (type(min_request_interval) not in (int, float)
                or not math.isfinite(min_request_interval) or min_request_interval < 0):
            raise ValueError('invalid minimum request interval')
        self.min_request_interval = min_request_interval
        self._next_request_at = {}
        self._pace_lock = Lock()
        self.opener = build_opener(_NoRedirect())

    def _pace(self, method, rate_limit_key=None):
        """Honor a declared endpoint bucket, or the transport's method bucket.

        A bucket describes a server handler, never a record ID or query value.
        Reserving slots under the lock keeps concurrent calls spaced without
        making unrelated buckets wait on a sleeping caller.
        """
        if not self.min_request_interval:
            return
        with self._pace_lock:
            key = method, rate_limit_key
            now = time.monotonic()
            slot = max(now, self._next_request_at.get(key, now))
            self._next_request_at[key] = slot + self.min_request_interval
        if slot > now:
            time.sleep(slot - now)

    def request(self, method, path, body=None, *, rate_limit_key=None):
        return self.request_response(method, path, body, rate_limit_key=rate_limit_key).body

    def request_response(self, method, path, body=None, *, rate_limit_key=None):
        return self._exchange(method, path, None if body is None else json.dumps(body, allow_nan=False).encode(),
                              content_type='application/json', journal_body=body, binary=False,
                              rate_limit_key=rate_limit_key)

    def request_binary(self, method, path):
        return self._exchange(method, path, None, content_type='application/json',
                              journal_body=None, binary=True)

    def request_multipart(self, path, *, field, filename, content_type, content):
        if (not isinstance(content, bytes) or any(not isinstance(value, str) or not value
                or any(c in value for c in ('\r', '\n', '"'))
                for value in (field, filename, content_type))):
            raise ValueError('invalid multipart file')
        boundary = 'mac-bench-' + uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                  f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode()
        data = prefix + content + f'\r\n--{boundary}--\r\n'.encode()
        return self._exchange('POST', path, data, content_type='multipart/form-data; boundary=' + boundary,
            journal_body={'file': {'field': field, 'filename': filename, 'content_type': content_type,
                                  'bytes': len(content), 'sha256': sha256(content).hexdigest()}}, binary=False)

    def _exchange(self, method, path, data, *, content_type, journal_body, binary, rate_limit_key=None):
        if self.offline:
            raise CompileFailure('archive_only_transport_cannot_issue_requests')
        url = urljoin(self.base, path)
        source, target = urlsplit(self.base), urlsplit(url)
        if (source.scheme, source.netloc) != (target.scheme, target.netloc):
            raise CompileFailure('REST_request_changes_origin')
        self._pace(method.upper(), rate_limit_key)
        request = Request(url, method=method, data=data,
                          headers={**self.headers, 'Content-Type': content_type,
                                   'Accept': '*/*' if binary else 'application/json'})
        journal_id = self.journal.begin(method=method, url=url, body=journal_body) if self.journal else None
        status = None
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                payload = response.read()
                result = RestResponse(status, payload if binary else json.loads(payload) if payload else None, journal_id)
        except HTTPError as exc:
            # Retain bounded JSON privately for app-aware field diagnostics.
            # Raw error text, headers and tracebacks never enter str or journals.
            payload = None
            try:
                raw = exc.read(65537)
                if len(raw) <= 65536:
                    payload = json.loads(raw)
            except (ValueError, OSError):
                pass
            if self.journal:
                self.journal.finish(journal_id, state='http_error', status=exc.code)
            raise NativeHTTPFailure(method, exc.code, payload) from None
        except Exception as exc:
            if self.journal:
                self.journal.finish(journal_id, state='transport_or_decode_error', status=status,
                                    error_type=type(exc).__name__)
            raise CompileFailure('REST_transport_or_decode_error_outcome_unresolved') from None
        if self.journal:
            self.journal.finish(journal_id, state='response_received', status=result.status, body=({'bytes': len(result.body), 'sha256': sha256(result.body).hexdigest()}
                                                               if binary else result.body))
        return result
