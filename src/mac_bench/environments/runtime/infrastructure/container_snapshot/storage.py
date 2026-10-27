"""Owned mount archives; preserve links, ownership, modes, ACLs and xattrs.

GNU tar runs only against stopped, explicitly owned application storage. The
snapshot store is outside every mount. Tar stderr and file contents stay private.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from hashlib import sha256
from pathlib import Path

from .engine import (
    SnapshotError,
)


def digest_file(path):
    result = sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return sha256(raw).hexdigest()


def overlaps(left, right):
    left, right = Path(left).resolve(), Path(right).resolve()
    return left == right or left in right.parents or right in left.parents


def check_tree(source):
    source = Path(source)
    if source.is_symlink() or not (source.is_dir() or source.is_file()):
        raise SnapshotError('container_snapshot_mount_root_not_regular')
    device = source.stat().st_dev
    if source.is_file() and source.stat().st_nlink != 1:
        raise SnapshotError('container_snapshot_mount_has_external_hardlink')
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        # mountinfo escapes space/tab/newline/backslash in its path field.
        mounted = Path(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), line.split()[4]))
        if source in mounted.parents:
            raise SnapshotError('container_snapshot_mount_contains_nested_filesystem')
    links = {}
    # Do not follow symlinks; their targets may refer to other container paths.
    for parent, directories, files in os.walk(source, followlinks=False):
        for name in [*directories, *files]:
            path = Path(parent) / name
            mode = path.lstat().st_mode
            if not any(test(mode) for test in (stat.S_ISDIR, stat.S_ISREG, stat.S_ISLNK)):
                raise SnapshotError('container_snapshot_mount_has_special_entry')
            if not path.is_symlink() and path.stat().st_dev != device:
                raise SnapshotError('container_snapshot_mount_contains_nested_filesystem')
            if stat.S_ISREG(mode):
                info = path.stat()
                entry = links.setdefault((info.st_dev, info.st_ino), [0, info.st_nlink])
                entry[0] += 1
    if any(observed != total for observed, total in links.values()):
        raise SnapshotError('container_snapshot_mount_has_external_hardlink')


def run_tar(arguments, timeout):
    result = subprocess.run(['tar', *arguments], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        raise SnapshotError('container_snapshot_tar_failed')


def archive(source, destination, timeout):
    source, destination = Path(source), Path(destination)
    check_tree(source)
    run_tar(['--format=pax', '--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime', '--sort=name',
             '--numeric-owner', '--acls', '--xattrs', '--xattrs-include=*', '--selinux',
             '-cpf', str(destination), '-C', str(source.parent), '--', source.name], timeout)
    os.chmod(destination, 0o600)
    with destination.open('rb') as stream:
        os.fsync(stream.fileno())
    return digest_file(destination)


def capture_mount(source, directory, timeout):
    destination = directory / 'contents.tar'
    first = archive(source, destination, timeout)
    second = directory / 'repeat.tar'
    if archive(source, second, timeout) != first:
        raise SnapshotError('container_snapshot_storage_changed_while_quiesced')
    second.unlink()
    return {'sha256': first, 'kind': 'directory' if Path(source).is_dir() else 'file'}


def verify_mount(directory, receipt):
    if digest_file(directory / 'contents.tar') != receipt['sha256']:
        raise SnapshotError('container_snapshot_mount_archive_changed')


def restore_mount(source, directory, receipt, timeout):
    source = Path(source)
    verify_mount(directory, receipt)
    check_tree(source)
    if (receipt['kind'] == 'directory') != source.is_dir():
        raise SnapshotError('container_snapshot_mount_root_type_changed')
    # Keep a mountpoint's root inode in place. Consumers are all stopped. A
    # partial extraction is an unresolved restore, never an automatic retry.
    if source.is_dir():
        for child in source.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        source.unlink()
    run_tar(['--numeric-owner', '--same-owner', '--acls', '--xattrs', '--xattrs-include=*', '--selinux',
             '-xpf', str(directory / 'contents.tar'), '-C', str(source.parent)], timeout)
    observed = directory / ('restored-' + uuid.uuid4().hex + '.tar')
    if archive(source, observed, timeout) != receipt['sha256']:
        raise SnapshotError('container_snapshot_restored_mount_mismatch')
    observed.unlink()
