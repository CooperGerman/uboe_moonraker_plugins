# Additional Pre-Print Checks Plugin
# Moonraker plugin to perform pre-print validation checks
#
# This plugin verifies sufficient filament weight is available before printing
# by comparing metadata weight requirements against Spoolman active spool data
#
from __future__ import annotations
import logging
import asyncio
from logging import config
from typing import TYPE_CHECKING, Dict, Any, Optional, List

if TYPE_CHECKING:
	from ..components.spoolman import SpoolManager
	from ..components.mmu_server import MmuServer
	from ..confighelper import ConfigHelper
	from ..components.klippy_apis import KlippyAPI as APIComp
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

		# Load components
		if config.has_section("spoolman"):
			self.spoolman = self.server.load_component(config, "spoolman", None)

		self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
		self.file_manager: FileManager = self.server.lookup_component("file_manager")
		self.metadata_storage: MetadataStorage = self.file_manager.get_metadata_storage()
		self.database: MoonrakerDatabase = self.server.lookup_component("database")

		# Configuration
		self.weight_margin = self.config.getfloat("weight_margin_grams", 5.0)
		self.enable_weight_check = self.config.getboolean("enable_weight_check", True)
		self.enable_material_check = self.config.getboolean("enable_material_check", True)
		self.enable_filament_name_check = self.config.getboolean("enable_filament_name_check", False)

		# Mismatch severity levels: 'error', 'warning', 'info', 'ignore'
		self.material_mismatch_severity = self.config.get("material_mismatch_severity", "warning")
		self.filament_name_mismatch_severity = self.config.get("filament_name_mismatch_severity", "info")

		# Cache for spool data during check
		self.cached_spool_info: Optional[Dict[str, Any]] = None
		self.cached_spool_id: Optional[int] = None

		# Init mmu_server component
		if config.has_section("mmu_server"):
			self.mmu_server = self.server.load_component(config, "mmu_server", None)

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

	def _is_mmu_enabled(self) -> bool:
		"""Check if MMU backend is present and enabled"""
		return self.mmu_server._mmu_backend_enabled()

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

	async def _log_to_console(self, msg: str, severity: str = "info") -> None:
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
			True if check passed or not applicable, False if failed
		"""
		if not self.spoolman or not self.enable_weight_check:
			return True

		# Get active spool and initialize data
		spool_id = await self._init_spool()
		if spool_id is None:
			await self._log_to_console("No active spool set or cannot fetch spool info, skipping weight check", "info")
			return True

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			logging.error(f"Metadata not available for {filename}")
			await self._log_to_console(f"No metadata available for {filename}", "error")
			return True

		# Extract required weight from metadata
		required_weight = metadata.get('filament_weight_total')
		if required_weight is None:
			await self._log_to_console("No weight data in file metadata, skipping check", "info")
			return True

		# Get remaining weight from cached spool info
		remaining_weight = self.cached_spool_info.get('remaining_weight')
		if remaining_weight is None:
			await self._log_to_console("Spool has no remaining weight data, skipping check", "info")
			return True

		# Perform check
		required_with_margin = required_weight + self.weight_margin
		sufficient = remaining_weight >= required_with_margin

		filament = self.cached_spool_info.get('filament', {})
		filament_name = filament.get('name', 'Unknown')

		if sufficient:
			msg = (f"Weight Check PASSED: Spool {spool_id} ({filament_name}) "
						f"has {remaining_weight:.1f}g, need {required_weight:.1f}g "
						f"(+{self.weight_margin:.1f}g margin)")
			await self._log_to_console(msg, "info")
			return True
		else:
			deficit = required_with_margin - remaining_weight
			msg = (f"Weight Check FAILED: Spool {spool_id} ({filament_name}) "
						f"has only {remaining_weight:.1f}g, need {required_weight:.1f}g "
						f"(+{self.weight_margin:.1f}g margin). SHORT BY {deficit:.1f}g!")
			await self._log_to_console(msg, "error")
			return False

	async def check_mmu_tools(self, filename: str) -> bool:
		"""
		Check all tools used in MMU print against their assigned spools

		Args:
			filename: Path to gcode file

		Returns:
			True if all checks passed, False if any critical check failed
		"""
		if not self.spoolman or not self.mmu_server:
			return True

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			logging.error(f"Metadata not available for {filename}")
			await self._log_to_console(f"No metadata available for {filename}", "error")
			return True

		# Get referenced tools from metadata
		referenced_tools = metadata.get('referenced_tools')
		if not referenced_tools:
			await self._log_to_console("No referenced tools in metadata, skipping MMU checks", "info")
			return True

		# Extract per-tool arrays from metadata
		filament_weights = metadata.get('filament_weights', [])
		filament_type_str = metadata.get('filament_type', '')
		filament_types = filament_type_str.split(';') if filament_type_str else []
		filament_name_str = metadata.get('filament_name', '')
		filament_names = filament_name_str.split(';') if filament_name_str else []

		await self._log_to_console(f"Checking {len(referenced_tools)} tools: {referenced_tools}", "info")

		# Refresh mmu_server cache to get current gate map
		await self.mmu_server.refresh_cache(silent=True)
		printer_hostname = self.mmu_server.printer_hostname

		all_checks_passed = True

		# Check each referenced tool
		for tool_idx in referenced_tools:
			# Find spool assigned to this gate (tool_idx == gate_number in Happy Hare)
			spool_id = self.mmu_server._find_first_spool_id(printer_hostname, tool_idx)

			if spool_id < 0:
				if self.enable_weight_check:
					msg = f"Tool {tool_idx}: No spool assigned to gate {tool_idx}"
					await self._log_to_console(msg, "error")
					all_checks_passed = False
				else:
					msg = f"Tool {tool_idx}: No spool assigned (checks disabled)"
					await self._log_to_console(msg, "warning")
				continue

			# Fetch spool info
			spool_info = await self.mmu_server._fetch_spool_info(spool_id)
			if not spool_info:
				msg = f"Tool {tool_idx}: Failed to fetch info for spool {spool_id}"
				await self._log_to_console(msg, "error")
				all_checks_passed = False
				continue

			filament = spool_info.get('filament', {})
			spool_name = filament.get('name', 'Unknown')
			spool_material = filament.get('material', '').strip()
			remaining_weight = spool_info.get('remaining_weight')

			# Weight check
			if self.enable_weight_check and tool_idx < len(filament_weights):
				required_weight = filament_weights[tool_idx]
				if required_weight and remaining_weight is not None:
					required_with_margin = required_weight + self.weight_margin
					if remaining_weight >= required_with_margin:
						msg = (f"Tool {tool_idx} [{spool_name}]: Weight OK "
								 f"({remaining_weight:.1f}g >= {required_weight:.1f}g + {self.weight_margin:.1f}g)")
						await self._log_to_console(msg, "info")
					else:
						deficit = required_with_margin - remaining_weight
						msg = (f"Tool {tool_idx} [{spool_name}]: INSUFFICIENT WEIGHT - "
								 f"has {remaining_weight:.1f}g, need {required_weight:.1f}g "
								 f"(+{self.weight_margin:.1f}g margin). SHORT BY {deficit:.1f}g!")
						await self._log_to_console(msg, "error")
						all_checks_passed = False

			# Material check
			if self.enable_material_check and tool_idx < len(filament_types):
				expected_material = filament_types[tool_idx].strip()
				if expected_material and spool_material:
					if spool_material.lower() == expected_material.lower():
						msg = f"Tool {tool_idx} [{spool_name}]: Material OK ({spool_material})"
						await self._log_to_console(msg, "info")
					else:
						msg = (f"Tool {tool_idx} [{spool_name}]: Material mismatch - "
								 f"has '{spool_material}' but expects '{expected_material}'")
						await self._log_to_console(msg, self.material_mismatch_severity)
						if self.material_mismatch_severity == "error":
							all_checks_passed = False

			# Filament name check
			if self.enable_filament_name_check and tool_idx < len(filament_names):
				expected_name = filament_names[tool_idx].strip()
				if expected_name and spool_name:
					if spool_name.lower() == expected_name.lower():
						msg = f"Tool {tool_idx}: Name OK ({spool_name})"
						await self._log_to_console(msg, "info")
					else:
						msg = (f"Tool {tool_idx}: Name mismatch - "
								 f"has '{spool_name}' but expects '{expected_name}'")
						await self._log_to_console(msg, self.filament_name_mismatch_severity)
						if self.filament_name_mismatch_severity == "error":
							all_checks_passed = False

		return all_checks_passed

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
		if spool_id is None:
			await self._log_to_console("No active spool set or cannot fetch spool info, skipping filament name check", "info")
			return True

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			logging.error(f"Metadata not available for {filename}")
			await self._log_to_console(f"No metadata available for {filename}", "error")
			return True

		# Extract filament name from metadata (first name in list)
		metadata_filament_names = metadata.get('filament_name')
		if not metadata_filament_names:
			await self._log_to_console("No filament name data in file metadata, skipping check", "info")
			return True

		# if return value is a list and has at least one entry
		if isinstance(metadata_filament_names, list):
			metadata_filament_name = metadata_filament_names[0].strip()
		else:
			metadata_filament_name = metadata_filament_names.strip()

		# Get spool filament name
		filament = self.cached_spool_info.get('filament', {})
		spool_filament_name = filament.get('name', '').strip()

		if not spool_filament_name:
			await self._log_to_console("Spool has no filament name data, skipping check", "info")
			return True

		# Check compliance (case-insensitive)
		compliant = spool_filament_name.lower() == metadata_filament_name.lower()

		if compliant:
			msg = f"Filament Name Check PASSED: Spool {spool_id} name '{spool_filament_name}' matches"
			await self._log_to_console(msg, "info")
			return True
		else:
			msg = (f"Filament Name Check FAILED: Spool {spool_id} "
						f"has name '{spool_filament_name}' but gcode expects '{metadata_filament_name}'")
			await self._log_to_console(msg, self.filament_name_mismatch_severity)
			return self.filament_name_mismatch_severity != "error"

	async def run_checks(self) -> None:
		"""
		Run all enabled pre-print checks on the current print file.
		Auto-detects MMU mode and runs appropriate checks.
		Pauses print if any check fails with error severity.
		Called automatically from Klipper macro without parameters.
		"""
		logging.info("Starting Additional Pre-Print Checks...")
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
		is_mmu = self._is_mmu_enabled()
		mode = "MMU multi-tool" if is_mmu else "Single-spool"
		logging.info(f"Running {mode} pre-print checks for file: {filename}")
		await self._log_to_console(f"Running {mode} checks for: {filename}", "info")

		# Clear cache at start of check session
		self._clear_spool_cache()

		try:
			if is_mmu:
				# MMU mode: check all referenced tools
				all_ok = await self.check_mmu_tools(filename)
			else:
				# Single-spool mode: check active spool
				weight_ok = await self.check_print_weight(filename)
				filament_name_ok = await self.check_filament_name_compliance(filename)
				all_ok = weight_ok and filament_name_ok

			if all_ok:
				await self._log_to_console("✓ All pre-print checks PASSED", "info")
			else:
				await self._log_to_console("✗ Pre-print checks FAILED - Print paused", "error")
				# Pause the print
				try:
					await self.klippy_apis.pause_print()
				except Exception as e:
					logging.error(f"Failed to pause print: {e}")
		finally:
			# Clear cache after checks complete
			self._clear_spool_cache()


def load_component(config: ConfigHelper) -> AdditionalPrePrintChecks:
	return AdditionalPrePrintChecks(config)
