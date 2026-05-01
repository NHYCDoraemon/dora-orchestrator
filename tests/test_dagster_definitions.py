import importlib.util
import unittest


class DagsterDefinitionsTest(unittest.TestCase):
    def test_definitions_import_when_dagster_installed(self):
        if importlib.util.find_spec("dagster") is None:
            self.skipTest("dagster not installed")
        from dora_orchestrator.dagster_defs.dora_definitions import defs

        self.assertIsNotNone(defs)
