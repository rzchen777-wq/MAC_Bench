"""Trusted read adapters; no model-supplied URLs, SQL, mutations or row subsets."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass

from mac_bench.environments.verification.contracts import digest
from mac_bench.environments.verification.errors import VerificationError
from mac_bench.environments.verification.evaluation import evaluate


@dataclass(frozen=True)
class ReadSource:
    name: str
    read: object
    identity_field: str
    scope: dict
    includes_new_records: bool = True

    def capture(self, max_rows=100000):
        rows = self.read()
        if not isinstance(rows, list) or len(rows) > max_rows:
            raise VerificationError('Read source exceeded inventory limit: '+self.name)
        keys = []
        for row in rows:
            if not isinstance(row, dict) or self.identity_field not in row:
                raise VerificationError('Missing native identity: '+self.name)
            identity = row[self.identity_field]
            if identity is None or isinstance(identity, (dict,list,bool)):
                raise VerificationError('Invalid native identity: '+self.name)
            keys.append(digest(identity))
        if len(keys) != len(set(keys)):
            raise VerificationError('Duplicate native inventory identity: '+self.name)
        return [deepcopy(row) for _,row in sorted(zip(keys,rows),key=lambda x:x[0])]


def pretix_source(name, client, event, collection, identity):
    return ReadSource(name, lambda:client.listing(client.path(event,collection+'/'),identity=identity),
                      identity, {'app':'pretix','organizer_root':client.root,'event':event,'collection':collection})


def frappe_source(name, client, doctype, filters):
    frozen=deepcopy(filters)
    def read():
        return [client.read(doctype,key) for key in client.list_names(doctype,filters=deepcopy(frozen))]
    return ReadSource(name,read,'name',{'app':'hrms','doctype':doctype,'filters':frozen})


def fixed_source(name, factories, objects, plans):
    """Only for fixed reference objects; cannot discover new transactional rows."""
    handles=deepcopy(objects)
    def read():
        rows=[]
        for key,obj in handles.items():
            value=factories.read(plans[key]['factory'],obj['identity']).data
            raw=value.get('record',value)
            if '_object_key' in raw:raise VerificationError('Reserved identity field collision')
            rows.append({**raw,'_object_key':key})
        return rows
    return ReadSource(name,read,'_object_key',{'kind':'fixed_references','keys':sorted(handles)},False)


def twenty_source(name, client, factories, factory, query_name, filters, *, max_pages=1000, max_rows=100000):
    args=client.schema.query_type.fields[query_name].args
    query=f'query Inventory($filter:{args["filter"].type},$after:{args["after"].type}){{{query_name}(filter:$filter,after:$after,first:100){{edges{{node{{id}}}}pageInfo{{hasNextPage endCursor}}}}}}'
    frozen=deepcopy(filters)
    def read():
        after=None;seen=set();result=[]
        for _ in range(max_pages):
            page=client.query(query,{'filter':deepcopy(frozen),'after':after})[query_name]
            for edge in page['edges']:
                identity=edge['node']['id']
                if identity in seen:raise VerificationError('Repeated GraphQL inventory page')
                seen.add(identity)
                if len(seen)>max_rows:raise VerificationError('GraphQL inventory row limit')
                data=factories.read(factory,{'id':identity}).data
                result.append(data.get('record',data))
            if not page['pageInfo']['hasNextPage']:return result
            cursor=page['pageInfo']['endCursor']
            if not cursor or cursor==after:raise VerificationError('Nonadvancing GraphQL cursor')
            after=cursor
        raise VerificationError('GraphQL inventory page limit')
    return ReadSource(name,read,'id',{'app':'twenty','query':query_name,'filter':frozen,'factory':factory})


def bigcapital_source(name, transport, path, records_key, *, scope_filter=None, scope_description=None,
                      max_pages=1000, max_rows=100000):
    """path/filter are trusted deployment configuration, never model output."""
    if not path.startswith('api/') or '?' in path or '..' in path:
        raise VerificationError('Invalid trusted collection path')
    def read():
        rows=[];seen=set();total=None
        for page in range(1,max_pages+1):
            data=transport.request('GET',path+f'?page={page}&pageSize=100')
            batch=data[records_key];pagination=data['pagination'];count=pagination['total']
            if type(count) is not int or count<0 or count>max_rows or not isinstance(batch,list) or pagination['page']!=page:
                raise VerificationError('Invalid accounting pagination')
            if total is not None and total!=count:raise VerificationError('Changing accounting inventory')
            total=count
            for row in batch:
                if row['id'] in seen:raise VerificationError('Repeated accounting page')
                seen.add(row['id']);rows.append(row)
            if len(rows)==total:return [r for r in rows if scope_filter is None or scope_filter(r)]
            if not batch or len(rows)>total:raise VerificationError('Incomplete accounting inventory')
        raise VerificationError('Accounting inventory page limit')
    return ReadSource(name,read,'id',{'app':'bigcapital','path':path,'filter':scope_description})


class NativeObserver:
    def __init__(self, sources):
        self.sources={s.name:s for s in sources}
        if len(self.sources)!=len(sources):raise VerificationError('Duplicate source registration')

    def capture(self, *, barrier=None):
        # Without an application write barrier this detects drift, not snapshot isolation.
        with barrier if barrier is not None else nullcontext():
            first={name:source.capture() for name,source in self.sources.items()}
            second={name:source.capture() for name,source in self.sources.items()}
            if digest(first)!=digest(second):raise VerificationError('Native state changed during collection')
        return first, {'scope':{name:s.scope for name,s in self.sources.items()},
                       'consistency':'write_barrier_and_double_read' if barrier is not None else 'stable_double_read',
                       'record_digest':digest(first)}

    def evaluate(self, contract, *, barrier=None):
        try:
            if set(contract['sources'])!=set(self.sources):raise VerificationError('Observer collection coverage mismatch')
            for name,card in contract['sources'].items():
                if card.get('requires_discovery',True) and not self.sources[name].includes_new_records:
                    raise VerificationError('Fixed reader cannot cover new records: '+name)
                if card.get('read_scope') is None:
                    raise VerificationError('Native read scope has not been frozen: '+name)
                if card['read_scope']!=self.sources[name].scope:
                    raise VerificationError('Native read scope changed: '+name)
            state,evidence=self.capture(barrier=barrier)
            return {**evaluate(contract,state,provenance='trusted_native_readback'),'observation':evidence}
        except Exception as exc:
            return {'status':'observation_error','complete':False,'task_success':None,'gc':None,
                    'error':str(exc),'native_execution_verified':False}
