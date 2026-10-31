import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class PluginScopeTests(unittest.TestCase):
    def test_package_import_and_profile_home_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'profile'; home.mkdir(mode=0o700)
            other=Path(tmp)/'other'; other.mkdir()
            spec=importlib.util.spec_from_file_location('sim_plugin_test',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
            module=importlib.util.module_from_spec(spec)
            import sys
            sys.modules[spec.name]=module
            spec.loader.exec_module(module)
            env={'HERMES_HOME':str(other),'SIM_CONNECTOR_HERMES_HOME':str(home),'SIM_CONNECTOR_DB':str(home/'sim-inbox.sqlite'),'SIM_CONNECTOR_OWNER':'owner','SIM_CONNECTOR_PROFILE':'kai'}
            args=dict(owner='owner',profile='kai',event_id='e1',kind='question',title='Title',body='Synthetic')
            with patch.dict(os.environ,env):
                self.assertFalse(module.configured_scope())
                self.assertFalse(json.loads(module.notify(args))['success'])
                self.assertFalse(Path(env['SIM_CONNECTOR_DB']).exists())
                os.environ['HERMES_HOME']=str(home)
                self.assertTrue(module.configured_scope())
                self.assertTrue(json.loads(module.notify(args))['success'])
                self.assertFalse(json.loads(module.notify(dict(args,profile='default')))['success'])
                from connector import canonical_store
                self.assertEqual(canonical_store(env['SIM_CONNECTOR_DB']),home/'sim-inbox.sqlite')
                with self.assertRaises(ValueError): canonical_store(home/'other.sqlite')
                alias=home/'alias.sqlite'; alias.symlink_to(home/'sim-inbox.sqlite')
                with self.assertRaises(ValueError): canonical_store(alias)
                os.environ['SIM_CONNECTOR_DB']=str(alias)
                self.assertFalse(module.configured_scope())
