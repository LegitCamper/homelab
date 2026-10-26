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
        with patch.object(backup, 'no_legacy_jobs'), patch.object(backup, 'plan', return_value=[('app', [], [])]), patch.object(backup, 'stage_group') as stage, patch.object(backup, 'restic', restic):
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
        summary = json.dumps({'message_type': 'summary', 'snapshot_id': 'abcdef12'})
        with patch.dict(os.environ, {'RESTIC_REPOSITORY': 'test-repo'}), patch.object(backup, 'no_legacy_jobs'), patch.object(backup, 'plan', return_value=[('app', [], [])]), patch.object(backup, 'stage_group'), patch.object(backup, 'restic', return_value=summary):
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
        self.assertEqual(set(environment), required)
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


if __name__ == '__main__':
    unittest.main()
