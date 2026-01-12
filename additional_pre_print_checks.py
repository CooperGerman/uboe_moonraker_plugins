# Additional Pre-Print Checks Plugin
# Moonraker plugin to perform pre-print validation checks
#
# This plugin verifies sufficient filament weight is available before printing
# by comparing metadata weight requirements against Spoolman active spool data
#
from __future__ import annotations
import logging
import asyncio
from typing import TYPE_CHECKING, Dict, Any, Optional

if TYPE_CHECKING:
	from ..components.spoolman import SpoolManager
	from ..confighelper import ConfigHelper
	from ..components.http_client import HttpClient
	from ..components.database import MoonrakerDatabase
	from ..components.klippy_apis import KlippyAPI as APIComp
	from ..components.file_manager.file_manager import FileManager

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"


class AdditionalPrePrintChecks:
	def __init__(self, config: ConfigHelper):
		self.config = config
		self.server = config.get_server()
		self.spoolman: Optional[SpoolManager] = None

		# Load components
		if config.has_section("spoolman"):
			self.spoolman = self.server.load_component(config, "spoolman", None)

		self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
		self.http_client: HttpClient = self.server.lookup_component("http_client")
		self.database: MoonrakerDatabase = self.server.lookup_component("database")
		self.file_manager: FileManager = self.server.lookup_component("file_manager")

		# Configuration
		self.weight_margin = self.config.getfloat("weight_margin_grams", 5.0)
		self.enable_check = self.config.getboolean("enable_weight_check", True)

		# Register remote methods
		if self.spoolman and self.enable_check:
			self.server.register_remote_method(
					"pre_print_check_weight",
					self.check_print_weight
			)
			logging.info("Additional Pre-Print Checks: Weight validation enabled")
		else:
			logging.info("Additional Pre-Print Checks: Disabled (spoolman not available or check disabled)")

	async def component_init(self) -> None:
		"""Initialize component"""
		if self.spoolman and self.enable_check:
			logging.info("Additional Pre-Print Checks component initialized")

	async def _get_active_spool_id(self) -> Optional[int]:
		"""Get currently active spool ID from database"""
		try:
			result = await self.database.get_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY)
			return int(result) if result is not None else None
		except Exception as e:
			logging.error(f"Failed to get active spool ID: {e}")
			return None

	async def _fetch_spool_info(self, spool_id: int) -> Optional[Dict[str, Any]]:
		"""Retrieve spool information from Spoolman"""
		try:
			response = await self.http_client.request(
					method="GET",
					url=f'{self.spoolman.spoolman_url}/v1/spool/{spool_id}',
					body=None
			)

			if response.status_code == 404:
					logging.error(f"Spool {spool_id} not found in Spoolman")
					return None
			elif response.has_error():
					err_msg = self.spoolman._get_response_error(response)
					logging.error(f"Failed to fetch spool info: {err_msg}")
					return None

			return response.json()
		except Exception as e:
			logging.error(f"Exception fetching spool info: {e}")
			return None

	async def _log_to_console(self, msg: str, error: bool = False) -> None:
		"""Send message to Klipper console"""
		if error:
			logging.error(msg)
		else:
			logging.info(msg)

		try:
			msg = msg.replace("\n", "\\n")
			await self.klippy_apis.run_gcode(f"M118 {msg}")
		except Exception as e:
			logging.error(f"Failed to send message to console: {e}")

	async def check_print_weight(self, filename: str) -> bool:
		"""
		Check if active spool has sufficient weight for the print job

		Args:
			filename: Path to gcode file (e.g., "gcodes/test.gcode")

		Returns:
			True if sufficient weight available or check not applicable, False otherwise
		"""
		if not self.spoolman or not self.enable_check:
			return True

		# Get active spool ID
		spool_id = await self._get_active_spool_id()
		if spool_id is None:
			await self._log_to_console("No active spool set, skipping weight check")
			return True

		# Get file metadata
		try:
			metadata = self.file_manager.get_file_metadata(filename)
		except Exception as e:
			logging.error(f"Failed to get metadata for {filename}: {e}")
			await self._log_to_console(f"Cannot read file metadata: {e}", error=True)
			return True  # Don't block print on metadata errors

		# Extract required weight from metadata
		required_weight = metadata.get('filament_weight_total')
		if required_weight is None:
			await self._log_to_console("No weight data in file metadata, skipping check")
			return True

		# Get spool info
		spool_info = await self._fetch_spool_info(spool_id)
		if spool_info is None:
			await self._log_to_console(f"Cannot fetch spool {spool_id} info", error=True)
			return True  # Don't block on spool fetch errors

		# Get remaining weight
		remaining_weight = spool_info.get('remaining_weight')
		if remaining_weight is None:
			await self._log_to_console("Spool has no remaining weight data, skipping check")
			return True

		# Perform check
		required_with_margin = required_weight + self.weight_margin
		sufficient = remaining_weight >= required_with_margin

		filament = spool_info.get('filament', {})
		filament_name = filament.get('name', 'Unknown')
		material = filament.get('material', 'Unknown')

		if sufficient:
			msg = (f"Weight Check PASSED: Spool {spool_id} ({filament_name}) "
						f"has {remaining_weight:.1f}g, need {required_weight:.1f}g "
						f"(+{self.weight_margin:.1f}g margin)")
			await self._log_to_console(msg)
			return True
		else:
			deficit = required_with_margin - remaining_weight
			msg = (f"Weight Check FAILED: Spool {spool_id} ({filament_name}) "
						f"has only {remaining_weight:.1f}g, need {required_weight:.1f}g "
						f"(+{self.weight_margin:.1f}g margin). SHORT BY {deficit:.1f}g!")
			await self._log_to_console(msg, error=True)
			return False


def load_component(config: ConfigHelper) -> AdditionalPrePrintChecks:
	return AdditionalPrePrintChecks(config)
