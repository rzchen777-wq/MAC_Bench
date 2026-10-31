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


def _bounded_pretix(content):
    """Size preinitialized Pretix for tool traffic, not the host CPU count."""
    text = content.decode()
    replacements = {
        'AUTOMIGRATE=${AUTOMIGRATE:-yes}': 'AUTOMIGRATE=skip',
        'export NUM_WORKERS=${NUM_WORKERS:-$NUM_WORKERS_DEFAULT}': 'export NUM_WORKERS=2',
        'exec celery -A pretix.celery_app worker -l info "$@"':
            'exec celery -A pretix.celery_app worker -l info --concurrency=2 "$@"',
    }
    for before, after in replacements.items():
        if text.count(before) != 1:
            raise RuntimeError('Pretix runtime configuration differs from prepared image')
        text = text.replace(before, after)
    return text.encode()


_PRETIX = 'import json,pathlib,re,subprocess\n' + inspect.getsource(_bounded_pretix) + r'''
root=pathlib.Path('/work/pretix-process-config');root.mkdir(exist_ok=True)
for remote,local in [('/usr/local/bin/pretix','pretix'),('/etc/nginx/nginx.conf','nginx.conf')]:
    path=root/local
    subprocess.run(['docker','cp','m6-pretix:'+remote,str(path)],check=True,capture_output=True)
    if local=='pretix':
        path.write_bytes(_bounded_pretix(path.read_bytes()))
    else:
        content,count=re.subn(r'(?m)^worker_processes\s+auto;', 'worker_processes 1;',path.read_text())
        if count!=1:raise RuntimeError('Pretix nginx worker configuration differs')
        path.write_text(content)
    subprocess.run(['docker','cp',str(path),'m6-pretix:'+remote],check=True,capture_output=True)
print(json.dumps({'policy':'pretix_api_processes','web_workers':2,'task_workers':2,'nginx_workers':1,'schema':'prepared'}))
'''


def configure_pretix_processes(backend, sandbox, spec):
    """Apply to restored, stopped containers with an initialized database."""
    if 'm6-pretix' not in spec.get('containers', ()):
        return None
    result=backend.exec(sandbox,shlex.join(['python3','-c',_PRETIX]),timeout=60)
    if result.exit_code:
        raise RuntimeError('Could not configure Pretix API processes')
    return json.loads(result.stdout)


_HRMS_SHIFT_POLICY = r'''
import json
import frappe
frappe.init(site='hrms.localhost', sites_path='/home/frappe/frappe-bench/sites')
frappe.connect()
try:
    before = int(frappe.db.get_single_value('HR Settings', 'allow_multiple_shift_assignments') or 0)
    if before != 1:
        frappe.db.set_single_value('HR Settings', 'allow_multiple_shift_assignments', 1)
        frappe.clear_cache(doctype='HR Settings')
        frappe.db.commit()
    after = int(frappe.db.get_single_value('HR Settings', 'allow_multiple_shift_assignments') or 0)
    if after != 1:
        raise RuntimeError('Same-day nonoverlapping shift support is unavailable')
    print(json.dumps({'policy':'hrms_nonoverlapping_same_day_shifts_v1',
        'setting':'allow_multiple_shift_assignments','before':before,'after':after,
        'native_overlap_validation':'unchanged'}))
finally:
    frappe.destroy()
'''


def configure_hrms_shifts(backend, sandbox, spec):
    """Enable the documented scheduling prerequisite on load and every reset.

    This native HR setting permits separate, nonoverlapping duties on one day.
    Native overlap validation stays enabled. No employee, qualification, leave,
    existing duty or task business record is edited.
    """
    if 'runtime-hrms' not in spec.get('containers', ()):
        return None
    command = shlex.join(['docker','exec','-u','frappe','-w','/home/frappe/frappe-bench/sites',
        'runtime-hrms','/home/frappe/frappe-bench/env/bin/python','-c',_HRMS_SHIFT_POLICY])
    result = backend.exec(sandbox, command, timeout=60)
    if result.exit_code:
        raise RuntimeError('Could not configure native HRMS same-day shifts')
    receipt = json.loads(result.stdout)
    if receipt.get('after') != 1 or receipt.get('policy') != 'hrms_nonoverlapping_same_day_shifts_v1':
        raise RuntimeError('Invalid native HRMS scheduling receipt')
    return receipt
