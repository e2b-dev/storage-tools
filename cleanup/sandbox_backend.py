"""Streaming cleanup API operations, shared by Python and shell entry points."""
import argparse
from datetime import datetime, timezone
import math
import os
import re
import sys
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlparse

import httpx

from request_control import AuthorizationError, BackendError, Missing, RequestControl, RetryLaterError


def timestamp(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError('timestamps must be ISO strings or datetime values')
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamps must include a timezone')
    return result.astimezone(timezone.utc)


def nonnegative(value):
    try:
        result = float(value)
        if not math.isfinite(result) or not 0 <= result <= 365000:
            raise ValueError()
        return result
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError('must be a finite number between 0 and 365000') from None


def positive(value):
    result = nonnegative(value)
    if result == 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return result


def metadata_arg(value):
    result = {}
    for item in value.split(','):
        key, sep, val = item.partition('=')
        if not sep or not key or key in result:
            raise argparse.ArgumentTypeError('use unique nonempty keys: key=value,key2=value2')
        result[key] = val
    return result


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
        raise BackendError(f'invalid resource ID: {value!r}')
    return value


def snapshot_identifier(value):
    # The API reports a named snapshot as "namespace/alias:tag" and an unnamed one as "templateID:tag".
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?(?::[A-Za-z0-9_.-]+)?', value):
        raise BackendError(f'invalid snapshot ID: {value!r}')
    return value


def normalize(raw):
    try:
        sid = raw.get('sandboxID') or raw.get('sandboxId')
        state, metadata = raw['state'], raw.get('metadata') or {}
        if state not in ('paused', 'running') or not isinstance(metadata, dict):
            raise ValueError('invalid state or metadata')
        return SimpleNamespace(sandbox_id=identifier(sid), state=state,
                               started_at=timestamp(raw['startedAt']),
                               end_at=timestamp(raw['endAt']), metadata=metadata)
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise BackendError(f'invalid sandbox response: {error}') from error


class APIBackend:
    def __init__(self, rate=20, retries=5, page_size=100, timeout=60, client=None):
        key = os.environ.get('E2B_API_KEY')
        if not key:
            raise BackendError('E2B_API_KEY is required')
        base = os.environ.get('E2B_API_URL') or f"https://api.{os.environ.get('E2B_DOMAIN') or 'e2b.app'}"
        parsed = urlparse(base)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
            raise BackendError('E2B_API_URL must be an HTTP(S) URL without embedded credentials')
        self.client = client or httpx.Client(base_url=base.rstrip('/'),
                                          headers={'X-API-Key': key}, timeout=timeout,
                                          follow_redirects=False)
        self.control = RequestControl(rate, retries)
        self.page_size = page_size
        self.invalid_rows = 0

    def close(self):
        self.client.close()

    def cancel(self):
        self.control.cancelled.set()

    def pages(self, path, params):
        params = {**params, 'limit': self.page_size}
        token = None
        while True:
            response = self.control.request(self.client, 'GET', path, params=params)
            try:
                page = response.json()
                if not isinstance(page, list):
                    raise ValueError('expected a list')
            except (ValueError, TypeError) as error:
                raise BackendError(f'{path}: invalid listing response') from error
            next_token = response.headers.get('X-Next-Token')
            if next_token and next_token == token:
                raise BackendError(f'{path}: pagination cursor did not advance')
            yield page
            if not next_token:
                return
            token = next_token
            params['nextToken'] = token

    def list_paused(self, metadata=None):
        params = {'state': 'paused', 'order': 'asc'}
        if metadata:
            params['metadata'] = urlencode({quote(k): quote(v) for k, v in metadata.items()})
        for page in self.pages('/v2/sandboxes', params):
            for item in page:
                try:
                    info = normalize(item)
                except BackendError as error:
                    # One unreadable row must not abort a cron run; log it and keep streaming.
                    self.invalid_rows += 1
                    sid = (item.get('sandboxID') or item.get('sandboxId')) if isinstance(item, dict) else None
                    print(f'Skipped unreadable listing row {sid!r}: {error}', file=sys.stderr, flush=True)
                    continue
                yield info

    def get_info(self, sid):
        response = self.control.request(self.client, 'GET', f'/sandboxes/{identifier(sid)}')
        try:
            return normalize(response.json())
        except (ValueError, TypeError) as error:
            raise BackendError('invalid sandbox response') from error

    def kill(self, sid):
        try:
            self.control.request(self.client, 'DELETE', f'/sandboxes/{identifier(sid)}')
            return True
        except Missing:
            return False

    def snapshots(self, sid):
        ids = []
        for page in self.pages('/snapshots', {'sandboxID': identifier(sid)}):
            for item in page:
                if not isinstance(item, dict):
                    raise BackendError('invalid snapshot listing response')
                ids.append(snapshot_identifier(item.get('snapshotID') or item.get('snapshotId')))
        return list(dict.fromkeys(ids))

    def delete_snapshot(self, sid):
        try:
            self.control.request(self.client, 'DELETE', f'/templates/{quote(snapshot_identifier(sid), safe="")}')
            return True
        except Missing:
            return False


def backend(name='sdk', **kwargs):
    if os.environ.get('E2B_DEBUG', '').lower() == 'true':
        raise BackendError('E2B_DEBUG=true is unsupported; unset it before account operations')
    return APIBackend(**kwargs)
