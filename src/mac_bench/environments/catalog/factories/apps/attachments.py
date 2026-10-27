"""Real multipart attachment creation and independent binary content readback."""
import base64
from hashlib import sha256

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.native import (
    NativeAdapter,
    object_schema,
    parameter_schema,
)


def recipe(context, binding):
    from mac_bench.environments.catalog.factories.apps.bigcapital import route
    values = object_schema({'filename': {'type': 'string', 'minLength': 1},
                            'content_type': {'type': 'string', 'minLength': 1},
                            'content_base64': {'type': 'string'}}, ['filename', 'content_type', 'content_base64'])
    native = {'kind': 'attachment', 'identity_fields': ['id'], 'input_schema': values,
              'create_path': route(binding, 'uploadAttachment')['path_template'],
              'read_path': route(binding, 'getAttachment')['path_template']}
    output = object_schema({'id': {'type': ['string', 'integer']}, 'content_base64': {'type': 'string'},
                            'sha256': {'type': 'string'}, 'bytes': {'type': 'integer'}},
                           ['id', 'content_base64', 'sha256', 'bytes'])
    spec = context.spec(binding, 'uploadAttachment', 'getAttachment', recipe=native,
                        parameters=parameter_schema(values), output=output)
    return [spec], {'status': 'factory', 'creation': 'native multipart upload with binary GET content verification'}


class AttachmentAdapter(NativeAdapter):
    def validate(self, payload, parents):
        super().validate(payload, parents)
        if any(any(char in payload['values'][key] for char in ('\r', '\n', '"'))
               for key in ('filename', 'content_type')):
            raise CatalogError('attachment_multipart_header_invalid')
        try:
            base64.b64decode(payload['values']['content_base64'], validate=True)
        except (ValueError, TypeError):
            raise CatalogError('attachment_content_base64_invalid') from None

    def create(self, payload, parents):
        values = payload['values']
        response = self.transport.request_multipart(self.recipe['create_path'], field='file',
            filename=values['filename'], content_type=values['content_type'],
            content=base64.b64decode(values['content_base64'], validate=True))
        if not isinstance(response.body, dict) or not isinstance(response.body.get('data'), dict):
            raise CatalogError('attachment_creation_receipt_invalid')
        return response.body['data']

    def read(self, identity, parents):
        from mac_bench.environments.catalog.factories.apps.bigcapital import (
            render,
        )
        response = self.transport.request_binary('GET', render(self.recipe['read_path'], identity))
        content = response.body
        return {'id': identity['id'], 'content_base64': base64.b64encode(content).decode(),
                'sha256': sha256(content).hexdigest(), 'bytes': len(content)}

    def verify(self, payload, record, parents):
        super().verify(payload, record, parents)
        expected = base64.b64decode(payload['values']['content_base64'], validate=True)
        if record['sha256'] != sha256(expected).hexdigest() or record['bytes'] != len(expected):
            raise CatalogError('attachment_content_readback_mismatch')
