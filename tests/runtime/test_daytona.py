"""Provider failures must not turn into an empty or incompatible task world."""
from types import SimpleNamespace

import pytest

from mac_bench.runtime.sandbox.daytona import DaytonaSandboxBackend, _BOOT, _STOP
from mac_bench.runtime.sandbox.base import SandboxHandle, PreparedImage
from mac_bench.runtime.sandbox.providers import provider_name, resolve_config, create_backend


def backend():
    value = DaytonaSandboxBackend.__new__(DaytonaSandboxBackend)
    value._networks = {}
    value._docker = True
    value._cpu, value._memory, value._disk = 4, 8, 10
    value._idle_timeout = 7200
    value._region, value._url = 'us', 'https://app.daytona.io/api'
    return value


def test_provider_is_explicit_and_invalid_name_fails(monkeypatch):
    monkeypatch.delenv('MAC_BENCH_BUSINESS_PROVIDER', raising=False)
    assert provider_name('business') == 'modal'
    monkeypatch.setenv('MAC_BENCH_BUSINESS_PROVIDER', 'daytona')
    assert provider_name('business') == 'daytona'
    monkeypatch.setenv('MAC_BENCH_BUSINESS_PROVIDER', 'typo')
    with pytest.raises(ValueError): provider_name('business')


def test_image_scope_separates_region_resources_and_engine():
    value = backend()
    first = value.image_scope
    for field, change in [('_region','eu'),('_cpu',8),('_memory',16),('_disk',80),('_url','https://other.invalid'),('_docker',False)]:
        other = backend();setattr(other,field,change)
        assert other.image_scope != first


def prepared(value):
    return PreparedImage('daytona','prepared','recipe',value.image_scope)


def test_only_not_found_counts_as_absent():
    daytona = pytest.importorskip('daytona')
    value = backend()
    def missing(name): raise daytona.DaytonaNotFoundError('missing')
    value._client = SimpleNamespace(snapshot=SimpleNamespace(get=missing))
    assert not value.image_exists(prepared(value))
    def denied(name): raise PermissionError('denied')
    value._client.snapshot.get = denied
    with pytest.raises(PermissionError): value.image_exists(prepared(value))


def test_failed_boot_deletes_new_sandbox():
    pytest.importorskip('daytona')
    value = backend();calls=[]
    raw = SimpleNamespace(id='new-sandbox',delete=lambda **kw:calls.append('delete'))
    value._client = SimpleNamespace(create=lambda *a,**kw:raw)
    def broken(sandbox, command, timeout):
        calls.append(command)
        raise RuntimeError('Docker startup failed')
    value._checked = broken
    with pytest.raises(RuntimeError,match='startup'):
        value.create(name='task',prepared=prepared(value))
    assert calls == [_BOOT,'delete']


def test_snapshot_requires_stopped_docker_and_resumes_source_in_order():
    value = backend();calls=[]
    value._checked = lambda sandbox,command,timeout:calls.append(command)
    raw = SimpleNamespace(stop=lambda **kw:calls.append('stop'),
        _experimental_create_snapshot=lambda *a,**kw:calls.append('capture'),
        start=lambda **kw:calls.append('start'))
    value.capture_image(SandboxHandle('id','daytona',raw),recipe='task')
    assert calls == [_STOP,'stop','capture','start',_BOOT]


def test_failed_preparation_never_publishes_snapshot():
    value = backend();calls=[]
    def fail(*args): raise RuntimeError('Docker still running')
    value._checked = fail
    raw = SimpleNamespace(stop=lambda **kw:calls.append('stop'),
        _experimental_create_snapshot=lambda *a,**kw:calls.append('capture'))
    with pytest.raises(RuntimeError):
        value.capture_image(SandboxHandle('id','daytona',raw),recipe='task')
    assert calls == []


def test_workspace_uses_selected_business_provider(monkeypatch):
    import mac_bench.runtime.sandbox.daytona as module
    calls=[]
    monkeypatch.setenv('MAC_BENCH_BUSINESS_PROVIDER','daytona')
    monkeypatch.setattr(module,'DaytonaSandboxBackend',lambda **kw:calls.append(kw) or SimpleNamespace())
    config = resolve_config('business').workspace()
    assert create_backend(config).config == config
    assert calls[0]['cpu']==1 and calls[0]['memory_mib']==1024 and calls[0]['disk_gib']==3


def test_create_timeout_cleans_up_allocated_sandbox_by_unique_name():
    pytest.importorskip('daytona')
    value = backend();seen=[]
    def create(params,**kwargs):
        seen.append(params.name)
        raise TimeoutError('allocation response lost')
    value._client = SimpleNamespace(create=create,get=lambda name:
        SimpleNamespace(delete=lambda **kw:seen.append(name)))
    with pytest.raises(TimeoutError):value.create(name='task',prepared=prepared(value))
    assert len(seen)==2 and seen[0]==seen[1] and seen[0].startswith('task-')


def test_software_snapshot_has_no_docker_dependency():
    value = backend(); value._docker = False
    assert value.image_scope['docker'] is False
    calls = []
    value._checked = lambda sandbox, command, timeout: calls.append(command)
    raw = SimpleNamespace(stop=lambda **kw: calls.append('stop'),
        _experimental_create_snapshot=lambda *a, **kw: calls.append('capture'),
        start=lambda **kw: calls.append('start'))
    value.capture_image(SandboxHandle('id', 'daytona', raw), recipe='dependencies')
    assert calls == ['sync', 'stop', 'capture', 'start']


def test_channel_close_accepts_exited_pty_but_not_provider_errors():
    from threading import Event
    daytona = pytest.importorskip("daytona")
    DaytonaError, DaytonaNotFoundError = daytona.DaytonaError, daytona.DaytonaNotFoundError
    from mac_bench.runtime.sandbox.daytona import _DaytonaChannel
    calls = []
    channel = _DaytonaChannel.__new__(_DaytonaChannel)
    channel.closed = Event()
    def exited():
        try: raise DaytonaNotFoundError('PTY gone')
        except DaytonaNotFoundError as error: raise DaytonaError('kill failed') from error
    channel.pty = SimpleNamespace(kill=exited, disconnect=lambda: calls.append('disconnect'))
    channel.close()
    assert calls == ['disconnect']
    channel.closed = Event()
    def denied(): raise PermissionError('denied')
    channel.pty.kill = denied
    with pytest.raises(PermissionError): channel.close()
    assert calls == ['disconnect', 'disconnect']


def test_offline_exec_and_channel_enter_the_same_network(monkeypatch):
    from mac_bench.runtime.sandbox.network import enter_network
    import mac_bench.runtime.sandbox.daytona as module
    value = backend(); value._docker = False; value._networks = {'id': 123}
    commands = []; files = {}
    raw = SimpleNamespace(process=SimpleNamespace(
        exec=lambda cmd, **kw: commands.append(cmd) or SimpleNamespace(exit_code=0, result='ok'),
        create_pty_session=lambda **kw: 'pty'))
    sandbox = SandboxHandle('id', 'daytona', raw)
    value.exec(sandbox, 'echo hello')
    assert '/proc/123/ns/net' in commands[0]
    value.write_file = lambda sb, *, path, content: files.update({path: content})
    monkeypatch.setattr(module, '_DaytonaChannel', lambda *args: args)
    value.open_channel(sandbox, argv=['python3', '-c', 'print(1)'], cwd='/work')
    assert enter_network(123).encode() in next(iter(files.values()))


def test_directory_upload_preserves_modes_and_uses_one_transfer(tmp_path):
    import io, tarfile, shlex
    source = tmp_path / 'checks'; source.mkdir()
    executable = source / 'check.sh'; executable.write_text('exit 0'); executable.chmod(0o755)
    nested = source / 'nested'; nested.mkdir(); (nested / 'data').write_text('fixture')
    value = backend(); calls = []; contents = []
    value.write_file = lambda sb, *, path, content: contents.append(content)
    value.exec = lambda sb, command, **kw: calls.append(command) or SimpleNamespace(exit_code=0)
    value.upload_tree(None, local_dir=source, remote_dir='/work/grader with spaces')
    assert len(contents) == len(calls) == 1
    assert shlex.quote('/work/grader with spaces') in calls[0]
    with tarfile.open(fileobj=io.BytesIO(contents[0])) as archive:
        assert archive.getmember('./check.sh').mode & 0o111
        assert archive.extractfile('./nested/data').read() == b'fixture'


def test_business_build_preserves_oci_paths_and_separates_shared_blob_uploads(tmp_path):
    pytest.importorskip("daytona")
    common = tmp_path / "common"
    task = tmp_path / "task"
    (common / "blobs/sha256").mkdir(parents=True)
    (task / "blobs/sha256").mkdir(parents=True)
    (common / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    for name in ("a" * 64, "b" * 64):
        (common / "blobs/sha256" / name).write_bytes(name.encode())
    (task / "index.json").write_text('{"schemaVersion":2,"manifests":[]}')
    image = backend().business_image(common, task)
    sources = [context.source_path for context in image._context_list]
    assert set(sources) == {str(p) for p in common.rglob("*") if p.is_file()} | {str(task)}
    assert str(common) not in sources
    dockerfile = image.dockerfile()
    for source in common.rglob("*"):
        if source.is_file():
            assert "/work/environment/images/" + source.relative_to(common).as_posix() in dockerfile
    assert dockerfile.count("COPY ") == 4


@pytest.mark.parametrize('domain', ['business', 'software'])
def test_environment_commands_read_checkout_config_without_a_model(tmp_path, monkeypatch, domain):
    from mac_bench.runtime.sandbox import providers
    monkeypatch.setattr(providers, 'repository_root', lambda: tmp_path)
    monkeypatch.setattr(providers.os, 'environ', {})
    variable = 'MAC_BENCH_' + domain.upper() + '_PROVIDER'
    (tmp_path / '.env').write_text(variable + '=daytona\nDAYTONA_TARGET=eu\n')
    config = resolve_config(domain)
    assert config.provider == 'daytona' and config.target == 'eu'
    providers.os.environ[variable] = 'modal'
    assert provider_name(domain) == 'modal'
