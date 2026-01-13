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
		if self.mmu_server is None:
			return False
		return self.mmu_server._init_mmu_backend()

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
			logging.info(msg)
			return True
		else:
			deficit = required_with_margin - remaining_weight
			msg = (f"Weight Check FAILED: Spool {spool_id} ({filament_name}) "
						f"has only {remaining_weight:.1f}g, need {required_weight:.1f}g "
						f"(+{self.weight_margin:.1f}g margin). SHORT BY {deficit:.1f}g!")
			await self._log_to_console(msg, "error")
			return False

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
			logging.info(msg)
			return True
		else:
			msg = (f"Filament Name Check FAILED: Spool {spool_id} "
						f"has name '{spool_filament_name}' but gcode expects '{metadata_filament_name}'")
			await self._log_to_console(msg, self.filament_name_mismatch_severity)
			return self.filament_name_mismatch_severity != "error"

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

		# Extract per-tool arrays from metadata
		filament_weights = metadata.get('filament_weights', [])
		filament_types = metadata.get('filament_type', [])
		filament_types = [filament_types] if isinstance(filament_types, str) else filament_types
		filament_names = metadata.get('filament_name', [])
		filament_names = [filament_names] if isinstance(filament_names, str) else filament_names
		logging.info(f"MMU Print Metadata - Weights: {filament_weights}, Types: {filament_types}, Names: {filament_names}")

		# Refresh mmu_server cache to get current gate map
		await self.mmu_server.refresh_cache(silent=True)

		# get tool to gate map from mmu backend config (example result : [2, 1, 2, 3, 4, 5, 6, 7, 8])
		# tool (index 0) mapped to gate 2, tool (index 1) mapped to gate 1, etc.
		ttg_map = self.mmu_server.mmu_backend_config.get('mmu', {}).get('ttg_map', False)
		gate_spool_id = self.mmu_server.mmu_backend_config.get('mmu', {}).get('gate_spool_id', False)
		endless_spool_groups = self.mmu_server.mmu_backend_config.get('mmu', {}).get('endless_spool_groups', False)

		logging.info(f"Tool to Gate Map: {ttg_map}")
		logging.info(f"Gate to Spool Map: {gate_spool_id}")
		logging.info(f"Endless Spool Groups: {endless_spool_groups}")

		all_ok = True
		# go through each filament used in the print and check the assigned gate and thus
		# the spool on which to check the name and weight
		for tool_index in range(len(filament_weights)):
			# Get assigned gate for tool
			if tool_index >= len(ttg_map):
				logging.error(f"Tool index {tool_index} out of range in tool-to-gate map")
				continue
			gate_number = ttg_map[tool_index]
			# check if any other gate belongs to the current gate's group
			group = [g for g, sg in enumerate(endless_spool_groups) if sg == endless_spool_groups[gate_number]]

			# Get assigned spool for gate
			spool_id = gate_spool_id[gate_number] if gate_number < len(gate_spool_id) else None
			if spool_id is None:
				await self._log_to_console(f"No spool assigned to gate {gate_number} for tool {tool_index}, skipping checks", "warning")
				continue

			# Fetch and cache spool info
			self.cached_spool_info = await self._fetch_spool_info(spool_id)
			if self.cached_spool_info is None:
				await self._log_to_console(f"Cannot fetch spool {spool_id} info for tool {tool_index}, skipping checks", "error")
				all_ok = False
				continue

			# Check weight
			required_weight = filament_weights[tool_index]
			weight_ok = True
			if self.enable_weight_check and required_weight is not None:
				# if more than current spool in group, sum up their weights
				if len(group) > 1:
					remaining_weight = 0.0
					for g in group:
						spool_id_g = gate_spool_id[g] if g < len(gate_spool_id) else None
						if spool_id_g is None:
							continue
						spool_info = await self._fetch_spool_info(spool_id_g)
						if spool_info is None:
							continue
						rw = spool_info.get('remaining_weight')
						if rw is not None:
							remaining_weight += rw
					self.cached_spool_info['remaining_weight'] = remaining_weight
				else:
					remaining_weight = self.cached_spool_info.get('remaining_weight')

				required_with_margin = required_weight + self.weight_margin
				weight_ok = remaining_weight >= required_with_margin

				filament = self.cached_spool_info.get('filament', {})
				filament_name = filament.get('name', 'Unknown')

				if weight_ok:
					msg = (f"Tool {tool_index} Weight Check PASSED: Spool {spool_id} ({filament_name}) "
								f"has {remaining_weight:.1f}g, need {required_weight:.1f}g "
								f"(+{self.weight_margin:.1f}g margin)")
					logging.info(msg)
				else:
					deficit = required_with_margin - remaining_weight
					msg = (f"Tool {tool_index} Weight Check FAILED: Spool {spool_id} ({filament_name}) "
								f"has only {remaining_weight:.1f}g, need {required_weight:.1f}g "
								f"(+{self.weight_margin:.1f}g margin). SHORT BY {deficit:.1f}g!")
					await self._log_to_console(msg, "error")
					weight_ok = False
			# Check material
			material_ok = True
			if self.enable_material_check and tool_index < len(filament_types):
				metadata_material = filament_types[tool_index].strip()
				if len(group) > 1:
					spool_materials = set()
					for g in group:
						spool_id_g = gate_spool_id[g] if g < len(gate_spool_id) else None
						if spool_id_g is None:
							continue
						spool_info = await self._fetch_spool_info(spool_id_g)
						if spool_info is None:
							continue
						sm = spool_info.get('filament', {}).get('material', '').strip()
						if sm:
							spool_materials.add(sm.lower())
					if len(spool_materials) == 1:
						spool_material = spool_materials.pop()
					else:
						spool_material = None  # Ambiguous materials in group
				else:
					spool_material = self.cached_spool_info.get('filament', {}).get('material', '').strip()

				if not spool_material:
					await self._log_to_console(f"Tool {tool_index} Spool {spool_id} has no material data, skipping material check", "info")
				else:
					material_ok = spool_material.lower() == metadata_material.lower()
					if material_ok:
						msg = (f"Tool {tool_index} Material Check PASSED: Spool {spool_id} material '{spool_material}' matches")
						logging.info(msg)
					else:
						msg = (f"Tool {tool_index} Material Check FAILED: Spool {spool_id} "
									f"has material '{spool_material}' but gcode expects '{metadata_material}'")
						await self._log_to_console(msg, self.material_mismatch_severity)
						if self.material_mismatch_severity == "error":
							material_ok = False
			# Check filament name
			filament_name_ok = True
			if self.enable_filament_name_check and tool_index < len(filament_names):
				metadata_filament_name = filament_names[tool_index].strip()
				spool_filament_name = self.cached_spool_info.get('filament', {}).get('name', '').strip()

				if not spool_filament_name:
					await self._log_to_console(f"Tool {tool_index} Spool {spool_id} has no filament name data, skipping name check", "info")
				else:
					filament_name_ok = spool_filament_name.lower() == metadata_filament_name.lower()
					if filament_name_ok:
						msg = (f"Tool {tool_index} Filament Name Check PASSED: Spool {spool_id} name '{spool_filament_name}' matches")
						logging.info(msg)
					else:
						msg = (f"Tool {tool_index} Filament Name Check FAILED: Spool {spool_id} "
									f"has name '{spool_filament_name}' but gcode expects '{metadata_filament_name}'")
						await self._log_to_console(msg, self.filament_name_mismatch_severity)
						if self.filament_name_mismatch_severity == "error":
							filament_name_ok = False
			if not (weight_ok and material_ok and filament_name_ok):
				all_ok = False
		return all_ok

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
