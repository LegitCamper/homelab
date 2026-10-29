#!/usr/bin/python3
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request


STATE = Path('/var/lib/homelab-backup')
CONFIG = Path('/etc/homelab-backup')
PROGRESS = {'stage': 'startup', 'staged': []}


def run(*args, capture=False, allowed=(0,)):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None)
    if result.returncode not in allowed:
        raise RuntimeError(f'{args[0]} {args[1]} failed with exit {result.returncode}')
    return result.stdout or ''


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def inspect(container):
    return json.loads(run('docker', 'inspect', container, capture=True))[0]


def pangolin(settings, path, body=None):
    request = urllib.request.Request(
        f'{settings["base_url"].rstrip("/")}/{path}',
        data=None if body is None else json.dumps(body).encode(),
        method='GET' if body is None else 'POST',
        headers={
            'Authorization': f'Bearer {os.environ["PANGOLIN_BACKUP_API_KEY"]}',
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(request, timeout=30) as reply:
        payload = json.load(reply)
    if not payload.get('success'):
        raise RuntimeError(f'Pangolin rejected {path}: {payload.get("message")}')
    return payload['data'] or {}


def org_path(settings, path):
    return f'org/{settings["org_id"]}/{path}'


def resolve_resources(settings, wanted):
    identifiers = {}
    page = 1
    while True:
        data = pangolin(settings, org_path(settings, f'resources?page={page}&pageSize=100'))
        for resource in data['resources']:
            identifiers[resource['niceId']] = resource['resourceId']
        pagination = data['pagination']
        if page * pagination['pageSize'] >= pagination['total']:
            break
        page += 1
    missing = sorted(set(wanted) - set(identifiers))
    if missing:
        raise RuntimeError(f'Unknown Pangolin resources: {", ".join(missing)}')
    return identifiers


# ponytail: mutes whole alert rules; switch to per-target hcEnabled if you need finer scope.
def mute_alerts(config):
    settings = config.get('pangolin')
    if not settings or not settings.get('alert_rules'):
        return
    prior = {}
    for rule in settings['alert_rules']:
        data = pangolin(settings, org_path(settings, f'alert-rule/{rule}'))
        if 'enabled' not in data:
            raise RuntimeError(f'Pangolin alert rule {rule} has no enabled state')
        prior[str(rule)] = bool(data['enabled'])
    atomic_json(STATE / 'alerts.json', {'settings': settings, 'prior': prior})
    for rule, enabled in prior.items():
        if enabled:
            pangolin(settings, org_path(settings, f'alert-rule/{rule}'), {'enabled': False})


MAINTENANCE_FIELDS = (
    'maintenanceModeEnabled',
    'maintenanceModeType',
    'maintenanceTitle',
    'maintenanceMessage',
    'maintenanceEstimatedTime',
)


def enter_maintenance(config, name, resources):
    settings = config.get('pangolin')
    if not settings or not resources:
        return
    notice = settings.get('maintenance', {})
    prior = {}
    for resource in resources:
        data = pangolin(settings, f'resource/{resource}')
        if 'maintenanceModeEnabled' not in data:
            raise RuntimeError(f'Pangolin resource {resource} has no maintenance state')
        prior[str(resource)] = {field: data.get(field) for field in MAINTENANCE_FIELDS}
    atomic_json(STATE / 'maintenance.json', {'settings': settings, 'prior': prior})
    announcement = {
        'maintenanceModeEnabled': True,
        'maintenanceModeType': 'forced',
        'maintenanceTitle': notice.get('title', 'Scheduled maintenance'),
        'maintenanceMessage': notice.get('message', f'{name} is briefly offline for its nightly backup.'),
        'maintenanceEstimatedTime': notice.get('estimated_time'),
    }
    for resource in prior:
        pangolin(settings, f'resource/{resource}', announcement)


def restore_maintenance():
    def send(settings, resource, prior):
        body = {field: prior[field] for field in MAINTENANCE_FIELDS if field != 'maintenanceModeType'}
        if prior['maintenanceModeType'] is not None:
            body['maintenanceModeType'] = prior['maintenanceModeType']
        pangolin(settings, f'resource/{resource}', body)
    restore_saved(STATE / 'maintenance.json', send, 'maintenance_grace_seconds')


def restore_alerts():
    restore_saved(
        STATE / 'alerts.json',
        lambda settings, rule, enabled: pangolin(
            settings, org_path(settings, f'alert-rule/{rule}'), {'enabled': enabled},
        ),
        'grace_seconds',
    )


def restore_saved(pending, send, grace):
    if not pending.exists():
        return
    saved = json.loads(pending.read_text())
    outstanding = dict(saved['prior'])
    time.sleep(saved['settings'].get(grace, 0))
    try:
        for key, value in saved['prior'].items():
            send(saved['settings'], key, value)
            del outstanding[key]
    finally:
        if outstanding:
            atomic_json(pending, {'settings': saved['settings'], 'prior': outstanding})
        else:
            pending.unlink()


def recover():
    try:
        try:
            recover_containers()
        finally:
            restore_maintenance()
    finally:
        restore_alerts()


def recover_containers():
    pending = STATE / 'stopped.json'
    if not pending.exists():
        return
    containers = json.loads(pending.read_text())
    failures = []
    for container in reversed(containers):
        try:
            run('docker', 'start', container)
            if not inspect(container)['State']['Running']:
                raise RuntimeError('Container did not stay running')
        except Exception as error:
            print(f'Recovery failed for {container}: {error}', file=sys.stderr, flush=True)
            failures.append(container)
    if failures:
        atomic_json(pending, list(reversed(failures)))
        raise RuntimeError('Recovery incomplete; stopped.json retained for retry')
    pending.unlink()


def require_mounts(config):
    for mount in config['required_mounts']:
        if not os.path.ismount(mount):
            raise RuntimeError(f'Required filesystem is not mounted: {mount}')


def no_legacy_jobs():
    names = run('docker', 'ps', '-a', '--format', '{{.Names}}', capture=True).splitlines()
    if set(names) & {'restic-backup', 'restic-prune', 'restic-check'}:
        raise RuntimeError('Retire legacy backup/prune/check containers before running native jobs')


def plan(config):
    require_mounts(config)
    staging = Path(config['staging']).resolve()
    groups = []
    names = set()
    owned_services = set()
    for group in config['groups']:
        for resource in group.get('resources', []):
            if not isinstance(resource, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', resource):
                raise RuntimeError(f'Invalid Pangolin niceId in {group["name"]}: {resource!r}')
    wanted = sorted({resource for group in config['groups'] for resource in group.get('resources', [])})
    settings = config.get('pangolin')
    identifiers = resolve_resources(settings, wanted) if settings and wanted else {}
    for group in config['groups']:
        name = group['name']
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', name) or name in names:
            raise RuntimeError(f'Invalid or duplicate group: {name}')
        names.add(name)
        containers = []
        for service in group['services']:
            if service in owned_services:
                raise RuntimeError(f'Service in multiple groups: {service}')
            owned_services.add(service)
            matches = run(
                'docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={config["project"]}',
                '--filter', f'label=com.docker.compose.service={service}', capture=True,
            ).splitlines()
            if len(matches) != 1:
                raise RuntimeError(f'Expected exactly one container for {service}; got {len(matches)}')
            container = inspect(matches[0])
            state = container['State']
            if state['Paused'] or state['Restarting']:
                raise RuntimeError(f'Container not stable: {service}')
            containers.append(container['Id'])
        paths = [(Path(source), Path(source).relative_to('/')) for source in group['paths']]
        for volume in group.get('volumes', []):
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', volume):
                raise RuntimeError(f'Invalid volume: {volume}')
            data = json.loads(run('docker', 'volume', 'inspect', volume, capture=True))[0]
            paths.append((Path(data['Mountpoint']), Path('volumes') / volume))
        if not paths:
            raise RuntimeError(f'No sources in {name}')
        for source, relative in paths:
            resolved = source.resolve()
            if not source.is_dir() or staging.is_relative_to(resolved) or resolved.is_relative_to(staging):
                raise RuntimeError(f'Missing or recursive source: {source}')
            if (staging / name / relative).resolve() != staging / name / relative:
                raise RuntimeError(f'Unsafe staging destination: {relative}')
        groups.append((
            name, containers, paths,
            [identifiers[resource] for resource in group.get('resources', [])],
        ))
    if not groups:
        raise RuntimeError('No backup groups configured')
    return groups


def sync_group(config, name, paths, final):
    require_mounts(config)
    for source, relative in paths:
        if not source.is_dir():
            raise RuntimeError(f'Source disappeared: {source}')
        destination = Path(config['staging']) / name / relative
        destination.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(config['staging']).free < config['minimum_free_bytes']:
            raise RuntimeError('Insufficient staging disk space; containers will be recovered')
        run(
            'rsync', '-aHAX', '--numeric-ids', '--delete-delay',
            *(['--checksum'] if final else []), '--', f'{source}/', f'{destination}/',
            allowed=(0,) if final else (0, 24),
        )


# ponytail: cold staged copies; use filesystem snapshots or DB dumps if checksum downtime is too long.
def stage_group(config, name, containers, paths, resources=()):
    print(f'Staging {name}', flush=True)
    PROGRESS['stage'] = f'staging {name}'
    sync_group(config, name, paths, final=False)
    running = [container for container in containers if inspect(container)['State']['Running']]
    atomic_json(STATE / 'stopped.json', running)
    try:
        enter_maintenance(config, name, resources)
        for container in running:
            run('docker', 'stop', '--timeout', str(config['stop_timeout']), container)
            state = inspect(container)['State']
            if state['Running'] or state['ExitCode'] == 137 or state['OOMKilled']:
                raise RuntimeError(f'Container did not stop cleanly: {container}')
        sync_group(config, name, paths, final=True)
    finally:
        try:
            recover_containers()
        finally:
            restore_maintenance()
    PROGRESS['staged'].append(name)


def restic(*args, capture=False):
    return run('restic', '--retry-lock', '10m', *args, capture=capture)


# ponytail: fire-and-forget notice; add retries/queueing only if you need delivery guarantees.
def notify(title, lines, failed):
    url = os.environ.get('DISCORD_BACKUP_WEBHOOK')
    if not url:
        return
    if not re.fullmatch(r'https://(discord|discordapp)\.com/api/webhooks/[\w/-]+', url):
        print('Refusing to post to non-Discord webhook URL', file=sys.stderr, flush=True)
        return
    body = {'embeds': [{
        'title': title,
        'description': '\n'.join(f'- {line}' for line in lines)[:4000],
        'color': 0xCC0000 if failed else 0x2E8B57,
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }]}
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except Exception as error:
        print(f'Discord notification failed: {error}', file=sys.stderr, flush=True)


def require_recent_backup(config):
    marker = json.loads((STATE / 'last-success.json').read_text())
    if marker['config'] != config or marker['repository'] != os.environ['RESTIC_REPOSITORY']:
        raise RuntimeError('Backup configuration changed; run a successful backup first')
    snapshots = json.loads(restic(
        'snapshots', '--json', '--host', config['host'], '--tag', config['tag'], capture=True,
    ))
    recent = [
        datetime.datetime.fromisoformat(snapshot['time']) for snapshot in snapshots
        if snapshot['id'].startswith(marker['snapshot_id'])
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    if not recent or not datetime.timedelta(0) <= now - max(recent) < datetime.timedelta(hours=48):
        raise RuntimeError('No staged snapshot within 48 hours; refusing activation/retention')


def execute(action, config):
    no_legacy_jobs()
    if action == 'backup':
        PROGRESS['stage'] = 'planning'
        groups = plan(config)
        PROGRESS['stage'] = 'checking repository'
        restic('cat', 'config')
        mute_alerts(config)
        try:
            for name, containers, paths, resources in groups:
                stage_group(config, name, containers, paths, resources)
        finally:
            restore_alerts()
        require_mounts(config)
        PROGRESS['stage'] = 'uploading to restic'
        output = restic(
            'backup', '--json', '--host', config['host'], '--tag', config['tag'], '--compression', 'auto',
            '--', *[str(Path(config['staging']) / group[0]) for group in groups],
            capture=True,
        )
        print(output, end='', flush=True)
        summary = next(json.loads(line) for line in output.splitlines() if json.loads(line).get('message_type') == 'summary')
        atomic_json(STATE / 'last-success.json', {
            'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'groups': [group[0] for group in groups],
            'snapshot_id': summary['snapshot_id'],
            'config': config,
            'repository': os.environ['RESTIC_REPOSITORY'],
        })
        notify(f'Backup complete on {config["host"]}', [
            f'Snapshot `{summary["snapshot_id"]}`',
            f'{len(groups)} groups: {", ".join(group[0] for group in groups)}',
            f'{summary["files_new"]} new, {summary["files_changed"]} changed,'
            f' {summary["data_added"] / 1e9:.2f} GB uploaded',
            f'Took {datetime.timedelta(seconds=round(summary["total_duration"]))}',
        ], failed=False)
    elif action == 'check':
        week = datetime.date.today().toordinal() // 7
        restic('check', f'--read-data-subset={(week % 12) + 1}/12')
    elif action == 'maintain':
        require_recent_backup(config)
        restic('check')
        restic('forget', '--host', config['host'], '--tag', config['tag'], '--group-by', 'host', *config['retention'])
        restic('prune', '--max-unused', '20%', '--max-repack-size', '1G')
        restic('check')
    elif action == 'ready':
        require_recent_backup(config)
        plan(config)
    elif action == 'restic':
        restic(*sys.argv[2:])
    else:
        raise RuntimeError(f'Unknown action: {action}')


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = sys.argv[1] if len(sys.argv) > 1 else 'backup'
        config = json.loads((CONFIG / 'config.json').read_text())
        environment = json.loads((CONFIG / 'environment.json').read_text())
        required = {'RESTIC_REPOSITORY', 'RESTIC_PASSWORD', 'B2_ACCOUNT_ID', 'B2_ACCOUNT_KEY'}
        optional = {'PANGOLIN_BACKUP_API_KEY', 'DISCORD_BACKUP_WEBHOOK'}
        if not required <= set(environment) <= required | optional or not all(isinstance(value, str) and value for value in environment.values()):
            raise RuntimeError('Invalid credential configuration')
        if config.get('pangolin', {}).get('alert_rules') and 'PANGOLIN_BACKUP_API_KEY' not in environment:
            raise RuntimeError('Alert muting configured without PANGOLIN_BACKUP_API_KEY')
        os.environ.update(environment)
        os.environ['RESTIC_CACHE_DIR'] = str(STATE / 'cache')
        PROGRESS['action'] = action
        PROGRESS['host'] = config['host']
        recover()
        if action == 'recover':
            return
        execute(action, config)


def failure_report(error):
    action = PROGRESS.get('action', 'backup')
    lines = [
        f'Job `{action}` failed during **{PROGRESS["stage"]}**',
        f'Error: `{str(error) or type(error).__name__}`',
    ]
    if PROGRESS['staged']:
        lines.append(f'Staged before failure: {", ".join(PROGRESS["staged"])}')
    for pending, label in (('stopped.json', 'containers left stopped'), ('maintenance.json', 'resources left in maintenance'), ('alerts.json', 'alert rules left muted')):
        path = STATE / pending
        if path.exists():
            lines.append(f'Needs attention: {label} ({path})')
    lines.append(f'Logs: `journalctl -u homelab-{action}.service`')
    return lines


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        if isinstance(error, SystemExit) and not error.code:
            raise
        print(str(error), file=sys.stderr, flush=True)
        notify(f'Backup FAILED on {PROGRESS.get("host", "homeserver")}', failure_report(error), failed=True)
        sys.exit(1)
