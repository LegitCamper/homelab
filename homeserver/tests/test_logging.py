"""Self-check: logging stack wired up (promtail -> loki -> grafana)."""
from pathlib import Path
import unittest
import yaml

D = Path(__file__).resolve().parents[1] / 'docker'


class LoggingStackTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((D / 'compose.yml').read_text())
        self.promtail = yaml.safe_load((D / 'promtail/config.yml').read_text())
        self.loki = yaml.safe_load((D / 'loki/config.yml').read_text())
        self.datasources = yaml.safe_load((D / 'grafana/datasource.yml').read_text())

    def test_promtail_scrapes_docker_not_dead_volume(self):
        jobs = self.promtail['scrape_configs']
        self.assertTrue(any('docker_sd_configs' in j for j in jobs))
        self.assertFalse(any('static_configs' in j for j in jobs))

    def test_promtail_can_read_docker_socket_and_logs(self):
        vols = self.compose['services']['promtail']['volumes']
        self.assertTrue(any('/var/run/docker.sock' in v for v in vols))
        self.assertTrue(any('/var/lib/docker/containers' in v for v in vols))

    def test_promtail_pushes_to_loki_on_shared_network(self):
        url = self.promtail['clients'][0]['url']
        self.assertIn('loki:3100', url)
        for svc in ('promtail', 'loki', 'grafana'):
            self.assertIn('logs-internal', self.compose['services'][svc]['networks'])

    def test_loki_storage_is_persistent_not_tmp(self):
        paths = [
            self.loki['common']['path_prefix'],
            self.loki['storage_config']['filesystem']['directory'],
            self.loki['compactor']['working_directory'],
        ]
        for p in paths:
            self.assertFalse(p.startswith('/tmp'), p)
        vols = self.compose['services']['loki']['volumes']
        self.assertTrue(any(v.startswith('loki-data:/loki') for v in vols))
        self.assertIn('loki-data', self.compose['volumes'])

    def test_loki_retention_enabled(self):
        self.assertTrue(self.loki['compactor']['retention_enabled'])
        self.assertIn('retention_period', self.loki['limits_config'])

    def test_grafana_has_loki_datasource(self):
        types = {d['type']: d['url'] for d in self.datasources['datasources']}
        self.assertEqual(types.get('loki'), 'http://loki:3100')
        self.assertEqual(types.get('prometheus'), 'http://prometheus:9090')


if __name__ == '__main__':
    unittest.main()
