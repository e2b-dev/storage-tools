"""Local overlap locking for storage cleanup processes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager


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
