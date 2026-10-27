"""Configured LiteLLM embeddings, with no implicit generative-model credentials."""
from dataclasses import dataclass
import os
from typing import Mapping
import numpy as np
from .config import _resolve_env_ref


@dataclass(frozen=True, repr=False)
class EmbeddingConfig:
    base_url: str
    model_name: str
    api_key: str | None = None
    timeout_seconds: float = 120
    batch_size: int = 32
    dimensions: int | None = None
    query_instruction: str = ''

    @classmethod
    def from_mapping(cls, mapping, *, environ: Mapping[str, str] | None = None):
        if environ is None:
            from dotenv import load_dotenv
            load_dotenv()
        env = os.environ if environ is None else environ
        m = dict(mapping or {})
        base = (_resolve_env_ref(m.get('base_url'), env) or env.get('EVOTEAM_EMBEDDING_BASE_URL')
                or 'https://api.openai.com/v1').rstrip('/')
        model = (_resolve_env_ref(m.get('model_name') or m.get('model'), env)
                 or env.get('EVOTEAM_EMBEDDING_MODEL') or '')
        key = (_resolve_env_ref(m.get('api_key'), env)
               or env.get(str(m.get('api_key_env') or 'EVOTEAM_EMBEDDING_API_KEY')))
        timeout = float(m.get('timeout_seconds', 120))
        batch = int(m.get('batch_size', 32))
        dims = m.get('dimensions')
        if not base.startswith(('http://', 'https://')) or not model:
            raise ValueError('Set embedding.base_url and embedding.model_name (or EVOTEAM_EMBEDDING_* variables)')
        if timeout <= 0 or batch <= 0 or (dims is not None and (type(dims) is not int or dims <= 0)):
            raise ValueError('Embedding timeout, batch_size and optional dimensions must be positive')
        return cls(base, model, key, timeout, batch, dims, str(m.get('query_instruction') or ''))

    def public_dict(self):
        return {'gateway':'litellm.embedding','base_url':self.base_url,'model_name':self.model_name,
                'api_key_configured':bool(self.api_key),'timeout_seconds':self.timeout_seconds,
                'batch_size':self.batch_size,'dimensions':self.dimensions,
                'query_instruction':self.query_instruction}

    def __repr__(self):
        return f'EmbeddingConfig({self.public_dict()!r})'


class LiteLLMEmbeddingClient:
    def __init__(self, config: EmbeddingConfig, *, record=lambda value: None):
        self.config = config
        self.record = record
        self.dimension = config.dimensions

    def encode(self, texts, batch_size=None):
        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        if any(not isinstance(s, str) for s in inputs):
            raise TypeError('Embedding inputs must be strings')
        if not inputs:
            return np.empty((0, self.dimension or 0), dtype=float)
        size = min(self.config.batch_size, int(batch_size or self.config.batch_size))
        if size <= 0:
            raise ValueError('Embedding batch_size must be positive')
        vectors = []
        from . import gateway
        for start in range(0, len(inputs), size):
            batch = [self.config.query_instruction + s for s in inputs[start:start+size]]
            self.record({'status':'starting','input_count':len(batch),'configuration':self.config.public_dict()})
            try:
                response = gateway.embed(config=self.config, inputs=batch)
                data = response['data'] if isinstance(response, dict) else response.data
                def field(row, key):
                    return row[key] if isinstance(row, dict) else getattr(row, key)
                by_index = {field(row,'index'):field(row,'embedding') for row in data}
                if len(data) != len(batch) or set(by_index) != set(range(len(batch))):
                    raise ValueError('Embedding response indices do not match inputs')
                array = np.asarray([by_index[i] for i in range(len(batch))], dtype=float)
                if array.ndim != 2 or array.shape[1] == 0 or not np.isfinite(array).all():
                    raise ValueError('Embedding response must contain finite vectors')
                if self.dimension is None:
                    self.dimension = array.shape[1]
                if array.shape[1] != self.dimension:
                    raise ValueError('Embedding dimensions changed or differ from configuration')
                usage = response.get('usage',{}) if isinstance(response,dict) else response.usage
                usage = usage.model_dump() if hasattr(usage,'model_dump') else dict(usage or {})
                self.record({'status':'completed','input_count':len(batch),'dimensions':self.dimension,'usage':usage})
                vectors.extend(array)
            except Exception as exc:
                self.record({'status':'error','error_type':type(exc).__name__,'input_count':len(batch)})
                raise
        array = np.asarray(vectors)
        return array[0] if single else array
