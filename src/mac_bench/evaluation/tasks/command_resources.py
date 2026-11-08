"""Shared command-session memory accounting for terminal and process tools.

The source is embedded in sandbox workers, which require only the standard library.
"""
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import signal

COMMAND_MEMORY_MIB = 6144


def session_usage(sessions):
    rows = {session: [] for session in sessions}
    page_size = os.sysconf('SC_PAGE_SIZE')
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            values = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
            session = int(values[3])
            if session in rows and values[0] != 'Z':
                rows[session].append((int(entry.name), int(values[21]) * page_size))
        except (OSError, ValueError, IndexError):
            continue
    return rows


def session_members(session):
    return session_usage({session})[session]


def stop_session(session, sig):
    for pid, _ in session_members(session):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


class CommandResources:
    """One shared budget per task workspace, including all command descendants."""
    def __init__(self, cwd, session, memory_mib=COMMAND_MEMORY_MIB):
        workspace = Path(cwd).resolve()
        self.directory = (workspace.parent / '.mac-bench-command-resources'
                          / sha256(str(workspace).encode()).hexdigest()[:16])
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.limit = memory_mib * 1024 * 1024
        self.registration = self.directory / str(session)
        self.stopping = self.directory / ('stopping-' + str(session))
        self.registration.touch()

    def reason(self):
        with (self.directory / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            sessions = {int(p.name) for p in self.directory.iterdir() if p.name.isdigit()}
            sessions.add(self.session)
            members = session_usage(sessions)
            usage = {session: sum(size for _, size in rows) for session, rows in members.items()}
            # Remove records left by terminated supervisors without touching
            # sessions whose children are still running.
            for session, rows in members.items():
                if not rows and session != self.session:
                    (self.directory / str(session)).unlink(missing_ok=True)
                    (self.directory / ('stopping-' + str(session))).unlink(missing_ok=True)
            reason = None
            if usage[self.session] > self.limit:
                reason = 'memory'
            elif sum(usage.values()) > self.limit:
                stopping = any(rows and (self.directory / ('stopping-' + str(session))).exists()
                               for session, rows in members.items())
                if not stopping and max(usage, key=lambda session: (usage[session], session)) == self.session:
                    reason = 'shared_memory'
            if reason:
                self.stopping.touch()
            return reason

    def close(self):
        self.registration.unlink(missing_ok=True)
        self.stopping.unlink(missing_ok=True)
