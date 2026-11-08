"""Capture complete candidate command output before returning a bounded preview."""
import json
import shlex
from pathlib import Path
from .command_resources import COMMAND_MEMORY_MIB

OUTPUT_BYTES = 24000


def bounded_output(text):
    """Bound escaped JSON too: control characters can multiply context size."""
    if len(json.dumps(text, ensure_ascii=False).encode()) <= OUTPUT_BYTES:
        return text, False
    low, high = 0, len(text) // 2
    marker = '\n... output abbreviated; full output is saved ...\n'
    while low < high:
        middle = (low + high + 1) // 2
        preview = text[:middle] + marker + text[-middle:]
        if len(json.dumps(preview, ensure_ascii=False).encode()) <= OUTPUT_BYTES:
            low = middle
        else:
            high = middle - 1
    return text[:low] + marker + (text[-low:] if low else ''), True




# This standalone program runs outside the candidate shell's process group.
# A terminal deadline kills that group, leaving the persistent workspace alive.
_COMMAND_SUPERVISOR = Path(__file__).with_name('command_resources.py').read_text() + r"""
import json,os,resource,signal,subprocess,sys,time
from pathlib import Path
command,cwd,log_path,seconds,memory,preview=sys.argv[1:]
seconds=float(seconds);memory=int(memory);preview=int(preview)
Path(log_path).parent.mkdir(parents=True,exist_ok=True)
def limit_data():
    soft,hard=resource.getrlimit(resource.RLIMIT_DATA)
    cap=memory*1024*1024
    if hard!=resource.RLIM_INFINITY:cap=min(cap,hard)
    resource.setrlimit(resource.RLIMIT_DATA,(cap,cap))
reason=None
with open(log_path,'wb') as output:
    process=subprocess.Popen(['sh','-c',command],cwd=cwd,stdout=output,stderr=subprocess.STDOUT,
        start_new_session=True,preexec_fn=limit_data)
    accounting=CommandResources(cwd,process.pid,memory)
    deadline=time.monotonic()+seconds
    try:
        while process.poll() is None or session_members(process.pid):
            reason='timeout' if time.monotonic()>=deadline else accounting.reason()
            if reason:
                stop_session(process.pid,signal.SIGTERM)
                time.sleep(.1)
                stop_session(process.pid,signal.SIGKILL)
                break
            time.sleep(.05)
        code=process.wait()
    finally:
        stop_session(process.pid,signal.SIGKILL)
        process.wait()
        accounting.close()
    if reason:
        output.write(('\n[terminal: '+reason+' limit exceeded; command process group stopped, workspace preserved]\n').encode())
if reason:code=124 if reason=='timeout' else 137
elif code<0:code=128-code
size=Path(log_path).stat().st_size
metadata={'output_bytes':size,'command_memory_limit_mib':memory,'shared_command_memory_limit_mib':memory}
if reason:metadata['resource_limit_exceeded']=reason
sys.stdout.write(json.dumps(metadata)+'\n');sys.stdout.flush()
with open(log_path,'rb') as output:
    if size<=preview:sys.stdout.buffer.write(output.read())
    else:
        sys.stdout.buffer.write(output.read(preview//2))
        sys.stdout.buffer.write(b'\n... output abbreviated; full output is saved ...\n')
        output.seek(-preview//2,2);sys.stdout.buffer.write(output.read())
sys.stdout.flush();sys.exit(code)
"""


def capture_command(command, log_path, *, cwd='/work/project', timeout_seconds=120,
                    memory_limit_mib=COMMAND_MEMORY_MIB):
    if timeout_seconds <= 0 or memory_limit_mib <= 0:
        raise ValueError('Terminal resource limits must be positive')
    return shlex.join(['python3', '-c', _COMMAND_SUPERVISOR, command, cwd, log_path,
                       str(timeout_seconds), str(memory_limit_mib), str(OUTPUT_BYTES)])


def output_result(result, log_path):
    header, separator, output = result.stdout.partition('\n')
    try:
        metadata = json.loads(header)
        size = metadata['output_bytes']
        if not separator or type(size) is not int or size < 0:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        preview, truncated = bounded_output(result.stdout)
        return {'exit_code': result.exit_code, 'output': preview,
                'duration_seconds': result.duration_seconds, 'capture_error': 'missing_output_metadata',
                'output_truncated': truncated}
    output, escaped_truncated = bounded_output(output)
    response = {'exit_code': result.exit_code, 'output': output, 'output_bytes': size,
                'output_truncated': size > OUTPUT_BYTES or escaped_truncated, 'full_output_path': log_path,
                'duration_seconds': result.duration_seconds}
    for key in ['command_memory_limit_mib', 'shared_command_memory_limit_mib', 'resource_limit_exceeded']:
        if key in metadata:
            response[key] = metadata[key]
    if response['output_truncated']:
        response['instruction'] = ('Full stdout/stderr is saved at full_output_path. Search it with grep '
            'or inspect selected lines with sed/head/tail through terminal; avoid printing the whole log. '
            'The recorded exit_code belongs to your command, not the preview.')
    return response
