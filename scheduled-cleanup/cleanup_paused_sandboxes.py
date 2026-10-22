#!/usr/bin/env python3
"""Preview or delete old paused E2B sandboxes.

Named snapshots are separate resources and are never deleted by this command.
Pages are consumed incrementally, and every HTTP attempt shares the request budget.
Deletion uses the listed state by default; --recheck adds a fresh lookup.
"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys

from cleanup_journal import run_lock
from sandbox_backend import AuthorizationError, BackendError, Missing, RetryLaterError, SelectionChanged, backend, metadata_arg, nonnegative, positive


def hours_as_days(value):
    nonnegative(value)
    return float(Decimal(value) / 24)


class RemovedSnapshotOption(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(f'{option_string} is no longer supported by cleanup-paused-sandboxes; '
                     'use cleanup-unused-snapshots for snapshot cleanup. '
                     'Existing recovery journals are not read or modified.')


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    age = parser.add_mutually_exclusive_group()
    age.add_argument('--unused-for-days', '--days', dest='days', type=int, default=90, help='select resources unused for more than N whole days (default 90; --days is a compatibility alias)')
    age.add_argument('--unused-for-hours', dest='days', type=hours_as_days, metavar='N', help='select resources unused for more than N hours')
    parser.add_argument('--metadata', type=metadata_arg, help='key=value,key2=value2')
    parser.add_argument('--age-from', choices=['paused','started'], default='paused')
    parser.add_argument('--delete-snapshots', '--retry-pending', action=RemovedSnapshotOption,
                        nargs=0, help=argparse.SUPPRESS)
    parser.add_argument('--journal', action=RemovedSnapshotOption, nargs='?', help=argparse.SUPPRESS)
    parser.add_argument('--apply', action='store_true', help='delete matches; default is a dry run')
    parser.add_argument('--resumes-stopped', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--recheck', action='store_true', help='recheck state, age and metadata immediately before deletion')
    parser.add_argument('--rate', type=positive, default=20, help='combined GET and DELETE attempts per second, default 20; lower this for shared budgets')
    parser.add_argument('--concurrency', type=int, default=10, help='sandbox deletes in flight, default 10')
    parser.add_argument('--page-size', type=int, default=100, help='resources per list request, 1-100, default 100')
    parser.add_argument('--retries', type=int, default=5, help='retries per request after 429/5xx or transport errors, default 5')
    parser.add_argument('--limit', type=int, help='process at most N matching sandboxes per run')
    parser.add_argument('--lock-file', help='local overlap lock; defaults to a credential/endpoint-specific file in the temporary directory')
    parser.add_argument('--backend', choices=['sdk','cli'], default='sdk', help=argparse.SUPPRESS)
    return parser


def is_eligible(info, args, cutoff):
    age = info.end_at if args.age_from == 'paused' else info.started_at
    return (info.state == 'paused' and age < cutoff and
            all(info.metadata.get(k) == v for k, v in (args.metadata or {}).items()))


def selected_rows(args, api, cutoff):
    for info in api.list_paused(args.metadata):
        # Start-time order only supports early termination for start-based retention.
        if args.age_from == 'started' and info.started_at >= cutoff:
            return
        if is_eligible(info, args, cutoff):
            yield info


def report_invalid_rows(api):
    count = getattr(api, 'invalid_rows', 0)
    if count:
        print(f'Skipped {count} unreadable listing row(s); see earlier messages. Other sandboxes were processed.', file=sys.stderr, flush=True)
    return count


def run_sandboxes(args, api, cutoff):
    total = deleted = skipped = failed = 0

    def delete(info):
        sid = info.sandbox_id

        def recheck():
            current = api.get_info(sid)
            if not is_eligible(current, args, cutoff):
                raise SelectionChanged('state, age or metadata changed')

        try:
            if args.recheck:
                recheck()
                deleted = api.kill(sid, before_retry=recheck)
            else:
                deleted = api.kill(sid)
            return ('deleted' if deleted else 'skipped'), sid, ''
        except SelectionChanged as error:
            return 'skipped', sid, str(error)
        except Missing:
            return 'skipped', sid, 'already gone'
        except (AuthorizationError, RetryLaterError):
            raise
        except BackendError as error:
            return 'failed', sid, str(error)

    def collect(future):
        nonlocal deleted, skipped, failed
        outcome, sid, detail = future.result()
        if outcome == 'deleted':
            deleted += 1
            print(f'Deleted paused sandbox {sid}', flush=True)
        elif outcome == 'failed':
            failed += 1
            print(f'Failed {sid}: {detail}', file=sys.stderr, flush=True)
        else:
            skipped += 1
            print(f'Skipped {sid}: {detail or "already gone"}', flush=True)

    pool = ThreadPoolExecutor(max_workers=args.concurrency) if args.apply else None
    pending = set()
    try:
        for info in islice(selected_rows(args, api, cutoff), args.limit):
            total += 1
            print(f'{info.sandbox_id}\tstarted={info.started_at.isoformat()}\tpaused={info.end_at.isoformat()}', flush=True)
            if pool:
                pending.add(pool.submit(delete, info))
                if len(pending) >= args.concurrency:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        collect(future)
        for future in pending:
            collect(future)
    except BaseException:
        api.cancel()
        for future in pending:
            future.cancel()
        raise
    finally:
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
    if args.apply:
        print(f'Deleted {deleted} of {total} matching paused sandbox(es); skipped {skipped}, failed {failed}.')
    else:
        print(f'Dry run: {total} paused sandbox(es) matched. Re-run with --apply to delete.')
    return 1 if failed or report_invalid_rows(api) else 0


def run(args, api, cutoff):
    return run_sandboxes(args, api, cutoff)


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
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        api = backend(args.backend, rate=args.rate, retries=args.retries, page_size=args.page_size)
        try:
            with run_lock(args.lock_file, args.apply):
                return run(args, api, cutoff)
        finally:
            api.close()
    except (BackendError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'Cleanup failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Interrupted; completed deletes stand. Re-run to continue.', file=sys.stderr)
        sys.exit(130)
