"""Reason-coded native infrastructure and binding failures."""


class CompileFailure(Exception):
    """A captured contract or native infrastructure precondition was not satisfied.

    The established exception name and reason codes remain part of the app adapters.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NativeHTTPFailure(CompileFailure):
    """Private response retained for app-aware diagnostics, never in str/logs."""

    def __init__(self, method, status, payload=None):
        super().__init__(f'REST_http_error:{method}:{status}')
        self.status = status
        self.native_payload = payload
        # Application adapters may establish a known pre-write rejection.
        self.outcome = 'unknown'


class NativeGraphQLFailure(CompileFailure):
    def __init__(self, errors):
        super().__init__('twenty_graphql_error_or_partial_result')
        self.native_errors = errors
