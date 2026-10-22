"""Snapshot discovery and canonical template state using the shared request budget."""
from dataclasses import dataclass
from datetime import datetime
import os
import re
import sys
from urllib.parse import quote

from sandbox_backend import APIBackend, BackendError, Missing, identifier, timestamp


def snapshot_reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?(?::[A-Za-z0-9_.-]+)?', value):
        raise BackendError(f'invalid snapshot reference: {value!r}')
    return value


@dataclass(frozen=True)
class SnapshotState:
    template_id: str
    last_spawned_at: datetime | None
    created_at: datetime
    updated_at: datetime
    spawn_count: int
    build_id: str
    build_count: int
    latest_build: tuple | None

    @property
    def retention_at(self):
        if self.last_spawned_at is None or self.latest_build is None:
            return None
        _, created, updated, status = self.latest_build
        if status not in ('ready', 'error'):
            return None
        return max(self.last_spawned_at, self.created_at, self.updated_at, created, updated)


def snapshot_state(raw, template_id):
    try:
        if identifier(raw['templateID']) != template_id:
            raise ValueError('template ID does not match the request')
        counts = [raw['spawnCount'], raw['buildCount']]
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError('usage and build counts must be nonnegative integers')
        builds = raw['builds']
        if not isinstance(builds, list):
            raise ValueError('builds must be a list')
        latest = None
        if builds:
            build = builds[0]  # The API returns newest-created builds first across all tags/statuses.
            if not isinstance(build['status'], str):
                raise ValueError('build status must be a string')
            latest = (identifier(build['buildID']), timestamp(build['createdAt']),
                      timestamp(build['updatedAt']), build['status'])
        return SnapshotState(template_id, None if raw['lastSpawnedAt'] is None else timestamp(raw['lastSpawnedAt']),
                             timestamp(raw['createdAt']), timestamp(raw['updatedAt']), counts[0],
                             identifier(raw['buildID']), counts[1], latest)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BackendError(f'invalid snapshot details for {template_id}: {error}') from error


class SnapshotBackend(APIBackend):
    def list_snapshot_refs(self, name=None):
        for page in self.pages('/snapshots', {'name': name} if name else {}):
            for row in page:
                try:
                    yield snapshot_reference(row['snapshotID'])
                except (KeyError, TypeError, BackendError) as error:
                    self.invalid_rows += 1
                    print(f'Skipped unreadable snapshot listing row: {error}', file=sys.stderr, flush=True)

    def resolve_snapshot(self, reference):
        response = self.control.request(self.client, 'GET',
                                        f'/templates/aliases/{quote(snapshot_reference(reference), safe="")}')
        try:
            return identifier(response.json()['templateID'])
        except (KeyError, TypeError, ValueError) as error:
            raise BackendError('invalid snapshot alias response') from error

    def snapshot_details(self, template_id):
        template_id = identifier(template_id)
        response = self.control.request(self.client, 'GET', f'/templates/{template_id}', params={'limit': 1})
        try:
            return snapshot_state(response.json(), template_id)
        except ValueError as error:
            raise BackendError(f'invalid snapshot details for {template_id}') from error

    def is_snapshot(self, template_id):
        template_id = identifier(template_id)
        invalid_before = self.invalid_rows
        try:
            for reference in self.list_snapshot_refs(template_id):
                try:
                    if self.resolve_snapshot(reference) == template_id:
                        return True
                except Missing:
                    continue
        except Missing as error:
            raise BackendError('snapshot membership listing is unavailable') from error
        if self.invalid_rows != invalid_before:
            raise BackendError('could not verify snapshot membership from malformed listing')
        return False

    def delete_snapshot(self, template_id, before_retry):
        try:
            self.control.request(self.client, 'DELETE', f'/templates/{identifier(template_id)}',
                                 before_retry=before_retry)
            return True
        except Missing:
            return False


def backend(**kwargs):
    if os.environ.get('E2B_DEBUG', '').lower() == 'true':
        raise BackendError('E2B_DEBUG=true is unsupported; unset it before account operations')
    return SnapshotBackend(**kwargs)
