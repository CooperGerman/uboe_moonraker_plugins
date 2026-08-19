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
import os
from typing import TYPE_CHECKING, Dict, Any, Optional, List
from packaging.version import Version
from datetime import datetime, time, timedelta

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

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"

class RunoutEstimate:
	def __init__(
		self,
		runout_index: int,
		target_volume_mm3: float,
		estimated_minutes_remaining: float,
		estimated_minutes_from_now: float,
		estimated_progress_percent: float,
		estimated_layer: int,
		tool_number: int
	):
		now = datetime.now()
		self.runout_index = runout_index
		self.target_volume_mm3 = target_volume_mm3
		self.estimated_minutes_remaining = estimated_minutes_remaining
		self.estimated_minutes_from_now = estimated_minutes_from_now
		self.estimated_progress_percent = estimated_progress_percent
		self.estimated_layer = estimated_layer
		self.tool_number = tool_number
		self.eta = now + timedelta(minutes=self.estimated_minutes_from_now)

	def __str__(self):
		return ("\t\n".join([
			f"Runout {self.runout_index}: at cumulative {self.target_volume_mm3:.2f} mm³, ",
			f"in {self.estimated_minutes_from_now:.2f} min ({str(timedelta(minutes=self.estimated_minutes_from_now))[:-3]}), ",
			f"ETA {self.eta.strftime('%Y-%m-%d %H:%M:%S')}, ",
			f"M73 P={self.estimated_progress_percent:.1f}%, R={self.estimated_minutes_remaining:.2f} min, ",
			f"Layer={self.estimated_layer}, Tool={self.tool_number}"
		])
		)

class ExtractedMetadata:
	'''
	Class to represent extracted metadata from gcode file
	with a little bit of formatting and helper functions for
	additional checks.
	'''
	def __init__(self, raw_metadata: MetadataStorage, filename, error_body):
		self.error_body = error_body
		metadata = raw_metadata.get(filename)
		if metadata is None:
			self.error_body.append(f"Metadata not available for {filename}")
			return None
		self.raw_metadata = metadata
		self.filament_weights = self._parse_filament_weights()
		self.filament_names = self._parse_filament_names()
		self.referenced_tools = self._parse_referenced_tools()
		self.extrusion_sample_points = self._parse_extrusion_sample_points()
		# logging.info(f"Extrusion sample points: {self.extrusion_sample_points.to_dict()}")

	def _parse_extrusion_sample_points(self) -> Optional[ExtrusionPoints]:
		points = self.raw_metadata.get('extrusion_sample_points')
		if points is None:
			self.error_body.append("No extrusion sample points in file metadata, skipping spool change estimation")
			return None
		if not isinstance(points, list):
			points = [points]
		return ExtrusionPoints(dict_init=points)

	def _parse_filament_weights(self) -> Optional[List[float]]:
		weights = self.raw_metadata.get('filament_weights')
		if weights is None:
			self.error_body.append("No filament weight requirements in file metadata, skipping weight check")
			return None
		if not isinstance(weights, list):
			weights = [weights]
		return [float(w) for w in weights]

	def _parse_filament_names(self) -> Optional[List[str]]:
		names = self.raw_metadata.get('filament_name')
		if names is None:
			self.error_body.append("No filament name data in file metadata, skipping filament name check")
			return None
		if not isinstance(names, list):
			# if it is not a list, make it a list
			logging.info(f"Raw metadata filament names: {names}")
			try:
				names = eval(names) # convert if it is a stringified list
			except:
				pass
			if not isinstance(names, list):
				names = [names]
			if not isinstance(names, list):
				names = json.loads(names)
		return names

	def _parse_referenced_tools(self) -> Optional[List[int]]:
		tools = self.raw_metadata.get('referenced_tools')
		if tools is None:
			self.error_body.append("No referenced tool data in file metadata, warnings and errors might be inaccurate")
			return None
		if not isinstance(tools, list):
			tools = [tools]
		return [int(t) for t in tools]

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
		self.extracted_metadata : ExtractedMetadata = None
		self.database: MoonrakerDatabase = self.server.lookup_component("database")
		self.klippy_connection: KlippyConnection = self.server.lookup_component("klippy_connection")
		# Configuration
		self.enable_weight_check = self.config.getboolean("enable_weight_check", True)
		self.weight_margin = self.config.getfloat("weight_margin_grams", 5.0)

		self.enable_material_check = self.config.getboolean("enable_material_check", True)
		self.enable_filament_name_check = self.config.getboolean("enable_filament_name_check", False)

		self.enable_spool_change_on_work_hours_check = self.config.getboolean("enable_spool_change_on_work_hours_check", True)
		self.spool_change_outside_work_hours_severity = self.config.get("spool_change_outside_work_hours_severity", "error")
		if self.enable_spool_change_on_work_hours_check:
			self.work_hours_start = self.config.get("work_hours_start")
			self.work_hours_end = self.config.get("work_hours_end")
			# if leading or trailing zeros are missing, add them
			self.work_hours_start = time.fromisoformat(self.work_hours_start.zfill(5))
			self.work_hours_end = time.fromisoformat(self.work_hours_end.zfill(5))


		self.multi_tool_mapping =  False
		self.filename = None

		# Mismatch severity levels: 'error', 'warning', 'info'
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
			self.enabled = False
		else:
			logging.info("Additional Pre-Print Checks: Disabled (spoolman not available)")
			self.enabled = False

	async def component_init(self) -> None:
		"""Initialize component"""
		if self.spoolman:
			await self._init_spool()
		try:
			self.uboe_metadata = self.server.lookup_component("uboe_metadata")
		except Exception as e:
			raise self.config.error(f"[{self.config.get_name()}]: {e}")
		logging.info("Additional Pre-Print Checks component initialized")

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
			self.filename = filename if filename else None
		except Exception as e:
			logging.error(f"Failed to get current filename: {e}")
			self.filename = None

	async def _log_to_console(self, msg: str, severity: str = "info", reason: str = "Pre-Print Check Failed", force_error_dialog=False) -> None:
		"""
		Send message to Klipper console with appropriate severity
		(heavily inspired by ratos see console_echo in ratos.py)
		Args:
			msg: Message to log
			severity: 'error', 'warning', or 'info'
		"""
		color = "white"
		opacity = 1.0
		if severity == 'info': color = "#38bdf8"
		if severity == 'success': color = "#a3e635"
		if severity == 'warning': color = "#fbbf24"
		if severity == 'alert': color = "#f87171"
		if severity == 'error': color = "#f87171"
		if severity == 'debug': color = "#38bdf8"
		if severity == 'debug': opacity = 0.7

		msg = msg.replace("_N_","\n")

		if (severity == 'error' or severity == 'alert'):
			logging.error(reason + ": " + msg)
		if (severity == 'warning'):
			logging.warning(reason + ": " + msg)
		if (severity == 'info'):
			logging.info(reason + ": " + msg)
		if (severity == 'debug'):
			logging.debug(reason + ": " + msg)

		_title = '<p style="font-weight: bold; margin:0; opacity:' + str(opacity) + '; color:' + color + '">' + reason + '</p>'
		if msg:
			_msg = '<p style="margin:0; opacity:' + str(opacity) + '; color:' + color + '">' + msg + '</p>'
		else:
			_msg = ''

		try:
			if self.is_hh:
				error_flag = "ERROR=1" if severity == "error" else ""
				msg = msg.replace("\n", "\\n") # Get through klipper filtering
				await self.klippy_apis.run_gcode(f"MMU_LOG MSG='{msg}' {error_flag}")
			else:
				msg = msg.replace("\n", "\\n")
				if severity == "error" or force_error_dialog:
					await self.klippy_apis.run_gcode('_UBOE_ERROR_DIALOG MSG="%s" REASON="%s"' % (msg, reason))
				else :
					await self.klippy_apis.run_gcode(f"M118 <div>{_title}{_msg}</div>")
		except Exception as e:
			logging.error(f"Failed to send message to console: {e}")

	def estimate_runouts(self,
		current_remaining_g: float,
  		density: float,
		spool_size_g: float,
		start_volume: float | None = None,
		extr_id: int = 0,
	) -> list[RunoutEstimate]:
		"""Estimate all spool runouts that can be inferred from parsed points.

		If start_volume is provided, calculations
		start from that point (for mid-print progress). The remaining filament
		budget is therefore counted from the current volume, then each subsequent
		runout is spaced by spool_size_g for the same tool.
		Otherwise, starts from the first parsed point.
		"""

		def closest_point(target_volume_mm3: float) -> int | None:
			"""Find the layer value at the closest point to the target cumulative extruded volume."""
			if not self.extracted_metadata.extrusion_sample_points:
				return None
			closest_point = min(self.extracted_metadata.extrusion_sample_points, key=lambda p: abs(p.extruded_volume_mm3 - target_volume_mm3))
			return closest_point

		def get_max_volume_for_tool(tool_number: int) -> float:
			"""Get the maximum cumulative extruded volume for a given tool number."""
			for point in reversed(self.extracted_metadata.extrusion_sample_points):
				if point.extr_id == tool_number:
					return point.extruded_volume_mm3
			return 0.0

		if len(self.extracted_metadata.extrusion_sample_points) < 2:
			return []

		if start_volume is None:
			start_volume = self.extracted_metadata.extrusion_sample_points[0].extruded_volume_mm3

		max_volume = get_max_volume_for_tool(extr_id)

		runouts: list[RunoutEstimate] = []
		runout_idx = 1

		target_volume = start_volume + current_remaining_g / (density / 1000) # Convert grams to mm³ using filament density
		logging.info(f"Estimating runouts starting from volume {start_volume:.2f} mm³, current remaining {current_remaining_g:.2f}g, target volume {target_volume:.2f} mm³, max volume {max_volume:.2f} mm³")
		while target_volume <= max_volume:
			closest = closest_point(target_volume)
			if closest is None:
				break
			layer = closest.layer

			eta_remaining, eta_progress = closest.minutes_remaining, closest.progress_percent
			runouts.append(
				RunoutEstimate(
					runout_index=runout_idx,
					target_volume_mm3=target_volume,
					estimated_minutes_remaining=eta_remaining,
					estimated_minutes_from_now=eta_remaining,
					estimated_progress_percent=eta_progress,
					estimated_layer=layer,
					tool_number=extr_id
				)
			)
			runout_idx += 1
			target_volume += spool_size_g / (density / 1000) # Assuming single filament density for simplicity

		return runouts

	def _get_oob_runouts(self, runouts: list[RunoutEstimate]) -> bool:
		"""
		Check if a given timestamp is within the configured work hours.

		Args:
			runouts: List of RunoutEstimate objects

		Returns:
			True if within work hours, False otherwise
		"""
		from datetime import datetime, time
		incriminated = []
		try:
			if not runouts:
				return incriminated

			# if runouts do not appear within working hours return a list of incriminated runouts
			for runout in runouts:
				# timestamps are of from '2026-08-19 16:10:17.979191'
				timestamp = runout.eta.time()
				# Now keep only HH:MM for comparison
				timestamp = timestamp.replace(second=0, microsecond=0)

				logging.info(f"Checking runout ETA {timestamp} against work hours {self.work_hours_start} - {self.work_hours_end}")
				# if start < end, it's a normal range (e.g., 08:00 to 20:00)
				if self.work_hours_start <= self.work_hours_end:
					if self.work_hours_start <= timestamp <= self.work_hours_end:
						continue
				else:  # if start > end, it means the range wraps around midnight (e.g., 20:00 to 08:00)
					if timestamp >= self.work_hours_start or timestamp <= self.work_hours_end:
						continue
				incriminated.append(runout)
			return incriminated

		except Exception as e:
			logging.error(f"Failed to parse timestamp '{timestamp}': {e}")
			return incriminated

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

		if self.extracted_metadata.filament_weights is None:
			self.error_body.append("No filament weight requirements in file metadata, skipping weight check")
			return False

		if self.multi_tool_mapping:
			tool_range = range(len(self.multi_tool_mapping))
		else:
			if self.extracted_metadata.referenced_tools is None:
				self.error_body.append("A referenced tool index is required in file metadata for (single-spool) weight check, but not found. Skipping weight check.")
				return False
			elif len(self.extracted_metadata.referenced_tools) == 0:
				self.error_body.append("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) weight check. Skipping weight check.")
				return False
			elif len(self.extracted_metadata.referenced_tools) > 1:
				self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool weight check. Skipping weight check.")
				return False
			tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

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
				self.error_body.append("Spool has no remaining weight data, skipping check")
				return False

			required_weight = self.extracted_metadata.filament_weights[tool_index]
			# Perform check
			required_with_margin = required_weight + self.weight_margin
			sufficient = remaining_weight >= required_with_margin

			filament = self.cached_spool_info.get('filament', {})
			filament_name = filament.get('name', 'Unknown')

			if sufficient:
				msg = (f"Weight Check PASSED: Spool {current_spool_id} ({filament_name}) "
							f"has {remaining_weight:.1f}g, need {required_weight:.1f}g "
							f"(+{self.weight_margin:.1f}g margin)")
				logging.info(msg)
			else:
				deficit = required_with_margin - remaining_weight
				msg = (f"Weight Check FAILED: Spool {current_spool_id} ({filament_name}) "
							f"has only {remaining_weight:.1f}g, need {required_weight:.1f}g "
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
		if not self.spoolman or not self.enable_filament_name_check:
			return True

		# Get active spool and initialize data
		spool_id = await self._init_spool()
		if spool_id is None and not self.multi_tool_mapping:
			await self._log_to_console("No active spool set or cannot fetch spool info, skipping filament name check", "info")
			return True

		if self.extracted_metadata.filament_names is None:
			self.error_body.append("No filament weight requirements in file metadata, skipping weight check")
			return False

		if self.multi_tool_mapping:
			tool_range = range(len(self.multi_tool_mapping))
		else:
			if self.extracted_metadata.referenced_tools is None:
				self.error_body.append("A referenced tool index is required in file metadata for (single-spool) weight check, but not found. Skipping weight check.", "error")
				return False
			elif len(self.extracted_metadata.referenced_tools) == 0:
				self.error_body.append("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) weight check. Skipping weight check.", "warning")
				return False
			elif len(self.extracted_metadata.referenced_tools) > 1:
				self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool weight check. Skipping weight check.", "error")
				return False
			tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

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
			if tool_index >= len(self.extracted_metadata.filament_names):
				# Should not happen if lengths checked, but for safety:
				target_name = self.extracted_metadata.filament_names[0]
			else:
				target_name = self.extracted_metadata.filament_names[tool_index]

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

	async def check_spool_change_work_hours(self, filename: str) -> bool:
		"""
		Check if spool changes are within allowed work hours based on metadata and current time.

		Args:
			filename: Path to gcode file

		Returns:
			True if spool change is within allowed work hours, False otherwise.
		"""
		if not self.spoolman or not self.enable_spool_change_on_work_hours_check:
			return True

		# Get active spool and initialize data
		spool_id = await self._init_spool()
		if spool_id is None and not self.multi_tool_mapping:
			await self._log_to_console("No active spool set or cannot fetch spool info, skipping filament name check", "info")
			return True

		if self.extracted_metadata.extrusion_sample_points is None:
			self.error_body.append("No extrusion sample points in file metadata, skipping spool change estimation")
			return False

		if self.multi_tool_mapping:
			tool_range = range(len(self.multi_tool_mapping))
		else:
			if self.extracted_metadata.referenced_tools is None:
				self.error_body.append("A referenced tool index is required in file metadata for (single-spool) spool runout check, but not found. Skipping spool change estimation.", "error")
				return False
			elif len(self.extracted_metadata.referenced_tools) == 0:
				self.error_body.append("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) spool runout check. Skipping spool change estimation.", "warning")
				return False
			elif len(self.extracted_metadata.referenced_tools) > 1:
				self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool spool runout check. Skipping spool change estimation.", "error")
				return False
			tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

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

			density = filament.get('density', None)
			if density is None:
				await self._log_to_console(f"Spool {current_spool_id} has no filament density data, skipping check", "info")
				continue
			remaining_weight = self.cached_spool_info.get('remaining_weight', None)
			if remaining_weight is None:
				await self._log_to_console(f"Spool {current_spool_id} has no remaining weight data, skipping check", "info")
				continue
			weight = filament.get('weight', None)
			if weight is None:
				await self._log_to_console(f"Spool {current_spool_id} has no spool size data, skipping check", "info")
				continue
			if not spool_filament_name:
				await self._log_to_console(f"Spool {current_spool_id} has no filament name data, skipping check", "info")
				continue

			oob_runouts = self._get_oob_runouts(self.estimate_runouts(
				current_remaining_g=remaining_weight,
				density=density,
				spool_size_g=weight,
				start_volume=0
			))

			if not oob_runouts:
				msg = f"\nFilament spool runouts Check PASSED: Spool {current_spool_id} name '{spool_filament_name}' runs out within work hours"
				logging.info(msg)
			else:
				msg = (f"Filament spool runouts Check FAILED: Spool {current_spool_id}. `{spool_filament_name}` runs out outside of work hours ({self.work_hours_start} - {self.work_hours_end})")
				if self.spool_change_outside_work_hours_severity != 'error':
					await self._log_to_console(msg, self.spool_change_outside_work_hours_severity)
				else :
					for incriminated_runout in oob_runouts:
						msg += f"\n\tRunout {incriminated_runout.runout_index}: ETA {incriminated_runout.eta.strftime('%Y-%m-%d %H:%M:%S')} (Layer {incriminated_runout.estimated_layer}, Tool {incriminated_runout.tool_number})"
					self.error_body.append(msg)
					check_passed = False

		return check_passed

	async def _prep_checks(self, tool_gate_map=None) -> None:
		if self.uboe_metadata is None:
			self.error_body.append(f"[{self.config.get_name()}]: uboe_metadata component is required for Additional Pre-Print Checks plugin, but not found.")
			return False

		if tool_gate_map is not None:
			self.multi_tool_mapping = tool_gate_map

		if not self.spoolman:
			logging.warning("Spoolman component not available, skipping checks")
			self.error_body.append("Pre-print checks skipped: Spoolman not available")
			return False

		# Get current filename from Klipper
		await self._get_current_filename()
		if not self.filename:
			logging.warning("No current filename available, skipping checks")
			self.error_body.append("Pre-print checks skipped: No filename available")
			return False

		# Check if MMU mode
		self._is_hh_enabled()
		mode = "Multi-tool" if self.multi_tool_mapping else "Single-spool"
		logging.info(f"Running {mode} pre-print checks for file: {self.filename}")
		await self._log_to_console(f"Running {mode} checks for: {self.filename}", "info")

		# Clear cache at start of check session
		self._clear_spool_cache()

		self.extracted_metadata = ExtractedMetadata(self.metadata_storage, self.filename, self.error_body)
		return True

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
		try:
			logging.info("Starting Additional Pre-Print Checks...")

			logging.info(f"tool_gate_map: {tool_gate_map}")

			pre_checks_ok = await self._prep_checks(tool_gate_map)

			# #######################################
			# Run the checks
			# #######################################
			if self.is_hh:
				await self._log_to_console("Pre-print checks skipped: Redundant with HH consistency checks", "warning")
				return
			try:
				if pre_checks_ok:
					# Single-spool mode: check active spool
					if self.enable_weight_check:
						weight_ok = await self.check_print_weight(self.filename)
					else:
						weight_ok = True
					if self.enable_spool_change_on_work_hours_check:
						spool_change_ok = await self.check_spool_change_work_hours(self.filename)
					else:
						spool_change_ok = True
					# material_ok = await self.check_material_compliance(self.filename)
					if self.enable_filament_name_check:
						filament_name_ok = await self.check_filament_name_compliance(self.filename)
					else:
						filament_name_ok = True

					all_ok = weight_ok and filament_name_ok and spool_change_ok
				else :
					all_ok = False

				if all_ok:
					await self._log_to_console("✓ All pre-print checks PASSED", "info")
					if self.enable_filament_name_check:
						await self._log_to_console("   ✓ filament name compliance check passed", "info")
					if self.enable_material_check:
						await self._log_to_console("   ✓ material compliance check passed", "info")
					if self.enable_weight_check:
						await self._log_to_console("   ✓ sufficient filament available", "info")
				else:
					# Pause the print
					try:
						await self.klippy_apis.pause_print()
					except Exception as e:
						logging.error(f"Failed to pause print: {e}")
					logging.error(f'Errors : {self.error_body}')
					await self._log_to_console(msg=(".\n".join(self.error_body)), reason="Pre-Print Check Failed", severity="error")
			finally:
				# Clear cache after checks complete
				self._clear_spool_cache()
				self.error_body = []
		except Exception as e:
			msg = f"Unexpected error during pre-print checks: {e}"
			logging.error(msg)
			try:
				await self.klippy_apis.pause_print()
			except Exception as e:
				logging.error(f"Failed to pause print: {e}")
			await self._log_to_console(msg=msg, reason="Pre-Print Check Failed", severity="error")

def load_component(config: ConfigHelper) -> AdditionalPrePrintChecks:
	return AdditionalPrePrintChecks(config)
