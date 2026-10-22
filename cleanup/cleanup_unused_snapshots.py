#!/usr/bin/env python3
"""Preview or delete snapshot resources by their own observed last use.

Deletion affects every tag/build of the canonical snapshot resource. Usage telemetry
is best-effort: it is buffered and can be delayed or lost, so it is not authoritative
proof of non-use. Fresh lookups cannot make deletion atomic with use.
Apply runs hold each batch for at least 120 seconds after its final candidate
observation, then recheck membership, usage and build state before deletion.
"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from itertools import islice
import sys

from cleanup_journal import run_lock
from cleanup_paused_sandboxes import hours_as_days
from request_control import AuthorizationError, BackendError, HTTPError, Missing, RetryLaterError
from sandbox_backend import SelectionChanged, positive
from snapshot_backend import backend


OBSERVATION_HOLD_SECONDS = 120
CANDIDATE_BATCH_SIZE = 1000


class NoMetadata(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error('--metadata is unsupported: snapshots do not expose their own metadata; use --name to select a snapshot resource')


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    age = parser.add_mutually_exclusive_group()
    age.add_argument('--unused-for-days', type=int, dest='days', default=90,
                     help='select snapshots unused for more than N whole days (default 90)')
    age.add_argument('--unused-for-hours', type=hours_as_days, dest='days', metavar='N',
                     help='select snapshots unused for more than N hours')
    parser.add_argument('--name', help='snapshot name, ID or tag; selects the whole resource, including every tag/build')
    parser.add_argument('--metadata', action=NoMetadata, nargs='?', help=argparse.SUPPRESS)
    parser.add_argument('--apply', action='store_true', help='delete matches; default is a dry run')
    parser.add_argument('--recheck', action='store_true', help='accepted for consistency; snapshot rechecks are always enabled')
    parser.add_argument('--rate', type=positive, default=20, help='total HTTP attempts per second, default 20')
    parser.add_argument('--concurrency', type=int, default=10, help='snapshot deletes in flight, 1-64, default 10')
    parser.add_argument('--page-size', type=int, default=100, help='snapshots per list request, 1-100, default 100')
    parser.add_argument('--retries', type=int, default=5, help='retries after transient failures, 0-10, default 5')
    parser.add_argument('--limit', type=int, help='process at most N matching snapshot resources')
    parser.add_argument('--lock-file', help='local overlap lock; defaults to the shared credential/endpoint lock')
    return parser


def eligible(info, cutoff):
    return info.retention_at is not None and info.retention_at < cutoff


def hold_for_usage_updates(control, observed_at):
    deadline = observed_at + OBSERVATION_HOLD_SECONDS
    while True:
        if control.cancelled.is_set():
            raise BackendError('cleanup stopped')
        remaining = deadline - control.clock()
        if remaining <= 0:
            return
        # The shared clock is monotonic; its default sleep waits on the same
        # cancellation event as HTTP pacing, so cancellation wakes the hold.
        control.sleep(remaining)


def run(args, api, cutoff):
    print('Warning: snapshot usage telemetry is buffered and can be delayed or lost. '
          'Last-use timestamps are not authoritative proof of non-use; '
          'rechecks are best-effort and deletion can race with use.', file=sys.stderr, flush=True)
    total = deleted = skipped = blocked = failed = 0
    seen = set()

    def candidates():
        nonlocal skipped, failed
        for reference in api.list_snapshot_refs(args.name):
            try:
                template_id = api.resolve_snapshot(reference)
                if template_id in seen:
                    continue
                seen.add(template_id)
                info = api.snapshot_details(template_id)
                if info.retention_at is None:
                    skipped += 1
                    print(f'Skipped {template_id}: last use or latest build is unknown or still in progress', flush=True)
                elif eligible(info, cutoff):
                    yield info, api.control.clock()
            except Missing:
                skipped += 1
                print(f'Skipped {reference}: already gone', flush=True)
            except (AuthorizationError, RetryLaterError):
                raise
            except BackendError as error:
                failed += 1
                print(f'Failed {reference}: {error}', file=sys.stderr, flush=True)

    def delete(info):
        template_id = info.template_id

        def recheck():
            if not api.is_snapshot(template_id):
                raise SelectionChanged('resource is no longer listed as a snapshot')
            current = api.snapshot_details(template_id)
            if current != info or not eligible(current, cutoff):
                raise SelectionChanged('snapshot usage or build state changed')

        try:
            recheck()
            return ('deleted' if api.delete_snapshot(template_id, before_retry=recheck) else 'skipped'), template_id, 'already gone'
        except Missing:
            return 'skipped', template_id, 'already gone'
        except SelectionChanged as error:
            return 'skipped', template_id, str(error)
        except (AuthorizationError, RetryLaterError):
            raise
        except HTTPError as error:
            if error.status_code == 400 and error.method == 'DELETE':
                return 'blocked', template_id, 'API refused deletion (HTTP 400; snapshot may still be in use)'
            return 'failed', template_id, str(error)
        except BackendError as error:
            return 'failed', template_id, str(error)

    def collect(future):
        nonlocal deleted, skipped, blocked, failed
        outcome, template_id, detail = future.result()
        if outcome == 'deleted':
            deleted += 1
            print(f'Deleted snapshot {template_id}', flush=True)
        elif outcome == 'failed':
            failed += 1
            print(f'Failed {template_id}: {detail}', file=sys.stderr, flush=True)
        elif outcome == 'blocked':
            blocked += 1
            print(f'Skipped {template_id}: {detail}', file=sys.stderr, flush=True)
        else:
            skipped += 1
            print(f'Skipped {template_id}: {detail}', flush=True)

    pool = ThreadPoolExecutor(max_workers=args.concurrency) if args.apply else None
    pending = set()
    try:
        selected = islice(candidates(), args.limit)
        batch_size = CANDIDATE_BATCH_SIZE if pool else 1
        while batch := list(islice(selected, batch_size)):
            for info, _ in batch:
                total += 1
                print(f'{info.template_id}\tlast_used={info.last_spawned_at.isoformat()}'
                      f'\tretention_at={info.retention_at.isoformat()}', flush=True)
            if pool:
                print(f'Holding {len(batch)} snapshot(s) for {OBSERVATION_HOLD_SECONDS}s '
                      'after the last observation before rechecking.', flush=True)
                hold_for_usage_updates(api.control, batch[-1][1])
                for info, _ in batch:
                    pending.add(pool.submit(delete, info))
                    if len(pending) >= args.concurrency:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            collect(future)
                for future in pending:
                    collect(future)
                pending.clear()
    except BaseException:
        api.cancel()
        for future in pending:
            future.cancel()
        raise
    finally:
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
    if args.apply:
        print(f'Deleted {deleted} of {total} matching snapshot(s); skipped {skipped}, blocked {blocked}, failed {failed}.')
    else:
        print(f'Dry run: {total} snapshot(s) matched; skipped {skipped}, failed {failed}. Re-run with --apply to delete.')
    if api.invalid_rows:
        print(f'Skipped {api.invalid_rows} unreadable snapshot listing row(s).', file=sys.stderr)
    return int(bool(failed or blocked or api.invalid_rows))


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    if not 0 <= args.days <= 365000:
        parser.error('--unused-for-days must be between 0 and 365000')
    if not 1 <= args.page_size <= 100:
        parser.error('--page-size must be between 1 and 100')
    if not 1 <= args.concurrency <= 64:
        parser.error('--concurrency must be between 1 and 64')
    if not 0 <= args.retries <= 10:
        parser.error('--retries must be between 0 and 10')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be at least 1')
    if args.name is not None and not args.name.strip():
        parser.error('--name must not be empty')
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        api = backend(rate=args.rate, retries=args.retries, page_size=args.page_size)
        try:
            with run_lock(args.lock_file, args.apply):
                return run(args, api, cutoff)
        finally:
            api.close()
    except (BackendError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'Snapshot cleanup failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Interrupted; completed deletes stand. Re-run to continue.', file=sys.stderr)
        sys.exit(130)
