"""Locked, atomic recovery state for explicitly selected named snapshot cleanup."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
from sandbox_backend import identifier, snapshot_identifier, timestamp


@contextmanager
def run_lock(path=None, enabled=True):
    if not enabled:
        yield
        return
    endpoint = os.environ.get('E2B_API_URL') or f"https://api.{os.environ.get('E2B_DOMAIN') or 'e2b.app'}"
    identity = json.dumps([os.environ.get('E2B_API_KEY'), endpoint.rstrip('/')])
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
    path = Path(path) if path else Path(tempfile.gettempdir()) / f'e2b-cleanup-{os.getuid()}-{suffix}.lock'
    with path.open('a') as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another cleanup is running with these credentials and endpoint') from None
        yield


class Journal:
    def __init__(self, path, scope, writable):
        # Equivalent numeric CLI arguments must select the same recovery file.
        scope = {**scope, 'days': float(scope['days']) if scope['days'] else 0.0}
        key = os.environ.get('E2B_API_KEY')
        if not key:
            raise ValueError('E2B_API_KEY is required for named snapshot recovery, including the CLI backend')
        domain = os.environ.get('E2B_DOMAIN') or 'e2b.app'
        endpoint = (os.environ.get('E2B_API_URL') or f'https://api.{domain}').rstrip('/')
        identity = json.dumps([key, endpoint])
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.scope = scope
        suffix = hashlib.sha256(json.dumps([self.identity, scope], sort_keys=True).encode()).hexdigest()[:20]
        self.path = Path(path) if path else Path(__file__).with_name(f'.cleanup-journal-{suffix}.json')
        self.writable, self.lock = writable, None
        self.data = {'schema_version': 1, 'identity': self.identity, 'scope': scope, 'pending': {}}

    def __enter__(self):
        if self.writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.lock = open(str(self.path) + '.lock', 'a')
            os.chmod(self.lock.name, 0o600)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock.close()
                raise ValueError('another cleanup is using this journal') from None
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict):
                    raise ValueError('invalid recovery journal')
                if data.get('schema_version') != 1 or data.get('identity') != self.identity or data.get('scope') != self.scope:
                    raise ValueError('journal belongs to different credentials, endpoint, or selection; use the original options or a different --journal')
                if not isinstance(data.get('pending'), dict):
                    raise ValueError('invalid recovery journal')
                for sid, record in data['pending'].items():
                    identifier(sid)
                    if not isinstance(record, dict):
                        raise ValueError('invalid pending journal entry')
                    if record['status'] not in ('prepared', 'sandbox_deleted') or not isinstance(record['snapshots'], list):
                        raise ValueError('invalid pending journal entry')
                    timestamp(record['started_at']); timestamp(record['paused_at'])
                    for snapshot in record['snapshots']:
                        snapshot_identifier(snapshot)
                self.data = data
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @property
    def pending(self):
        return self.data['pending']

    def save(self):
        if not self.writable:
            raise ValueError('cannot write a dry-run journal')
        fd, temp = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def __exit__(self, *_):
        if self.lock is not None:
            self.lock.close()
