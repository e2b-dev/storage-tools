#!/usr/bin/env python3
"""Preview or delete old paused E2B sandboxes.

Named snapshots are separate resources, included only with --delete-snapshots.
Pages are consumed incrementally, and every HTTP attempt shares the request budget.
Deletion uses the listed state by default; --recheck adds a fresh lookup.
"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice
from datetime import datetime, timedelta, timezone
import sys

from cleanup_journal import Journal, run_lock
from sandbox_backend import AuthorizationError, BackendError, Missing, RetryLaterError, backend, metadata_arg, nonnegative, positive, timestamp


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unused-for-days', '--days', dest='days', type=nonnegative, default=90, help='select resources unused for more than N days (default 90; --days is a compatibility alias)')
    parser.add_argument('--metadata', type=metadata_arg, help='key=value,key2=value2')
    parser.add_argument('--age-from', choices=['paused','started'], default='paused')
    parser.add_argument('--delete-snapshots', action='store_true', help='also include all named snapshots from matching paused sandboxes, regardless of snapshot age')
    parser.add_argument('--apply', action='store_true', help='delete matches; default is a dry run')
    parser.add_argument('--resumes-stopped', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--recheck', action='store_true', help='recheck state, age and metadata immediately before deletion')
    parser.add_argument('--rate', type=positive, default=20, help='combined GET and DELETE attempts per second, default 20; lower this for shared budgets')
    parser.add_argument('--concurrency', type=int, default=10, help='sandbox deletes in flight, default 10')
    parser.add_argument('--page-size', type=int, default=100, help='resources per list request, 1-100, default 100')
    parser.add_argument('--retries', type=int, default=5, help='retries per request after 429/5xx or transport errors, default 5')
    parser.add_argument('--limit', type=int, help='process at most N matching/pending sandboxes per run')
    parser.add_argument('--lock-file', help='local overlap lock; defaults to a credential/endpoint-specific file in the temporary directory')
    parser.add_argument('--journal', help='named snapshot recovery file (default: account/selection-specific file beside this script)')
    parser.add_argument('--retry-pending', action='store_true', help='only process existing journal entries; use the same selection options as the original run')
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
        try:
            if args.recheck:
                info = api.get_info(sid)
                if not is_eligible(info, args, cutoff):
                    return 'skipped', sid, 'state, age or metadata changed'
            return ('deleted' if api.kill(sid) else 'skipped'), sid, ''
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
        print(f'Deleted {deleted} of {total} matching/pending paused sandbox(es); skipped {skipped}, failed {failed}.')
    else:
        print(f'Dry run: {total} paused sandbox(es) matched/pending. Re-run with --apply to delete.')
    return 1 if failed or report_invalid_rows(api) else 0


def run(args, api, cutoff):
    def eligible(info):
        return is_eligible(info, args, cutoff)

    if not args.delete_snapshots:
        return run_sandboxes(args, api, cutoff)

    scope = dict(days=float(args.days), age_from=args.age_from, metadata=args.metadata or {})
    with Journal(args.journal, scope, args.apply) as journal:
        # Load recovery records before listing so --retry-pending can work during list failures.
        pending = journal.pending if journal else {}
        recovering = set(pending)

        def entries():
            for sid in recovering:
                yield sid, None
            if not args.retry_pending:
                for info in selected_rows(args, api, cutoff):
                    if info.sandbox_id not in recovering:
                        yield info.sandbox_id, info

        total = 0
        deleted = skipped = failed = snapshots_deleted = 0
        if journal:
            print(f'Named snapshot recovery journal: {journal.path}')
        for sid, listed in islice(entries(), args.limit):
            total += 1
            record = pending.get(sid)
            if listed:
                print(f'{sid}\tstarted={listed.started_at.isoformat()}\tpaused={listed.end_at.isoformat()}')
            else:
                print(f'Pending cleanup for {sid}')
            try:
                if not args.apply:
                    if args.delete_snapshots:
                        snapshots = record['snapshots'] if record else api.snapshots(sid)
                        for snapshot in snapshots:
                            print(f'  named snapshot {snapshot}')
                    continue

                try:
                    current = api.get_info(sid)
                except Missing:
                    current = None
                if current is None and record is None:
                    print(f'Skipped {sid}: already gone')
                    skipped += 1
                    continue
                if current is not None:
                    unchanged = record is None or (current.started_at == timestamp(record['started_at']) and current.end_at == timestamp(record['paused_at']))
                    if not eligible(current) or not unchanged or (record and record['status'] == 'sandbox_deleted'):
                        print(f'Skipped {sid}: state, metadata, or pause changed')
                        skipped += 1
                        if record:
                            # Cancel snapshots of a parent whose eligibility changed; never delete them here.
                            del pending[sid]
                            journal.save()
                        continue
                    if args.delete_snapshots and record is None:
                        record = dict(status='prepared', started_at=current.started_at.isoformat(),
                                      paused_at=current.end_at.isoformat(), snapshots=api.snapshots(sid))
                        pending[sid] = record
                        journal.save()  # Must be durable before kill can run.
                    # Preserve the journaled parent identity across snapshot enumeration.
                    try:
                        latest = api.get_info(sid)
                    except Missing:
                        latest = None
                    if latest is not None and (not eligible(latest) or latest.started_at != current.started_at or latest.end_at != current.end_at):
                        print(f'Skipped {sid}: changed immediately before deletion')
                        skipped += 1
                        if record:
                            del pending[sid]
                            journal.save()
                        continue
                    if latest is not None and api.kill(sid):
                        deleted += 1
                        print(f'Deleted paused sandbox {sid}')
                    else:
                        skipped += 1
                        print(f'Paused sandbox {sid} already gone')
                else:
                    skipped += 1
                    print(f'Paused sandbox {sid} already gone; recovering named snapshot cleanup')
                if record:
                    record['status'] = 'sandbox_deleted'
                    journal.save()
                    for snapshot in list(record['snapshots']):
                        try:
                            if api.delete_snapshot(snapshot):
                                snapshots_deleted += 1
                                print(f'  Deleted named snapshot {snapshot}')
                            else:
                                print(f'  Named snapshot {snapshot} already gone')
                        except (AuthorizationError, RetryLaterError):
                            raise
                        except BackendError as error:
                            failed += 1
                            print(f'Failed named snapshot {snapshot}: {error}', file=sys.stderr)
                            continue
                        record['snapshots'].remove(snapshot)
                        journal.save()
                    if not record['snapshots']:
                        del pending[sid]
                        journal.save()
            except (AuthorizationError, RetryLaterError):
                raise
            except BackendError as error:
                failed += 1
                print(f'Failed {sid}: {error}', file=sys.stderr)
        if args.apply:
            print(f'Deleted {deleted} of {total} matching/pending paused sandbox(es); skipped {skipped}, failed {failed}.')
            if args.delete_snapshots:
                print(f'Deleted {snapshots_deleted} named snapshot(s); {len(pending)} pending sandbox record(s).')
        else:
            print(f'Dry run: {total} paused sandbox(es) matched/pending; failed {failed}. Re-run with --apply to delete.')
        return 1 if failed or report_invalid_rows(api) else 0


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    if args.retry_pending and not args.delete_snapshots:
        parser.error('--retry-pending requires --delete-snapshots')
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
