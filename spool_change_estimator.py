# Spool Change Estimator Plugin
# Moonraker plugin to perform spool change estimations during print
#
# This plugin updates the spool change estimation data in the database and provides an API to retrieve it.
from __future__ import annotations
import json
import logging
import asyncio
from logging import config, error
import os
from typing import TYPE_CHECKING, Dict, Any, Optional, List
from packaging.version import Version
from datetime import datetime, time, timedelta
from .additional_pre_print_checks import AdditionalPrePrintChecks
from ..components.uboe_metadata import ExtrusionPoints

if TYPE_CHECKING:
	from ..components.spoolman import SpoolManager
	from ..components.mmu_server import MmuServer
	from ..confighelper import ConfigHelper, ConfigError
	from ..components.klippy_apis import KlippyAPI as APIComp
	from ..components.klippy_connection import KlippyConnection
	from ..components.file_manager.file_manager import FileManager
	from ..components.file_manager.metadata import MetadataStorage
	from ..components.database import MoonrakerDatabase

class SpoolChangeEstimator:
	def __init__(self, config: ConfigHelper):
		self.config = config
		self.server = config.get_server()
		self.server.register_remote_method(
			"uboe_spool_change_estimate",
			self.cmd_UBOE_SPOOL_CHANGE_ESTIMATE
		)

	async def cmd_UBOE_SPOOL_CHANGE_ESTIMATE(self, extr_id, volume) -> None:
		"""Estimate the spool change for a given extruder ID and volume."""
		# cast args to correct types directly
		extr_id = int(extr_id)
		volume = float(volume)
		if not self.additional_pre_print_checks.enabled:
			await self._log_to_console("Additional Pre-Print Checks component is not enabled. Spool Change Estimator will not function properly.", "warning", "Spool Change Estimator Initialization")
		# get current remaining from active spool (spoolman)
		spool_id = await self.additional_pre_print_checks._init_spool()
		if spool_id is None:
			await self._log_to_console("No active spool found. Cannot estimate spool change.", "warning")
			return

		current_spool = await self.additional_pre_print_checks._fetch_spool_info(spool_id)
		if current_spool is None:
			await self._log_to_console(f"Cannot fetch spool info for spool ID {spool_id}. Cannot estimate spool change.", "error")
			return

		current_remaining_g = current_spool.get("remaining_weight")
		density = current_spool.get("filament").get("density")
		spool_size_g = current_spool.get("filament").get("weight")
		if not all([current_remaining_g, density, spool_size_g]):
			await self._log_to_console(f"Spool info is incomplete for spool ID {spool_id}. Cannot estimate spool change.", "error")
			return

		# start sample point for estimation (get from volume and extr_id associated to sample point)
		if not self.additional_pre_print_checks.extracted_metadata:
			await self.additional_pre_print_checks._prep_checks()
		if not self.additional_pre_print_checks.extracted_metadata:
			await self._log_to_console("No extracted metadata found. Cannot estimate spool change.", "error")
			return
		if not self.additional_pre_print_checks.extracted_metadata.extrusion_sample_points:
			await self._log_to_console("No extrusion sample points found in extracted metadata. Cannot estimate spool change.", "error")
			return
		point = self.additional_pre_print_checks.extracted_metadata.extrusion_sample_points.has_point(extr_id, volume)
		if not point:
			await self._log_to_console(f"No sample points found starting from extruder ID {extr_id} and volume {volume}, using closest. UBOE_SPOOL_CHANGE_ESTIMATE command and parsed extrusion points should match. (See moonraker.log for list of searched points)", "warning")

		runouts = self.additional_pre_print_checks.estimate_runouts(current_remaining_g=current_remaining_g, density=density, spool_size_g=spool_size_g, start_volume=volume, extr_id=extr_id)
		if not runouts:
			# await self._log_to_console(f"No spool change runouts estimated for extruder ID {extr_id} and volume {volume}.", "debug")
			return

		nxt_runout = runouts[0]
		eta = (datetime.now() + timedelta(minutes=nxt_runout.estimated_minutes_from_now))
		await self._log_to_console(f"Estimated spool change for extruder {extr_id} is:", "info")
		await self._log_to_console(f"   in {nxt_runout.estimated_minutes_from_now:.2f} min ({str(timedelta(minutes=nxt_runout.estimated_minutes_from_now))[:-3]})", "info", reason='')
		await self._log_to_console(f"   ETA : {eta.strftime('%Y-%m-%d %H:%M:%S')}", "info", reason='')
		await self._log_to_console(f"   layer : {nxt_runout.estimated_layer}", "info", reason='')

	async def component_init(self) -> None:
		"""Initialize component"""
		try:
			self.additional_pre_print_checks : AdditionalPrePrintChecks = self.server.lookup_component("additional_pre_print_checks")
		except Exception as e:
			raise self.config.error(f"[{self.config.get_name()}]: {e}")
		logging.info("Spool Change Estimator component initialized")

	async def _log_to_console(self, msg: str = "Empty message", severity: str = "info", reason: str = "Spool Change Estimate", popup: bool = False) -> None:
		await self.additional_pre_print_checks._log_to_console(msg, severity, reason, popup=popup)

def load_component(config: ConfigHelper) -> SpoolChangeEstimator:
	return SpoolChangeEstimator(config)