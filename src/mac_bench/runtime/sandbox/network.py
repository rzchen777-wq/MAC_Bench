"""A shared loopback-only network for offline software commands and local services."""

# Keep one network namespace alive independently of any agent command. The
# provider's control process remains outside it, so uploads and PTYs still work.
START_NETWORK = r"""
import ctypes, fcntl, json, os, socket, struct, time
reader, writer = os.pipe()
child = os.fork()
if child == 0:
    os.close(reader)
    os.setsid()
    if os.fork():
        os._exit(0)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(0x40000000) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot create offline network')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            fcntl.ioctl(control.fileno(), 0x8914, struct.pack('16sH', b'lo', 0x49))
        os.write(writer, json.dumps({'pid': os.getpid()}).encode())
    except BaseException as error:
        os.write(writer, json.dumps({'error': str(error)}).encode())
        os._exit(1)
    os.close(writer)
    null = os.open('/dev/null', os.O_RDWR)
    for fd in (0, 1, 2): os.dup2(null, fd)
    if null > 2: os.close(null)
    while True: time.sleep(3600)
os.close(writer)
with os.fdopen(reader) as pipe: result = json.load(pipe)
os.waitpid(child, 0)
if 'error' in result: raise RuntimeError(result['error'])
print(result['pid'])
"""


def enter_network(pid):
    """Enter the shared namespace and remove capabilities that could escape it."""
    pid = int(pid)
    if pid <= 0:
        raise ValueError("Network namespace PID must be positive")
    return f"""import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
fd=os.open('/proc/{pid}/ns/net',os.O_RDONLY)
if libc.setns(fd,0x40000000)!=0:
    raise OSError(ctypes.get_errno(),'Cannot enter offline network')
os.close(fd)
# Removing these bounding capabilities also applies across subsequent execs.
for capability in (12,13,21):
    if libc.prctl(24,capability,0,0,0)!=0:
        raise OSError(ctypes.get_errno(),'Cannot restrict network capabilities')
"""
