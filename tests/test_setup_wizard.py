"""Exposure planning must be safe, honest about prerequisites, and side-effect free."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import setup_wizard as sw


class ModeCatalogTest(unittest.TestCase):
    def test_every_documented_mode_plans(self):
        for mode in sw.MODES:
            plan = sw.plan_exposure(mode, domain='bridge.example.com', url='https://bridge.example.com')
            self.assertEqual(plan['mode'], mode)
            self.assertIsNotNone(plan['base_url'], mode)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            sw.plan_exposure('open-the-floodgates')

    def test_only_loopback_is_not_device_capable(self):
        for mode, entry in sw.MODES.items():
            self.assertEqual(entry['device_capable'], mode != 'loopback', mode)


class DeviceUrlTest(unittest.TestCase):
    def test_plain_http_rejected(self):
        # The firmware trusts system CAs only, so plain HTTP can never be the base URL.
        with self.assertRaises(ValueError):
            sw.validate_public_url('http://bridge.example.com')

    def test_lan_ip_rejected_without_tls(self):
        with self.assertRaises(ValueError):
            sw.validate_public_url('http://192.168.86.20:8766')

    def test_loopback_https_rejected(self):
        with self.assertRaises(ValueError):
            sw.validate_public_url('https://127.0.0.1')

    def test_credentials_and_paths_rejected(self):
        for bad in ('https://user:pw@bridge.example.com', 'https://bridge.example.com/api',
                    'https://bridge.example.com/?x=1', 'https://bridge.example.com/#f'):
            with self.assertRaises(ValueError, msg=bad):
                sw.validate_public_url(bad)

    def test_https_host_accepted_and_normalised(self):
        self.assertEqual(sw.validate_public_url('https://bridge.example.com/'), 'https://bridge.example.com')
        self.assertEqual(sw.validate_public_url('https://bridge.example.com:8443'), 'https://bridge.example.com:8443')


class PrerequisiteTest(unittest.TestCase):
    def test_missing_domain_is_reported_not_raised(self):
        plan = sw.plan_exposure('caddy', domain=None)
        self.assertIn('Domain oder Hostname fuer den Endpunkt', plan['missing'])
        self.assertIn('Caddyfile', plan['files'])

    def test_tailscale_without_node_reports_missing_and_placeholder(self):
        plan = sw.plan_exposure('tailscale', tailnet_name=None)
        if plan['missing']:
            self.assertIn('https://<dein-node>.<dein-tailnet>.ts.net', plan['base_url'])

    def test_tailscale_with_node_builds_https_url(self):
        plan = sw.plan_exposure('tailscale', tailnet_name='orb-host.tail1234.ts.net')
        self.assertEqual(plan['base_url'], 'https://orb-host.tail1234.ts.net')
        self.assertEqual(plan['missing'], [])
        self.assertTrue(any('tailscale serve' in s for s in plan['steps']))

    def test_generated_configs_carry_no_secrets(self):
        for mode in ('caddy', 'nginx', 'cloudflare'):
            plan = sw.plan_exposure(mode, domain='bridge.example.com')
            blob = '\n'.join(plan['files'].values())
            for marker in ('PRIVATE KEY', 'Bearer ', 'password', 'token='):
                self.assertNotIn(marker, blob, mode)

    def test_nginx_config_disables_buffering(self):
        plan = sw.plan_exposure('nginx', domain='bridge.example.com')
        config = plan['files']['si-m-bridge.conf']
        self.assertIn('proxy_buffering off', config)
        self.assertIn('client_max_body_size 256k', config)

    def test_port_reaches_generated_config(self):
        plan = sw.plan_exposure('caddy', domain='bridge.example.com', port=9999)
        self.assertIn('127.0.0.1:9999', plan['files']['Caddyfile'])

    def test_invalid_port_rejected(self):
        with self.assertRaises(ValueError):
            sw.plan_exposure('caddy', domain='bridge.example.com', port=0)


class WriteTest(unittest.TestCase):
    def test_write_refuses_overwrite(self):
        plan = sw.plan_exposure('caddy', domain='bridge.example.com')
        with tempfile.TemporaryDirectory() as tmp:
            written = sw.write_plan_files(plan, tmp)
            self.assertEqual(len(written), 1)
            with self.assertRaises(FileExistsError):
                sw.write_plan_files(plan, tmp)

    def test_render_mentions_device_capability(self):
        text = sw.render_plan(sw.plan_exposure('loopback'))
        self.assertIn('NEIN', text)


class EnvBlockTest(unittest.TestCase):
    def test_env_block_is_consistent(self):
        plan = sw.plan_exposure('tailscale', tailnet_name='host.tail1.ts.net')
        lines = sw.env_lines(plan, profile='work', home='/srv/hermes', owner='owner-a', device='orb-a')
        joined = '\n'.join(lines)
        self.assertIn('SIM_CONNECTOR_DB=/srv/hermes/sim-inbox.sqlite', joined)
        self.assertIn('SIM_CONNECTOR_PROFILE=work', joined)
        self.assertIn('SIM_BRIDGE_BASE_URL=https://host.tail1.ts.net', joined)


if __name__ == '__main__':
    unittest.main()
