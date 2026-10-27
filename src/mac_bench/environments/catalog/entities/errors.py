"""Stable, machine-readable failures of the standalone catalog API."""


class CatalogError(ValueError):
    def __init__(self, code, **details):
        self.code = code
        self.details = details
        super().__init__(code)

    def as_dict(self):
        return {'code': self.code, 'details': self.details}
