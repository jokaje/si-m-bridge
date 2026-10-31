"""Mocked OAuth resolver contract; no live provider credentials or network."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_runtime import check_compatibility


class OAuthRoutingTests(unittest.TestCase):
    def test_selected_home_oauth_does_not_inherit_custom_endpoint(self):
        from hermes_cli.oneshot import _resolve_model_and_provider
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_with_fallback
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            home = Path(tmp)
            (home/'config.yaml').write_text('model:\n  provider: openai-codex\n  default: gpt-5\n')
            # Isolate even when the operator's shell has a custom override.
            with patch.dict(os.environ, {'HERMES_HOME': str(home), 'HERMES_INFERENCE_MODEL': '',
                                         'HERMES_INFERENCE_PROVIDER': ''}):
                cfg = load_config()
                choice = _resolve_model_and_provider(cfg, None, None)
                self.assertEqual(choice.model, 'gpt-5')
                self.assertIsNone(choice.base_url)
                self.assertIsNone(choice.api_key)
                self.assertIn(choice.provider, (None, 'openai-codex'))
                with patch('hermes_cli.runtime_provider.resolve_runtime_provider', return_value={
                        'provider': 'openai-codex', 'base_url': 'https://oauth.example/v1', 'api_key': 'mock-oauth'}) as resolver:
                    route, fallback = resolve_runtime_with_fallback(cfg, requested=choice.provider,
                        target_model=choice.model, explicit_base_url=choice.base_url,
                        explicit_api_key=choice.api_key)
                    self.assertIsNone(fallback)
                    self.assertEqual(route['provider'], 'openai-codex')
                    self.assertIsNone(resolver.call_args.kwargs['explicit_base_url'])
                    self.assertIsNone(resolver.call_args.kwargs['explicit_api_key'])

    def test_private_api_drift_fails_closed(self):
        import hermes_cli.oneshot as oneshot
        with patch.object(oneshot, '_resolve_model_and_provider', lambda cfg: None):
            with self.assertRaisesRegex(RuntimeError, 'unsupported Hermes connector integration API'):
                check_compatibility()
