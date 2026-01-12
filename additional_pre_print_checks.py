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
	from ..components.klippy_apis import KlippyAPI as APIComp
	from ..components.file_manager.file_manager import FileManager
	from ..components.file_manager.metadata import MetadataStorage


class AdditionalPrePrintChecks:
	def __init__(self, config: ConfigHelper):
		self.config = config
		self.server = config.get_server()
		self.spoolman: Optional[SpoolManager] = None

		# Load components
		if config.has_section("spoolman"):
			self.spoolman = self.server.load_component(config, "spoolman", None)

		self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
		self.file_manager: FileManager = self.server.lookup_component("file_manager")
		self.metadata_storage: MetadataStorage = self.file_manager.get_metadata_storage()

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
			logging.info("Additional Pre-Print Checks component initialized")

	async def _init_spool_data(self, spool_id: int) -> bool:
		"""
		Initialize and cache spool data for current check session

		Args:
			spool_id: Spool ID to fetch and cache

		Returns:
			True if spool data successfully cached, False otherwise
		"""
		# Check if already cached
		if self.cached_spool_id == spool_id and self.cached_spool_info is not None:
			return True

		# Fetch spool info
		self.cached_spool_info = await self._fetch_spool_info(spool_id)
		if self.cached_spool_info is None:
			self.cached_spool_id = None
			return False

		self.cached_spool_id = spool_id
		return True

	def _clear_spool_cache(self) -> None:
		"""Clear cached spool data"""
		self.cached_spool_info = None
		self.cached_spool_id = None

	async def _get_active_spool_id(self) -> Optional[int]:
		"""Get currently active spool ID from Spoolman"""
		if not self.spoolman:
			return None
		try:
			return await self.spoolman.get_active_spool()
		except Exception as e:
			logging.error(f"Failed to get active spool ID: {e}")
			return None

	async def _fetch_spool_info(self, spool_id: int) -> Optional[Dict[str, Any]]:
		"""Retrieve spool information from Spoolman"""
		try:
			spool_info = await self.spoolman.get_spool(spool_id)
			return spool_info
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

		# Get active spool ID
		spool_id = await self._get_active_spool_id()
		if spool_id is None:
			await self._log_to_console("No active spool set, skipping weight check", "info")
			return True

		# Initialize spool data
		if not await self._init_spool_data(spool_id):
			await self._log_to_console(f"Cannot fetch spool {spool_id} info", "error")
			return True  # Don't block on fetch errors

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

	async def check_material_compliance(self, filename: str) -> bool:
		"""
		Check if active spool material matches metadata material

		Args:
			filename: Path to gcode file

		Returns:
			True if compliant or check not applicable, False if error severity and mismatch
		"""
		if not self.spoolman or not self.enable_material_check or self.material_mismatch_severity == "ignore":
			return True

		# Get active spool ID
		spool_id = await self._get_active_spool_id()
		if spool_id is None:
			await self._log_to_console("No active spool set, skipping material check", "info")
			return True

		# Initialize spool data
		if not await self._init_spool_data(spool_id):
			await self._log_to_console(f"Cannot fetch spool {spool_id} info", "error")
			return True

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			logging.error(f"Metadata not available for {filename}")
			await self._log_to_console(f"No metadata available for {filename}", "error")
			return True

		# Extract material from metadata (first material in list)
		metadata_materials = metadata.get('filament_type')
		if not metadata_materials or not isinstance(metadata_materials, list) or len(metadata_materials) == 0:
			await self._log_to_console("No material data in file metadata, skipping check", "info")
			return True

		metadata_material = metadata_materials[0].upper().strip()

		# Get spool material
		filament = self.cached_spool_info.get('filament', {})
		spool_material = filament.get('material', '').upper().strip()
		filament_name = filament.get('name', 'Unknown')

		if not spool_material:
			await self._log_to_console("Spool has no material data, skipping check", "info")
			return True

		# Check compliance
		compliant = spool_material == metadata_material

		if compliant:
			msg = f"Material Check PASSED: Spool {spool_id} ({filament_name}) material '{spool_material}' matches"
			await self._log_to_console(msg, "info")
			return True
		else:
			msg = (f"Material Check FAILED: Spool {spool_id} ({filament_name}) "
						f"has material '{spool_material}' but gcode expects '{metadata_material}'")
			await self._log_to_console(msg, self.material_mismatch_severity)
			return self.material_mismatch_severity != "error"

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

		# Get active spool ID
		spool_id = await self._get_active_spool_id()
		if spool_id is None:
			await self._log_to_console("No active spool set, skipping filament name check", "info")
			return True

		# Initialize spool data
		if not await self._init_spool_data(spool_id):
			await self._log_to_console(f"Cannot fetch spool {spool_id} info", "error")
			return True

		# Get file metadata
		metadata = self.metadata_storage.get(filename)
		if metadata is None:
			logging.error(f"Metadata not available for {filename}")
			await self._log_to_console(f"No metadata available for {filename}", "error")
			return True

		# Extract filament name from metadata (first name in list)
		metadata_filament_names = metadata.get('filament_name')
		if not metadata_filament_names or not isinstance(metadata_filament_names, list) or len(metadata_filament_names) == 0:
			await self._log_to_console("No filament name data in file metadata, skipping check", "info")
			return True

		metadata_filament_name = metadata_filament_names[0].strip()

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

			logging.info(f"Running pre-print checks for file: {filename}")
			await self._log_to_console(f"Running pre-print checks for: {filename}", "info")

			# Clear cache at start of check session
			self._clear_spool_cache()

			try:
				# Run all checks
				weight_ok = await self.check_print_weight(filename)
				material_ok = await self.check_material_compliance(filename)
				filament_name_ok = await self.check_filament_name_compliance(filename)

				all_ok = weight_ok and material_ok and filament_name_ok

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
