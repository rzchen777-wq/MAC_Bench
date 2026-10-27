"""Keep restored benchmark business state free of periodic HRMS housekeeping."""
import json
import shlex
import inspect


def _paused_procfile(content):
    """Retain app services; accept the pinned scheduler or an already paused file."""
    lines = content.decode().splitlines(keepends=True)
    schedules = [i for i, line in enumerate(lines)
                 if line.partition(':')[0].strip() == 'schedule']
    if len(schedules) > 1 or (schedules and lines[schedules[0]].strip() != 'schedule: bench schedule'):
        raise RuntimeError('Unexpected pinned HRMS scheduler configuration')
    after = ''.join(line for i, line in enumerate(lines) if i not in schedules).encode()
    if not any(line.startswith('web:') and 'bench serve' in line for line in after.decode().splitlines()) or not any(line.startswith('worker:') and 'bench worker' in line for line in after.decode().splitlines()):
        raise RuntimeError('HRMS web/worker configuration is missing')
    return after


_PAUSE = 'import hashlib,json,pathlib,subprocess\n' + inspect.getsource(_paused_procfile) + r'''
path=pathlib.Path('/work/benchmark-hrms-Procfile')
subprocess.run(['docker','cp','runtime-hrms:/home/frappe/frappe-bench/Procfile',str(path)],check=True,capture_output=True)
before=path.read_bytes()
after=_paused_procfile(before)
path.write_bytes(after)
subprocess.run(['docker','cp',str(path),'runtime-hrms:/home/frappe/frappe-bench/Procfile'],check=True,capture_output=True)
print(json.dumps({'policy':'hrms_periodic_scheduler_paused_v1','container':'runtime-hrms','before_sha256':hashlib.sha256(before).hexdigest(),'after_sha256':hashlib.sha256(after).hexdigest(),'removed_process':'schedule' if before!=after else None,'already_paused':before==after,'retained_processes':['web','worker']}))
'''


def pause_periodic_hrms(backend, sandbox, spec):
    """Apply after immutable storage restore, before starting any app process.

    Only the periodic scheduler is omitted. Workers still process explicit
    business operations. No database rows, native API code or sealed files
    change. Every restore reapplies the same recorded ephemeral process policy.
    """
    if 'runtime-hrms' not in spec.get('containers', ()):
        return None
    result = backend.exec(sandbox, shlex.join(['python3', '-c', _PAUSE]), timeout=60)
    if result.exit_code:
        raise RuntimeError('Could not pause periodic HRMS housekeeping before resume')
    receipt = json.loads(result.stdout)
    if receipt.get('policy') != 'hrms_periodic_scheduler_paused_v1':
        raise RuntimeError('Invalid HRMS process policy receipt')
    return receipt
