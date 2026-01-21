# Additional Pre-Print Checks Plugin
# Moonraker plugin to perform pre-print validation checks
#
# This plugin verifies sufficient filament weight is available before printing
# by comparing metadata weight requirements against Spoolman active spool data
#
from __future__ import annotations
import json
import logging
import asyncio
from logging import config, error
import os
from typing import TYPE_CHECKING, Dict, Any, Optional, List

if TYPE_CHECKING:
	from ..components.spoolman import SpoolManager
	from ..components.mmu_server import MmuServer
	from ..confighelper import ConfigHelper
	from ..components.klippy_apis import KlippyAPI as APIComp
	from ..components.klippy_connection import KlippyConnection
	from ..components.file_manager.file_manager import FileManager
	from ..components.file_manager.metadata import MetadataStorage
	from ..components.database import MoonrakerDatabase

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"

class AdditionalPrePrintChecks:
	def __init__(self, config: ConfigHelper):
		self.config = config
		self.server = config.get_server()
		self.spoolman: Optional[SpoolManager] = None
		self.mmu_server: Optional[MmuServer] = None
		self.error_body = []

		# Load components
		if config.has_section("spoolman"):
			self.spoolman = self.server.load_component(config, "spoolman", None)

		self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
		self.file_manager: FileManager = self.server.lookup_component("file_manager")
		self.metadata_storage: MetadataStorage = self.file_manager.get_metadata_storage()
		self.database: MoonrakerDatabase = self.server.lookup_component("database")
		self.klippy_connection: KlippyConnection = self.server.lookup_component("klippy_connection")
		# Configuration
		self.weight_margin = self.config.getfloat("weight_margin_grams", 5.0)
		self.enable_weight_check = self.config.getboolean("enable_weight_check", True)
		self.enable_material_check = self.config.getboolean("enable_material_check", True)
		self.enable_filament_name_check = self.config.getboolean("enable_filament_name_check", False)

		self.multi_tool_mapping =  False

		# Mismatch severity levels: 'error', 'warning', 'info', 'ignore'
		self.material_mismatch_severity = self.config.get("material_mismatch_severity", "warning")
		self.filament_name_mismatch_severity = self.config.get("filament_name_mismatch_severity", "info")

		# Cache for spool data during check
		self.cached_spool_info: Optional[Dict[str, Any]] = None
		self.cached_spool_id: Optional[int] = None

		# Init mmu_server component
		self.is_hh = False

		# Register remote methods
		if self.spoolman:
			self.server.register_remote_method(
				"pre_print_checks",
				self.run_checks
			)
			logging.info("Additional Pre-Print Checks: Enabled")
		else:
			logging.info("Additional Pre-Print Checks: Disabled (spoolman not available)")

	async def component_init(self) -> None:
		"""Initialize component"""
		if self.spoolman:
			await self._init_spool()
			logging.info("Additional Pre-Print Checks component initialized")

		# Create a background task to wait for connection and finish init
		# We use a task because blocking component_init on wait_connected would cause a deadlock
		asyncio.create_task(self._finish_init(3))

	async def _finish_init(self, retry=3) -> None:
		"""Wait for Klippy connection then finish initialization"""
		# Wait for Klippy to be connected
		for __ in range(retry):
			connected = await self.klippy_connection.wait_connected()
			logging.info("Additional Pre-Print Checks: Klippy connected, finishing init")

			if connected:
				break
			await asyncio.sleep(2)
		# Look up MMU server now that we are connected
		if self.config.has_section("mmu_server"):
				self.mmu_server = self.server.lookup_component("mmu_server", None)

		logging.info(f"Additional Pre-Print Checks: MMU Server = {self.mmu_server is not None}")

		if self.mmu_server:
			await self.mmu_server._initialize_mmu()
		# Initialize metadata script override
		self._is_hh_enabled()
		if not self.is_hh:
			self._init_metadata_script()

	def _init_metadata_script(self) -> None:
		from .file_manager import file_manager
		current_dir = os.path.dirname(os.path.abspath(__file__))
		file_manager.METADATA_SCRIPT = current_dir + "/super_metadata.py"
		logging.info("Additional Pre-Print Checks: Set new metadata script for enhanced parsing")

	def _is_hh_enabled(self) -> bool:
		"""Check if MMU backend is present and enabled"""
		if self.mmu_server is None:
			self.is_hh = False
			return False
		self.is_hh = self.mmu_server._mmu_backend_enabled()
		return self.is_hh

	async def _init_spool(self) -> Optional[int]:
		"""
		Get active spool ID from database and initialize/cache spool data.
		Combines getting active spool ID and initializing spool data functionality.

		Returns:
			Spool ID if successful, None if no active spool or fetch failed
		"""
		if not self.spoolman:
			return None

		try:
			# Get active spool ID from database
			spool_id = await self.database.get_item(
				DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
			)
			if spool_id is None:
				return None

			# Check if already cached
			if self.cached_spool_id == spool_id and self.cached_spool_info is not None:
				return spool_id

			# Fetch and cache spool info
			self.cached_spool_info = await self._fetch_spool_info(spool_id)
			if self.cached_spool_info is None:
				self.cached_spool_id = None
				return None

			self.cached_spool_id = spool_id
			return spool_id
		except Exception as e:
			logging.error(f"Failed to initialize spool data: {e}")
			return None

	def _clear_spool_cache(self) -> None:
		"""Clear cached spool data"""
		self.cached_spool_info = None
		self.cached_spool_id = None

	async def _fetch_spool_info(self, spool_id: int) -> Optional[Dict[str, Any]]:
		"""Retrieve spool information from Spoolman (same method as mmu_server)"""
		try:
			response = await self.spoolman.http_client.request(
				method="GET",
				url=f'{self.spoolman.spoolman_url}/v1/spool/{spool_id}',
				body=None
			)
			if response.status_code == 404:
				logging.error(f"Spool {spool_id} not found in Spoolman")
				return None
			elif response.has_error():
				logging.error(f"Error fetching spool {spool_id}: HTTP {response.status_code}")
				return None
			return response.json()
		except Exception as e:
			logging.error(f"Failed to fetch spool {spool_id}: {e}")
			return None

	async def _get_current_filename(self) -> Optional[str]:
		"""
		Get the currently printing or selected filename from Klipper

		Returns:
			Filename or None if not available
		"""
		try:
			result = await self.klippy_apis.query_objects({'print_stats': None})
			filename = result.get('print_stats', {}).get('filename')
			return filename if filename else None
		except Exception as e:
			logging.error(f"Failed to get current filename: {e}")
			return None

	async def _log_to_console(self, msg: str, severity: str = "info", reason: str = "Pre-Print Check Failed") -> None:
		"""
		Send message to Klipper console with appropriate severity

		Args:
			msg: Message to log
			severity: 'error', 'warning', or 'info'
		"""
		if severity == "error":
			logging.error(msg)
		elif severity == "warning":
			logging.warning(msg)
		else:
			logging.info(msg)

		try:
			if self.is_hh:
				error_flag = "ERROR=1" if severity == "error" else ""
				msg = msg.replace("\n", "\\n") # Get through klipper filtering
				await self.klippy_apis.run_gcode(f"MMU_LOG MSG='{msg}' {error_flag}")
			else:
				msg = msg.replace("\n", "\\n")
				if severity == "error":
					await self.klippy_apis.run_gcode('_UBOE_ERROR_DIALOG MSG="%s" REASON="%s"' % (msg, reason))
				else :
					await self.klippy_apis.run_gcode(f"M118 {msg}")
		except Exception as e:
			logging.error(f"Failed to send message to console: {e}")

	async def check_print_weight(self, filename: str) -> bool:
		"""
		Check if active spool has sufficient weight for the print job

		Args:
			filename: Path to gcode file (e.g., "gcodes/test.gcode")

		Returns:
			True if check passed or not applicable, False if failed
		"""
		if not self.spoolman or not self.enable_weight_check:
			return True

		# Get active spool and initialize data
		spool_id = await self._init_spool()
		if spool_id is None:
			self.error_body.append("No active spool set or cannot fetch spool info")
			return False

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			self.error_body.append(f"Metadata not available for {filename}")
			return False

		# Extract required weight from metadata
		required_weight = metadata.get('filament_weights')

		if required_weight is None:
			self.error_body.append("No filament weight data in file metadata")
			return False

		# if it is not a list, make it a list
		if not isinstance(required_weight, list):
			required_weight = [required_weight]

		if self.multi_tool_mapping:
			tool_range = range(len(self.multi_tool_mapping))
		else:
			tool_range = range(1)  # Single tool T0

		# Check each tool/spool
		weight_ok = True
		for tool_index in tool_range:
			if self.multi_tool_mapping:
				current_spool_id =  self.multi_tool_mapping[tool_index]
				self.cached_spool_info = await self._fetch_spool_info(current_spool_id)
				if self.cached_spool_info is None:
					self.error_body.append(f"Cannot fetch spool info for tool {tool_index} (spool ID {current_spool_id})")
					return False
			else:
				current_spool_id = spool_id

			# Get remaining weight from cached spool info
			remaining_weight = self.cached_spool_info.get('remaining_weight')
			if remaining_weight is None:
				self.error_body.append("Spool has no remaining weight data, skipping check", "warning")
				return False

			# Perform check
			required_with_margin = required_weight[tool_index] + self.weight_margin
			sufficient = remaining_weight >= required_with_margin

			filament = self.cached_spool_info.get('filament', {})
			filament_name = filament.get('name', 'Unknown')

			if sufficient:
				msg = (f"Weight Check PASSED: Spool {current_spool_id} ({filament_name}) "
							f"has {remaining_weight:.1f}g, need {required_weight[tool_index]:.1f}g "
							f"(+{self.weight_margin:.1f}g margin)")
				logging.info(msg)
			else:
				deficit = required_with_margin - remaining_weight
				msg = (f"Weight Check FAILED: Spool {current_spool_id} ({filament_name}) "
							f"has only {remaining_weight:.1f}g, need {required_weight[tool_index]:.1f}g "
							f"(+{self.weight_margin:.1f}g margin). SHORT BY {deficit:.1f}g!")
				self.error_body.append(msg)
				weight_ok = False
		return weight_ok

	async def check_filament_name_compliance(self, filename: str) -> bool:
		"""
		Check if active spool filament name matches metadata filament name

		Args:
			filename: Path to gcode file

		Returns:
			True if compliant or check not applicable, False if error severity and mismatch
		"""
		if not self.spoolman or not self.enable_filament_name_check or self.filament_name_mismatch_severity == "ignore":
			return True

		# Get active spool and initialize data
		spool_id = await self._init_spool()
		if spool_id is None and not self.multi_tool_mapping:
			await self._log_to_console("No active spool set or cannot fetch spool info, skipping filament name check", "info")
			return True

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			self.error_body.append(f"Metadata not available for {filename}")
			return False

		# Extract filament name from metadata (first name in list)
		metadata_filament_names = metadata.get('filament_name')
		if not metadata_filament_names:
			await self._log_to_console("No filament name data in file metadata, skipping check", "info")
			return True
		# if it is not a list, make it a list
		logging.info(f"Raw metadata filament names: {metadata_filament_names}")
		metadata_filament_names = json.loads(metadata_filament_names)
		if not isinstance(metadata_filament_names, list):
			metadata_filament_names = [metadata_filament_names]

		if self.multi_tool_mapping:
			tool_range = range(len(self.multi_tool_mapping))
		else:
			tool_range = range(1)  # Single tool T0

		check_passed = True

		for tool_index in tool_range:
			if self.multi_tool_mapping:
				current_spool_id = self.multi_tool_mapping[tool_index]
				self.cached_spool_info = await self._fetch_spool_info(current_spool_id)
				if self.cached_spool_info is None:
					self.error_body.append(f"Cannot fetch spool info for tool {tool_index} (spool ID {current_spool_id})")
					return False
			else:
				current_spool_id = spool_id

			# Get spool filament name
			filament = self.cached_spool_info.get('filament', {})
			spool_filament_name = filament.get('name', '').strip()

			if not spool_filament_name:
				await self._log_to_console(f"Spool {current_spool_id} has no filament name data, skipping check", "info")
				continue

			# Check compliance (case-insensitive)
			if tool_index >= len(metadata_filament_names):
				# Should not happen if lengths checked, but for safety:
				target_name = metadata_filament_names[0]
			else:
				target_name = metadata_filament_names[tool_index]

			compliant = spool_filament_name.lower() == target_name.lower()

			if compliant:
				msg = f"Filament Name Check PASSED: Spool {current_spool_id} name '{spool_filament_name}' matches"
				logging.info(msg)
			else:
				msg = (f"Filament Name Check FAILED: Spool {current_spool_id} "
							f"has name `{spool_filament_name}` but gcode expects `{target_name}`")
				if self.filament_name_mismatch_severity != 'error':
					await self._log_to_console(msg, self.filament_name_mismatch_severity)
				else :
					self.error_body.append(msg)
					check_passed = False

		return check_passed

	async def run_checks(self, tool_gate_map=None) -> None:
		"""
		Run all enabled pre-print checks on the current print file.
		Auto-detects MMU mode and runs appropriate checks.
		Pauses print if any check fails with error severity.
		Called automatically from Klipper macro without parameters.
		params:
			tools: List of tool indices to check default checks only T0
			gate_ids: List of gate IDs. This is mandatory if tools arg is used.
		"""
		logging.info("Starting Additional Pre-Print Checks...")

		logging.info(f"tool_gate_map: {tool_gate_map}")
		if tool_gate_map is not None:
			self.multi_tool_mapping = tool_gate_map

		if not self.spoolman:
			logging.warning("Spoolman component not available, skipping checks")
			await self._log_to_console("Pre-print checks skipped: Spoolman not available", "warning")
			return

		# Get current filename from Klipper
		filename = await self._get_current_filename()
		if not filename:
			logging.warning("No current filename available, skipping checks")
			await self._log_to_console("Pre-print checks skipped: No filename available", "warning")
			return

		# Check if MMU mode
		await self._is_hh_enabled() # only check once here and cache
		mode = "Multi-tool" if self.multi_tool_mapping else "Single-spool"
		logging.info(f"Running {mode} pre-print checks for file: {filename}")
		await self._log_to_console(f"Running {mode} checks for: {filename}", "info")

		# Clear cache at start of check session
		self._clear_spool_cache()

		if self.is_hh:
			await self._log_to_console("Pre-print checks skipped: Redundant with HH consistency checks", "warning")
			return
		try:
			# Single-spool mode: check active spool
			weight_ok = await self.check_print_weight(filename)
			# material_ok = await self.check_material_compliance(filename)
			filament_name_ok = await self.check_filament_name_compliance(filename)
			all_ok = weight_ok and filament_name_ok

			if all_ok:
				await self._log_to_console("✓ All pre-print checks PASSED", "info")
				if self.enable_weight_check:
					await self._log_to_console("   ✓ sufficient filament available", "info")
				if self.enable_material_check:
					await self._log_to_console("   ✓ material compliance check passed", "info")
				if self.enable_filament_name_check:
					await self._log_to_console("   ✓ filament name compliance check passed", "info")
			else:
				# Pause the print
				try:
					await self.klippy_apis.pause_print()
				except Exception as e:
					logging.error(f"Failed to pause print: {e}")
				await self._log_to_console(msg=(". ".join(self.error_body)), reason="Pre-Print Check Failed", severity="error")
		finally:
			# Clear cache after checks complete
			self._clear_spool_cache()
			self.error_body = []

def load_component(config: ConfigHelper) -> AdditionalPrePrintChecks:
	return AdditionalPrePrintChecks(config)
