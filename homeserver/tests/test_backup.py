import importlib.util
import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('backup', ROOT / 'homeserver/files/homelab-backup.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_patch = patch.object(backup, 'STATE', self.root)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_only_running_containers_restart_in_reverse_order_before_upload(self):
        events = []
        states = {'app': True, 'db': True, 'off': False}

        def inspect(container):
            return {'State': {'Running': states[container], 'ExitCode': 0, 'OOMKilled': False}}

        def run(*args, **kwargs):
            self.assertTrue((self.root / 'stopped.json').exists())
            if args[1] == 'stop':
                self.assertEqual(args[:4], ('docker', 'stop', '--timeout', '120'))
            events.append((args[1], args[-1]))
            states[args[-1]] = args[1] == 'start'

        def sync(config, name, paths, final):
            events.append(('sync', final))

        with patch.object(backup, 'inspect', inspect), patch.object(backup, 'run', run), patch.object(backup, 'sync_group', sync):
            backup.stage_group({'stop_timeout': 120}, 'group', ['app', 'db', 'off'], [])
        self.assertEqual(events, [('sync', False), ('stop', 'app'), ('stop', 'db'), ('sync', True), ('start', 'db'), ('start', 'app')])
        self.assertFalse(states['off'])
        self.assertFalse((self.root / 'stopped.json').exists())

    def test_stop_or_final_sync_failure_still_recovers(self):
        for failing_step in ('stop', 'sync'):
            with self.subTest(failing_step=failing_step):
                events = []
                def run(*args, **kwargs):
                    events.append(args[1])
                    if args[1] == failing_step:
                        raise RuntimeError('injected failure')
                def sync(config, name, paths, final):
                    if final and failing_step == 'sync':
                        raise RuntimeError('injected failure')
                states = [True, False, True] if failing_step == 'sync' else [True, True]
                with patch.object(backup, 'inspect', side_effect=[{'State': {'Running': state, 'ExitCode': 0, 'OOMKilled': False}} for state in states]), patch.object(backup, 'run', run), patch.object(backup, 'sync_group', sync):
                    with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                        backup.stage_group({'stop_timeout': 120}, 'group', ['app'], [])
                self.assertIn('start', events)
                self.assertFalse((self.root / 'stopped.json').exists())

    def test_recovery_failure_retains_only_failed_containers(self):
        backup.atomic_json(self.root / 'stopped.json', ['app', 'db'])
        def run(*args, **kwargs):
            if args[-1] == 'db':
                raise RuntimeError('unavailable')
        with patch.object(backup, 'run', run), patch.object(backup, 'inspect', return_value={'State': {'Running': True}}):
            with self.assertRaisesRegex(RuntimeError, 'Recovery incomplete'):
                backup.recover()
        self.assertEqual(json.loads((self.root / 'stopped.json').read_text()), ['db'])

    def test_checksum_detects_equal_size_and_mtime_change_and_deletion(self):
        source = self.root / 'source'
        source.mkdir()
        data = source / 'db'
        data.write_text('first')
        (source / 'removed').write_text('old')
        config = {'staging': str(self.root / 'staging'), 'required_mounts': [], 'minimum_free_bytes': 0}
        backup.sync_group(config, 'group', [(source, Path('data'))], final=False)
        modified = data.stat().st_mtime_ns
        data.write_text('other')
        os.utime(data, ns=(modified, modified))
        (source / 'removed').unlink()
        backup.sync_group(config, 'group', [(source, Path('data'))], final=True)
        target = self.root / 'staging/group/data'
        self.assertEqual((target / 'db').read_text(), 'other')
        self.assertFalse((target / 'removed').exists())

    def test_final_sync_rejects_vanished_files(self):
        with patch.object(backup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 24, '')):
            backup.run('rsync', 'source', allowed=(0, 24))
            with self.assertRaisesRegex(RuntimeError, 'exit 24'):
                backup.run('rsync', 'source')

    def test_upload_failure_does_not_record_success_or_prune(self):
        calls = []
        def restic(*args, **kwargs):
            calls.append(args[0])
            if args[0] == 'backup':
                raise RuntimeError('upload failed')
        config = {'staging': str(self.root), 'required_mounts': [], 'host': 'test', 'tag': 'test'}
        with patch.object(backup, 'no_legacy_jobs'), patch.object(backup, 'plan', return_value=[('app', [], [], [])]), patch.object(backup, 'stage_group') as stage, patch.object(backup, 'restic', restic):
            with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                backup.execute('backup', config)
            stage.assert_called_once()
        self.assertEqual(calls, ['cat', 'backup'])
        self.assertFalse((self.root / 'last-success.json').exists())

    def test_missing_mount_prevents_any_docker_call(self):
        with patch.object(backup.os.path, 'ismount', return_value=False), patch.object(backup, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'not mounted'):
                backup.plan({'required_mounts': ['/missing']})
            run.assert_not_called()

    def test_missing_success_marker_blocks_retention(self):
        with patch.object(backup, 'no_legacy_jobs'), patch.object(backup, 'restic') as restic:
            with self.assertRaises(FileNotFoundError):
                backup.execute('maintain', {})
            restic.assert_not_called()

    def test_retention_requires_exact_successful_fresh_snapshot(self):
        config = {'host': 'test', 'tag': 'test'}
        backup.atomic_json(self.root / 'last-success.json', {
            'config': config, 'repository': 'test-repo', 'snapshot_id': 'abcdef12',
        })
        now = datetime.datetime.now(datetime.timezone.utc)
        for snapshot_id, timestamp, accepted in [
            ('abcdef123456', now, True),
            ('different', now, False),
            ('abcdef123456', now - datetime.timedelta(days=3), False),
        ]:
            with self.subTest(snapshot_id=snapshot_id, timestamp=timestamp):
                snapshots = json.dumps([{'id': snapshot_id, 'time': timestamp.isoformat()}])
                with patch.dict(os.environ, {'RESTIC_REPOSITORY': 'test-repo'}), patch.object(backup, 'restic', return_value=snapshots):
                    if accepted:
                        backup.require_recent_backup(config)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'No staged snapshot'):
                            backup.require_recent_backup(config)

    def test_force_kill_aborts_before_final_copy_and_recovers(self):
        states = [
            {'Running': True},
            {'Running': False, 'ExitCode': 137, 'OOMKilled': False},
            {'Running': True},
        ]
        with patch.object(backup, 'inspect', side_effect=[{'State': state} for state in states]), patch.object(backup, 'run') as run, patch.object(backup, 'sync_group') as sync:
            with self.assertRaisesRegex(RuntimeError, 'did not stop cleanly'):
                backup.stage_group({'stop_timeout': 120}, 'group', ['app'], [])
            sync.assert_called_once_with({'stop_timeout': 120}, 'group', [], final=False)
            self.assertEqual(run.call_args.args, ('docker', 'start', 'app'))

    def test_successful_upload_records_snapshot_for_activation(self):
        config = {'staging': str(self.root), 'required_mounts': [], 'host': 'test', 'tag': 'test'}
        summary = json.dumps({'message_type': 'summary', 'snapshot_id': 'abcdef12', 'files_new': 3,
                              'files_changed': 1, 'data_added': 2.5e9, 'total_duration': 90})
        with patch.dict(os.environ, {'RESTIC_REPOSITORY': 'test-repo'}), patch.object(backup, 'no_legacy_jobs'), patch.object(backup, 'plan', return_value=[('app', [], [], [])]), patch.object(backup, 'stage_group'), patch.object(backup, 'restic', return_value=summary):
            backup.execute('backup', config)
        marker = json.loads((self.root / 'last-success.json').read_text())
        self.assertEqual(marker['snapshot_id'], 'abcdef12')
        self.assertEqual(marker['repository'], 'test-repo')
        self.assertEqual(marker['config'], config)

    def test_credentials_reuse_vaulted_env_with_private_copy_and_cleanup(self):
        tasks = yaml.safe_load((ROOT / 'homeserver-backup.yml').read_text())[0]['tasks']
        extraction = tasks[0]
        self.assertTrue(extraction['no_log'])
        self.assertEqual(extraction['when'], 'not ansible_check_mode')
        temporary, copy, resolve, retain = extraction['block']
        self.assertEqual(temporary['ansible.builtin.tempfile']['state'], 'file')
        self.assertEqual(copy['ansible.builtin.copy']['src'], 'secrets.env')
        self.assertTrue(copy['ansible.builtin.copy']['decrypt'])
        self.assertEqual(copy['ansible.builtin.copy']['mode'], '0600')
        self.assertFalse(copy['diff'])
        command = resolve['ansible.builtin.command']
        self.assertEqual(command['argv'][-3:], ['config', '--format', 'json'])
        self.assertIn('{{ backup_env_file.path }}', command['argv'])
        environment = yaml.safe_load(command['stdin'])['services']['backup']['environment']
        required = {'RESTIC_REPOSITORY', 'RESTIC_PASSWORD', 'B2_ACCOUNT_ID', 'B2_ACCOUNT_KEY'}
        self.assertEqual(set(environment), required | {'PANGOLIN_BACKUP_API_KEY', 'DISCORD_BACKUP_WEBHOOK'})
        for name in required:
            self.assertEqual(environment[name], '${' + name + ':?required in secrets.env}')
            self.assertEqual(command['argv'][command['argv'].index(name) - 1], '-u')
        self.assertIn('services.backup.environment', retain['ansible.builtin.set_fact']['backup_environment'])
        cleanup = extraction['always'][0]
        self.assertEqual(cleanup['ansible.builtin.file']['state'], 'absent')
        self.assertEqual(cleanup['ansible.builtin.file']['path'], '{{ backup_env_file.path }}')
        writer = next(task for task in tasks if task.get('ansible.builtin.copy', {}).get('dest') == '/etc/homelab-backup/environment.json')
        self.assertTrue(writer['no_log'])
        self.assertFalse(writer['diff'])
        self.assertEqual(writer['ansible.builtin.copy']['mode'], '0600')

    def test_compose_persistent_mounts_have_explicit_coverage_or_exclusion(self):
        compose = yaml.safe_load((ROOT / 'homeserver/docker/compose.yml').read_text())
        config = yaml.safe_load((ROOT / 'homeserver/backup.yml').read_text())['backup_config']
        excluded = {'logs', 'jellyfin-cache', 'fetcharr-cache', 'fetcharr-logs', 'fileflows-temp', 'immich-model-cache'}
        excluded_services = {'backup', 'prune', 'check', 'node_exporter', 'cadvisor'}
        excluded_paths = ('/mnt/media', '/mnt/homelab_iscsi/languagetool', '/mnt/homelab_iscsi/prometheus')
        for name, service in compose['services'].items():
            if name in excluded_services:
                continue
            for mount in service.get('volumes', []):
                source, destination, *options = mount.split(':')
                if 'ro' in options or source in excluded or any(source == prefix or source.startswith(prefix + '/') for prefix in excluded_paths):
                    continue
                if source.startswith('/'):
                    owners = [group for group in config['groups'] if any(Path(source).is_relative_to(Path(path)) for path in group['paths'])]
                else:
                    owners = [group for group in config['groups'] if f'homeserver_{source}' in group.get('volumes', [])]
                self.assertEqual(len(owners), 1, (name, source))
                self.assertIn(name, owners[0]['services'], (name, source))


    def test_alert_muting_saves_prior_state_and_skips_already_disabled(self):
        calls = []

        def pangolin(settings, path, body=None):
            calls.append((path, body))
            return {'enabled': path.endswith('11')}

        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org', 'alert_rules': [11, 12]}
        with patch.object(backup, 'pangolin', pangolin):
            backup.mute_alerts({'pangolin': settings})
        saved = json.loads((self.root / 'alerts.json').read_text())
        self.assertEqual(saved['prior'], {'11': True, '12': False})
        self.assertEqual(calls, [
            ('org/org/alert-rule/11', None),
            ('org/org/alert-rule/12', None),
            ('org/org/alert-rule/11', {'enabled': False}),
        ])

    def test_no_pangolin_configuration_leaves_alerts_untouched(self):
        with patch.object(backup, 'pangolin', side_effect=AssertionError('must not call')):
            backup.mute_alerts({})
            backup.mute_alerts({'pangolin': {'base_url': 'x', 'org_id': 'y', 'alert_rules': []}})
        self.assertFalse((self.root / 'alerts.json').exists())

    def test_alerts_restore_to_prior_state_and_retry_only_failures(self):
        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org', 'alert_rules': [11, 12]}
        backup.atomic_json(self.root / 'alerts.json', {'settings': settings, 'prior': {'11': True, '12': False}})
        restored = []

        def pangolin(settings, path, body=None):
            if path.endswith('12'):
                raise RuntimeError('gateway unreachable')
            restored.append((path, body))
            return {}

        with patch.object(backup, 'pangolin', pangolin):
            with self.assertRaisesRegex(RuntimeError, 'gateway unreachable'):
                backup.restore_alerts()
        self.assertEqual(restored, [('org/org/alert-rule/11', {'enabled': True})])
        self.assertEqual(json.loads((self.root / 'alerts.json').read_text())['prior'], {'12': False})

        with patch.object(backup, 'pangolin', lambda settings, path, body=None: restored.append((path, body)) or {}):
            backup.restore_alerts()
        self.assertEqual(restored[-1], ('org/org/alert-rule/12', {'enabled': False}))
        self.assertFalse((self.root / 'alerts.json').exists())

    def test_staging_failure_restores_alerts_and_containers(self):
        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org', 'alert_rules': [11]}
        config = {'staging': str(self.root), 'required_mounts': [], 'host': 'test', 'tag': 'test', 'pangolin': settings}
        events = []

        def pangolin(settings, path, body=None):
            events.append(body['enabled'] if body else 'read')
            return {'enabled': True}

        with patch.object(backup, 'pangolin', pangolin), patch.object(backup, 'no_legacy_jobs'),                 patch.object(backup, 'plan', return_value=[('app', [], [], [])]),                 patch.object(backup, 'restic'),                 patch.object(backup, 'stage_group', side_effect=RuntimeError('stop failed')):
            with self.assertRaisesRegex(RuntimeError, 'stop failed'):
                backup.execute('backup', config)
        self.assertEqual(events, ['read', False, True])
        self.assertFalse((self.root / 'alerts.json').exists())

    def test_unmuting_waits_for_services_before_reenabling(self):
        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org', 'alert_rules': [11], 'grace_seconds': 45}
        backup.atomic_json(self.root / 'alerts.json', {'settings': settings, 'prior': {'11': True}})
        with patch.object(backup, 'pangolin', return_value={}), patch.object(backup.time, 'sleep') as sleep:
            backup.restore_alerts()
        sleep.assert_called_once_with(45)

    def test_pangolin_rejects_unsuccessful_response_and_sends_bearer_token(self):
        settings = {'base_url': 'https://api.test/v1/', 'org_id': 'org'}
        captured = {}

        class Reply:
            def __init__(self, payload):
                self.payload = payload
            def read(self):
                return json.dumps(self.payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *arguments):
                return False

        def urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['method'] = request.method
            captured['authorization'] = request.get_header('Authorization')
            return Reply(captured['payload'])

        with patch.dict(os.environ, {'PANGOLIN_BACKUP_API_KEY': 'key.secret'}),                 patch.object(backup.urllib.request, 'urlopen', urlopen):
            captured['payload'] = {'success': True, 'data': {'enabled': False}}
            self.assertEqual(backup.pangolin(settings, 'org/org/alert-rule/11'), {'enabled': False})
            self.assertEqual(captured['url'], 'https://api.test/v1/org/org/alert-rule/11')
            self.assertEqual(captured['method'], 'GET')
            self.assertEqual(captured['authorization'], 'Bearer key.secret')
            captured['payload'] = {'success': False, 'message': 'forbidden'}
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                backup.pangolin(settings, 'org/org/alert-rule/11', {'enabled': False})
            self.assertEqual(captured['method'], 'POST')

    def test_api_key_is_optional_and_vault_blank_values_are_dropped(self):
        tasks = yaml.safe_load((ROOT / 'homeserver-backup.yml').read_text())[0]['tasks']
        command = tasks[0]['block'][2]['ansible.builtin.command']
        environment = yaml.safe_load(command['stdin'])['services']['backup']['environment']
        self.assertEqual(environment['PANGOLIN_BACKUP_API_KEY'], '${PANGOLIN_BACKUP_API_KEY:-}')
        self.assertEqual(command['argv'][command['argv'].index('PANGOLIN_BACKUP_API_KEY') - 1], '-u')
        self.assertIn("rejectattr('value', 'eq', '')", tasks[0]['block'][3]['ansible.builtin.set_fact']['backup_environment'])
        assertion = next(task for task in tasks if 'ansible.builtin.assert' in task)['ansible.builtin.assert']['that']
        self.assertTrue(any('PANGOLIN_BACKUP_API_KEY' in clause for clause in assertion))


    def test_maintenance_page_forced_per_group_and_prior_settings_saved(self):
        settings = {
            'base_url': 'https://api.test/v1', 'org_id': 'org',
            'maintenance': {'title': 'Backup', 'message': 'Back soon', 'estimated_time': '10 minutes'},
        }
        calls = []

        def pangolin(settings, path, body=None):
            calls.append((path, body))
            return {
                'maintenanceModeEnabled': False,
                'maintenanceModeType': 'automatic',
                'maintenanceTitle': 'Old title',
                'maintenanceMessage': None,
                'maintenanceEstimatedTime': None,
            }

        with patch.object(backup, 'pangolin', pangolin):
            backup.enter_maintenance({'pangolin': settings}, 'gitea', [7])
        saved = json.loads((self.root / 'maintenance.json').read_text())
        self.assertEqual(saved['prior']['7'], {
            'maintenanceModeEnabled': False,
            'maintenanceModeType': 'automatic',
            'maintenanceTitle': 'Old title',
            'maintenanceMessage': None,
            'maintenanceEstimatedTime': None,
        })
        self.assertEqual(calls[0], ('resource/7', None))
        self.assertEqual(calls[1], ('resource/7', {
            'maintenanceModeEnabled': True,
            'maintenanceModeType': 'forced',
            'maintenanceTitle': 'Backup',
            'maintenanceMessage': 'Back soon',
            'maintenanceEstimatedTime': '10 minutes',
        }))

    def test_maintenance_restores_prior_values_not_blanket_disable(self):
        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org'}
        prior = {
            '7': {
                'maintenanceModeEnabled': True,
                'maintenanceModeType': 'automatic',
                'maintenanceTitle': 'Keep me',
                'maintenanceMessage': None,
                'maintenanceEstimatedTime': None,
            },
        }
        backup.atomic_json(self.root / 'maintenance.json', {'settings': settings, 'prior': prior})
        calls = []
        with patch.object(backup, 'pangolin', lambda s, path, body=None: calls.append((path, body)) or {}), \
                patch.object(backup.time, 'sleep'):
            backup.restore_maintenance()
        self.assertEqual(calls, [('resource/7', prior['7'])])
        self.assertFalse((self.root / 'maintenance.json').exists())

    def test_maintenance_restore_omits_null_mode_type_rejected_by_api(self):
        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org'}
        backup.atomic_json(self.root / 'maintenance.json', {'settings': settings, 'prior': {
            '7': {
                'maintenanceModeEnabled': False,
                'maintenanceModeType': None,
                'maintenanceTitle': None,
                'maintenanceMessage': None,
                'maintenanceEstimatedTime': None,
            },
        }})
        calls = []
        with patch.object(backup, 'pangolin', lambda s, path, body=None: calls.append(body) or {}), \
                patch.object(backup.time, 'sleep'):
            backup.restore_maintenance()
        self.assertNotIn('maintenanceModeType', calls[0])
        self.assertIs(calls[0]['maintenanceModeEnabled'], False)

    def test_maintenance_page_covers_full_group_downtime_and_clears_after_restart(self):
        settings = {'base_url': 'https://api.test/v1', 'org_id': 'org', 'maintenance': {}}
        events = []

        def pangolin(settings, path, body=None):
            if body is None:
                return {field: None for field in backup.MAINTENANCE_FIELDS} | {'maintenanceModeEnabled': False}
            events.append(('page', body['maintenanceModeEnabled']))
            return {}

        states = {'app': True}

        def run(*args, **kwargs):
            events.append((args[1], args[-1]))
            states[args[-1]] = args[1] == 'start'

        def inspect(container):
            return {'State': {'Running': states[container], 'ExitCode': 0, 'OOMKilled': False}}

        with patch.object(backup, 'pangolin', pangolin), patch.object(backup, 'sync_group'), \
                patch.object(backup, 'inspect', inspect), \
                patch.object(backup, 'run', run), patch.object(backup.time, 'sleep'):
            backup.stage_group({'stop_timeout': 120, 'pangolin': settings}, 'gitea', ['app'], [], [7])
        self.assertEqual(events, [('page', True), ('stop', 'app'), ('start', 'app'), ('page', False)])
        self.assertFalse((self.root / 'maintenance.json').exists())

    def test_group_resources_must_be_niceid_strings(self):
        config = {
            'staging': str(self.root / 'staging'), 'required_mounts': [], 'project': 'homeserver',
            'groups': [{'name': 'gitea', 'services': [], 'paths': [], 'resources': [7]}],
        }
        with self.assertRaisesRegex(RuntimeError, 'Invalid Pangolin niceId'):
            backup.plan(config)
        config['groups'][0]['resources'] = ['Parallel_Afro']
        with self.assertRaisesRegex(RuntimeError, 'Invalid Pangolin niceId'):
            backup.plan(config)

    def test_niceids_resolve_to_numeric_ids_before_any_container_stops(self):
        config = {
            'staging': str(self.root / 'staging'), 'required_mounts': [], 'minimum_free_bytes': 0,
            'project': 'homeserver',
            'pangolin': {'base_url': 'https://api.test/v1', 'org_id': 'org'},
            'groups': [{'name': 'gitea', 'services': [], 'paths': [], 'resources': ['parallel-afrotyphlops-schlegelii']}],
        }
        source = self.root / 'staging-source'
        source.mkdir()
        config['groups'][0]['paths'] = [str(source)]
        pages = [
            {'resources': [{'niceId': 'other-animal', 'resourceId': 3}], 'pagination': {'total': 2, 'pageSize': 1, 'page': 1}},
            {'resources': [{'niceId': 'parallel-afrotyphlops-schlegelii', 'resourceId': 9}], 'pagination': {'total': 2, 'pageSize': 1, 'page': 2}},
        ]
        requested = []

        def pangolin(settings, path, body=None):
            requested.append(path)
            return pages[len(requested) - 1]

        with patch.object(backup, 'pangolin', pangolin), patch.object(backup, 'require_mounts'):
            groups = backup.plan(config)
        self.assertEqual(groups[0][3], [9])
        self.assertEqual(requested, [
            'org/org/resources?page=1&pageSize=100',
            'org/org/resources?page=2&pageSize=100',
        ])

    def test_unknown_niceid_fails_before_downtime(self):
        config = {
            'staging': str(self.root / 'staging'), 'required_mounts': [], 'project': 'homeserver',
            'pangolin': {'base_url': 'https://api.test/v1', 'org_id': 'org'},
            'groups': [{'name': 'gitea', 'services': [], 'paths': [], 'resources': ['renamed-in-dashboard']}],
        }
        page = {'resources': [{'niceId': 'other-animal', 'resourceId': 3}], 'pagination': {'total': 1, 'pageSize': 100, 'page': 1}}
        with patch.object(backup, 'pangolin', return_value=page), patch.object(backup, 'require_mounts'):
            with self.assertRaisesRegex(RuntimeError, 'Unknown Pangolin resources: renamed-in-dashboard'):
                backup.plan(config)

    def test_configured_pangolin_scope_matches_declared_groups(self):
        config = yaml.safe_load((ROOT / 'homeserver/backup.yml').read_text())['backup_config']
        settings = config['pangolin']
        self.assertEqual(set(settings['maintenance']), {'title', 'message', 'estimated_time'})
        self.assertIsInstance(settings['maintenance_grace_seconds'], int)
        for group in config['groups']:
            for resource in group.get('resources', []):
                self.assertRegex(resource, r'^[a-z0-9][a-z0-9-]*$', group['name'])


    def setUpProgress(self):
        reset = {'stage': 'startup', 'staged': []}
        patcher = patch.dict(backup.PROGRESS, reset, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_webhook_configured_sends_nothing(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(backup.urllib.request, 'urlopen', side_effect=AssertionError('must not post')):
            backup.notify('title', ['line'], failed=False)

    def test_non_discord_webhook_is_refused(self):
        with patch.dict(os.environ, {'DISCORD_BACKUP_WEBHOOK': 'https://evil.example/hook'}), \
                patch.object(backup.urllib.request, 'urlopen', side_effect=AssertionError('must not post')):
            backup.notify('title', ['line'], failed=False)

    def test_webhook_failure_never_breaks_the_backup(self):
        with patch.dict(os.environ, {'DISCORD_BACKUP_WEBHOOK': 'https://discord.com/api/webhooks/1/abc'}), \
                patch.object(backup.urllib.request, 'urlopen', side_effect=OSError('discord down')):
            backup.notify('title', ['line'], failed=True)

    def test_success_notice_reports_snapshot_and_group_totals(self):
        self.setUpProgress()
        config = {'staging': str(self.root), 'required_mounts': [], 'host': 'homeserver', 'tag': 'test'}
        summary = json.dumps({
            'message_type': 'summary', 'snapshot_id': 'abcdef12', 'files_new': 5,
            'files_changed': 2, 'data_added': 3.2e9, 'total_duration': 5430,
        })
        posted = []
        with patch.dict(os.environ, {'RESTIC_REPOSITORY': 'test-repo'}), \
                patch.object(backup, 'no_legacy_jobs'), \
                patch.object(backup, 'plan', return_value=[('gitea', [], [], []), ('immich', [], [], [])]), \
                patch.object(backup, 'stage_group'), patch.object(backup, 'restic', return_value=summary), \
                patch.object(backup, 'notify', lambda title, lines, failed: posted.append((title, lines, failed))):
            backup.execute('backup', config)
        title, lines, failed = posted[0]
        self.assertFalse(failed)
        self.assertIn('homeserver', title)
        joined = '\n'.join(lines)
        self.assertIn('abcdef12', joined)
        self.assertIn('gitea, immich', joined)
        self.assertIn('3.20 GB', joined)
        self.assertIn('1:30:30', joined)

    def test_failure_notice_names_stage_group_and_leftover_state(self):
        self.setUpProgress()
        backup.PROGRESS['action'] = 'backup'
        backup.PROGRESS['stage'] = 'staging immich'
        backup.PROGRESS['staged'] = ['gitea', 'jellyfin']
        backup.atomic_json(self.root / 'stopped.json', ['immich_server'])
        backup.atomic_json(self.root / 'maintenance.json', {'settings': {}, 'prior': {}})
        lines = backup.failure_report(RuntimeError('Container did not stop cleanly: immich_server'))
        joined = '\n'.join(lines)
        self.assertIn('staging immich', joined)
        self.assertIn('did not stop cleanly: immich_server', joined)
        self.assertIn('gitea, jellyfin', joined)
        self.assertIn('containers left stopped', joined)
        self.assertIn('resources left in maintenance', joined)
        self.assertNotIn('alert rules left muted', joined)
        self.assertIn('journalctl -u homelab-backup.service', joined)

    def test_failure_notice_survives_empty_error_message(self):
        self.setUpProgress()
        self.assertIn('KeyError', '\n'.join(backup.failure_report(KeyError())))
        self.assertIn('143', '\n'.join(backup.failure_report(SystemExit(143))))

    def test_discord_payload_is_a_single_embed_within_length_limits(self):
        captured = {}

        class Reply:
            def __enter__(self):
                return self
            def __exit__(self, *arguments):
                return False

        def urlopen(request, timeout=None):
            captured['body'] = json.loads(request.data)
            captured['url'] = request.full_url
            return Reply()

        with patch.dict(os.environ, {'DISCORD_BACKUP_WEBHOOK': 'https://discord.com/api/webhooks/1/abc'}), \
                patch.object(backup.urllib.request, 'urlopen', urlopen):
            backup.notify('Backup FAILED', ['x' * 5000], failed=True)
        embed = captured['body']['embeds'][0]
        self.assertEqual(captured['url'], 'https://discord.com/api/webhooks/1/abc')
        self.assertEqual(embed['title'], 'Backup FAILED')
        self.assertEqual(embed['color'], 0xCC0000)
        self.assertLessEqual(len(embed['description']), 4000)

    def test_webhook_is_optional_in_vaulted_credentials(self):
        tasks = yaml.safe_load((ROOT / 'homeserver-backup.yml').read_text())[0]['tasks']
        command = tasks[0]['block'][2]['ansible.builtin.command']
        environment = yaml.safe_load(command['stdin'])['services']['backup']['environment']
        self.assertEqual(environment['DISCORD_BACKUP_WEBHOOK'], '${DISCORD_BACKUP_WEBHOOK:-}')
        self.assertEqual(command['argv'][command['argv'].index('DISCORD_BACKUP_WEBHOOK') - 1], '-u')
        assertion = next(task for task in tasks if 'ansible.builtin.assert' in task)['ansible.builtin.assert']['that']
        self.assertTrue(any('DISCORD_BACKUP_WEBHOOK' in clause for clause in assertion))


if __name__ == '__main__':
    unittest.main()
