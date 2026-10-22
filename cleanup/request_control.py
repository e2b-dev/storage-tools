"""Shared pacing and bounded retries for every cleanup HTTP attempt."""
import math
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


class BackendError(RuntimeError):
    pass


class Missing(BackendError):
    pass


class AuthorizationError(BackendError):
    pass


class RetryLaterError(BackendError):
    pass


def retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            return None
        return max(0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class RequestControl:
    def __init__(self, rate=20, retries=5, clock=time.monotonic, sleep=None):
        self.interval = 1 / rate
        self.retries = retries
        self.clock = clock
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.cancelled = threading.Event()
        self.sleep = sleep or self.cancelled.wait
        self.attempts = 0
        self.retried = 0

    def wait(self):
        while True:
            with self.lock:
                if self.cancelled.is_set():
                    raise BackendError('cleanup stopped')
                delay = self.next_request - self.clock()
                if delay <= 0:
                    self.next_request = self.clock() + self.interval
                    self.attempts += 1
                    return
            # Recheck after waking: another worker or Retry-After can move the slot.
            self.sleep(delay)

    def defer(self, delay):
        with self.lock:
            self.next_request = max(self.next_request, self.clock() + delay)
            self.retried += 1

    def request(self, client, method, path, *, before_retry=None, **kwargs):
        for attempt in range(self.retries + 1):
            if attempt and before_retry is not None:
                # The guard's GET shares this controller, so it waits out the
                # cooldown before reading state; the DELETE takes its own slot.
                before_retry()
            self.wait()
            try:
                response = client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise BackendError(f'{method} {path}: transport failure after {attempt + 1} attempts') from None
                self.defer(min(30, 2 ** attempt) + random.uniform(0, 0.25))
                continue
            if response.status_code in (401, 403):
                self.cancelled.set()
                raise AuthorizationError(f'{method} {path}: HTTP {response.status_code}; check API key and region')
            if response.status_code == 404:
                raise Missing(f'{method} {path}: not found')
            if response.status_code in (429, 500, 502, 503, 504):
                delay = retry_after(response.headers.get('Retry-After'))
                if delay is not None and delay > 60:
                    self.cancelled.set()
                    raise RetryLaterError(f'{method} {path}: Retry-After exceeds 60s; retry in a later run')
                self.defer(delay if delay is not None else min(30, 2 ** attempt) + random.uniform(0, 0.25))
                if attempt < self.retries:
                    response.close()
                    continue
            if response.status_code >= 300:
                raise BackendError(f'{method} {path}: HTTP {response.status_code} after {attempt + 1} attempts')
            return response
        raise AssertionError('unreachable')
