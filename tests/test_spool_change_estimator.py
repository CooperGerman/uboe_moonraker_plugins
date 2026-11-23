import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
package = types.ModuleType("plugin_test")
package.__path__ = [str(ROOT)]
components = types.ModuleType("plugin_test.components")
components.__path__ = [str(ROOT)]
common = types.ModuleType("plugin_test.common")
common.JobEvent = str
metadata = types.ModuleType("plugin_test.components.uboe_metadata")
metadata.ExtrusionPoints = object
sys.modules.update({
	"plugin_test": package,
	"plugin_test.components": components,
	"plugin_test.common": common,
	"plugin_test.components.uboe_metadata": metadata,
})


def load_component(name):
	spec = importlib.util.spec_from_file_location(
		f"plugin_test.components.{name}", ROOT / f"{name}.py"
	)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


checks_module = load_component("additional_pre_print_checks")
estimator_module = load_component("spool_change_estimator")


class PreparationTests(unittest.IsolatedAsyncioTestCase):
	def setUp(self):
		self.estimator = estimator_module.SpoolChangeEstimator.__new__(
			estimator_module.SpoolChangeEstimator
		)
		self.estimator.printing = False
		self.estimator._job_active = False
		self.estimator._prep_checks_ok = False
		self.estimator.cumulated_length = {}
		self.estimator._highest_epos = {}
		self.estimator.additional_pre_print_checks = Mock()
		self.estimator.additional_pre_print_checks._prep_checks = AsyncMock()
		self.estimator._log_to_console = AsyncMock()

	async def test_start_resume_and_manual_request_do_not_prepare(self):
		await self.estimator._on_print_started()
		await self.estimator._on_print_paused()
		await self.estimator._on_print_resumed()
		await self.estimator.cmd_UBOE_SPOOL_CHANGE_ESTIMATE()
		self.estimator.additional_pre_print_checks._prep_checks.assert_not_awaited()
		self.estimator._log_to_console.assert_awaited_once()
		self.assertFalse(self.estimator._prep_checks_ok)

	async def test_ready_event_during_pause_and_end_reset(self):
		await self.estimator._on_print_started()
		await self.estimator._on_print_paused()
		self.estimator._on_checks_prepared([0, 3])
		self.assertTrue(self.estimator._prep_checks_ok)
		self.assertEqual(self.estimator.cumulated_length, {0: 0.0, 3: 0.0})
		self.estimator.cumulated_length[0] = 4.0
		self.estimator._on_checks_prepared([0, 3])
		self.assertEqual(self.estimator.cumulated_length[0], 4.0)
		await self.estimator._on_print_end()
		self.estimator._on_checks_prepared([0])
		self.assertFalse(self.estimator._prep_checks_ok)
		self.assertEqual(self.estimator.cumulated_length, {})

	async def test_pre_print_checks_notify_only_after_successful_preparation(self):
		checks = checks_module.AdditionalPrePrintChecks.__new__(
			checks_module.AdditionalPrePrintChecks
		)
		checks.server = Mock()
		checks.server.send_event = AsyncMock()
		checks._is_hh_enabled = AsyncMock()
		checks.is_hh = True
		checks._prep_checks = AsyncMock(return_value=Mock(multi=True))
		checks.extracted_metadata = Mock(referenced_tools=[0, 3])
		checks.filename = "test.gcode"
		checks._log_to_console = AsyncMock()
		checks._clear_spool_cache = Mock()
		checks.klippy_apis = Mock(pause_print=AsyncMock())
		checks.enable_weight_check = False
		checks.enable_spool_change_on_work_hours_check = False
		checks.enable_filament_name_check = False
		checks.enable_spoolman_setup_check = False
		checks.enable_material_check = False
		checks.error_body = []

		await checks.run_checks()
		checks.server.send_event.assert_awaited_once_with(
			"additional_pre_print_checks:prepared", [0, 3]
		)

		checks.server.send_event.reset_mock()
		checks._prep_checks.return_value = False
		await checks.run_checks()
		checks.server.send_event.assert_not_awaited()
		checks.klippy_apis.pause_print.assert_awaited_once()


if __name__ == "__main__":
	unittest.main()
