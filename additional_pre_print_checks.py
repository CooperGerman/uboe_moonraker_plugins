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

MMU_NAME_FIELD   = 'printer_name'
MMU_GATE_FIELD   = 'mmu_gate_map'
DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"

class VirtualToolMap():
	'''
	Class to represent a virtual tool mapping in order to wrap under
	one representation mutiple scanrios such as :
	- Single-spool setups (static active spool)
	- Multi-material setups with MMU using Happy Hare
	- Tool-specific spool assignments (given by the klipper macros)
	This class builds upon request a tool to "gate" mapping, allowing
	checks to use a unified checking method, iterate over all tools,
	and check their associated spoools.
	'''
	def __init__(self, checks : AdditionalPrePrintChecks, tool_to_spoolid: List[int]|None = None):
		self.checks = checks
		self.tool_to_spoolid = tool_to_spoolid
		# remove all '-1' values from the tool to spool ID mapping
		# self.tool_to_spoolid = [v for v in self.tool_to_spoolid if v != -1] if self.tool_to_spoolid is not None else None
		self.tool_to_spoolid_mapping = []
		# tool to gate map naively is the tool index itself but in
  		# the case of HH overwrite it with those
		self.tool_to_gate_mapping = []
		self.multi = None

	async def build(self):

		# Spool_ids given at remote method call in a list coresponding to the tool numbers
		if self.tool_to_spoolid is not None:
			if self.checks.is_hh:
				logging.warning("Tool gate map provided but HH mode is enabled, ignoring tool gate map")
			else:
				self.tool_to_spoolid_mapping = self.tool_to_spoolid
				self.tool_to_gate_mapping = list(range(len(self.tool_to_spoolid)))
		else :
			# No given spool mapping, fallback to default gate mapping if available
			spool_id = await self.checks._init_spool()
			if spool_id is None:
				self.checks.error_body.append("No active spool set or cannot fetch spool info")
				return False
			else :
				self.tool_to_spoolid_mapping.append(spool_id)
				self.tool_to_gate_mapping.append(0)

		# Spool_ids given by the HH slicer tool mapping
		if self.checks.is_hh:
			self.tool_to_spoolid_mapping = []
			self.tool_to_gate_mapping = []
			ttg = self.checks.mmu_server.mmu_backend_config["mmu"]["ttg_map"]
			gate_spool_id = self.checks.mmu_server.mmu_backend_config["mmu"]["gate_spool_id"]
			for tool, tool_data in self.checks.mmu_server.mmu_backend_config["mmu"]["slicer_tool_map"]["tools"].items():
				logging.debug(f"Detected tool {tool} in slicer_tool_map: {tool_data}")
				self.tool_to_spoolid_mapping.append(gate_spool_id[ttg[int(tool)]])
				self.tool_to_gate_mapping.append(ttg[int(tool)])

		logging.info(f"tool_to_spoolid_mapping: {self.tool_to_spoolid_mapping}")


		if not self.checks.is_hh:
			for i in range(len(self.tool_to_spoolid_mapping)):
				self.tool_to_gate_mapping.append(i)

		self.multi = len(self.tool_to_spoolid_mapping) > 1

	async def fetch_tool_mapping_spool_info(self):
		'''
		Fetches the spool information for the current tool mapping.
  		'''
		self.tool_spoolid_info = []
		for spool_id in self.tool_to_spoolid_mapping:
			spool_info = await self.checks._fetch_spool_info(spool_id)
			self.tool_spoolid_info.append(spool_info)

	def get_spoolid_for_tool(self, tool_number: int) -> Optional[str]:
			return self.tool_to_spoolid_mapping[tool_number] if tool_number < len(self.tool_to_spoolid_mapping) else None

	def get_spool_info_for_tool(self, tool_number: int) -> Optional[Dict[str, Any]]:
		return self.tool_spoolid_info[tool_number] if tool_number < len(self.tool_spoolid_info) else None

	def get_gate_for_tool(self, tool_number: int) -> Optional[int]:
		return self.tool_to_gate_mapping[tool_number] if tool_number < len(self.tool_to_gate_mapping) else None


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
		self.extrusion_sample_points = self._parse_extrusion_sample_points(report_error=False)
		self.processed_by_uboe = self.raw_metadata.get('processed_by_uboe', False)
		# logging.info(f"Extrusion sample points: {self.extrusion_sample_points.to_dict()}")

	def _parse_extrusion_sample_points(self, report_error: bool = True) -> Optional[ExtrusionPoints]:
		points = self.raw_metadata.get('extrusion_sample_points')
		if points is None:
			if report_error:
				self.error_body.append("No extrusion sample points in file metadata, skipping spool change estimation")
			return None
		if not isinstance(points, list):
			points = [points]
		return ExtrusionPoints(dict_init=points)

	def refresh_from_storage(self, raw_metadata: MetadataStorage, filename: str) -> None:
		"""Refresh processing fields after asynchronous metadata enrichment."""
		metadata = raw_metadata.get(filename)
		if metadata is None:
			self.processed_by_uboe = False
			self.extrusion_sample_points = None
			return
		self.raw_metadata = metadata
		self.extrusion_sample_points = self._parse_extrusion_sample_points(report_error=False)
		self.processed_by_uboe = metadata.get('processed_by_uboe', False)

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
		tools = self.raw_metadata.get('referenced_tools', None)
		logging.debug(f"Raw metadata referenced tools: {tools}")
		if tools == []:
			tools = 0 # !FIXME : mmu_server should not write "; referenced_tools = "
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
		self.printer_info = self.server.get_host_info()
		self.printer_hostname = None
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

		self.enable_spoolman_setup_check = self.config.getboolean("enable_spoolman_setup_check", True)
		self.spoolman_bad_severity = self.config.get("spoolman_setup_severity", "error")

		if self.enable_spool_change_on_work_hours_check:
			self.work_hours_start = self.config.get("work_hours_start")
			self.work_hours_end = self.config.get("work_hours_end")
			# if leading or trailing zeros are missing, add them
			self.work_hours_start = time.fromisoformat(self.work_hours_start.zfill(5))
			self.work_hours_end = time.fromisoformat(self.work_hours_end.zfill(5))
			self.spool_runouts: Optional[List[RunoutEstimate]] = None

		self.filename = None
		self._uboe_metadata_updates: Dict[str, Dict[str, Any]] = {}
		self.server.register_event_handler(
			"uboe_metadata:update_started",
			self._handle_uboe_metadata_update_started,
		)
		self.server.register_event_handler(
			"uboe_metadata:update_finished",
			self._handle_uboe_metadata_update_finished,
		)

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
			self.enabled = True
		else:
			logging.warning("Additional Pre-Print Checks: Disabled (spoolman not available)")
			self.enabled = False

	async def component_init(self) -> None:
		"""Initialize component"""
		self.printer_hostname = self.printer_info["hostname"]
		if self.spoolman:
			await self._init_spool()
		try:
			self.uboe_metadata = self.server.lookup_component("uboe_metadata")
		except Exception as e:
			raise self.config.error(f"[{self.config.get_name()}]: {e}")

		self.mmu_server = self.server.lookup_component("mmu_server", None)

	async def _is_hh_enabled(self) -> bool:
		"""Check if MMU backend is present and enabled"""
		if self.mmu_server is None:
			self.is_hh = False
			return False
		await self.mmu_server._initialize_mmu()
		self.is_hh = self.mmu_server._mmu_backend_enabled()
		return self.is_hh

	async def _init_spool(self) -> Optional[int]:
		"""
		Get active spool ID from database and initialize/cache spool data.
		Combines getting active spool ID and initializing spool data functionality.

		Returns:
			Spool ID if successful, None if no active spool or fetch failed
		"""
		if self.is_hh:
			return self.mmu_server.mmu_backend_config["mmu"]["active_filament"]["spool_id"]

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
		for attempt in range(3):
			try:
				response = await asyncio.wait_for(
					self.spoolman.http_client.request(
						method="GET",
						url=f'{self.spoolman.spoolman_url}/v1/spool/{spool_id}',
						body=None
					),
					timeout=5
				)
				if response.status_code == 404:
					logging.error(f"Spool {spool_id} not found in Spoolman")
					return None
				if response.has_error():
					error_message = f"HTTP {response.status_code}"
				else:
					return response.json()
			except Exception as e:
				error_message = str(e)

			if attempt < 2:
				logging.warning(
					f"Failed to fetch spool {spool_id} (attempt {attempt + 1}/3): "
					f"{error_message}. Retrying."
				)
			else:
				logging.error(
					f"Failed to fetch spool {spool_id} after 3 attempts: {error_message}"
				)
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

	async def _log_to_console(self, msg: str, severity: str = "info", reason: str = "Pre-Print Checks", popup=False) -> None:
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

		if not popup:
			msg = msg.split("\n")
			msg = "<br>".join(msg)

		if (severity == 'error' or severity == 'alert'):
			logging.error(reason + ": " + msg)
		if (severity == 'warning'):
			logging.warning(reason + ": " + msg)
		if (severity == 'info'):
			logging.info(reason + ": " + msg)
		if (severity == 'debug'):
			logging.debug(reason + ": " + msg)

		if reason :
			_title = '<p style="font-weight: bold; margin:0; opacity:' + str(opacity) + '; color:' + color + '">' + reason + '</p>'
		else :
			_title = ''
		if msg:
			_msg = '<p style="margin:0; opacity:' + str(opacity) + '; color:' + color + '">' + msg + '</p>'
		else:
			_msg = ''

		try:
			msg = msg.replace("\n", "\\n")
			if popup :
				await self.klippy_apis.run_gcode('_UBOE_DIALOG MSG="%s" REASON="%s"' % (msg, reason))
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

		def closest_point(target_volume_mm3: float, tool_number: int) -> int | None:
			"""Find the layer value at the closest point to the target cumulative extruded volume."""
			if not self.extracted_metadata.extrusion_sample_points:
				return None
			closest_point = min(
				(p for p in self.extracted_metadata.extrusion_sample_points if p.extr_id == tool_number),
				key=lambda p: abs(p.extruded_volume_mm3 - target_volume_mm3),
				default=None
			)
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

		elapsed = closest_point(start_volume, extr_id)

		target_volume = start_volume + current_remaining_g / (density / 1000.0) # Convert grams to mm³ using filament density
		logging.info(f"Estimating runouts starting from volume {start_volume:.2f} mm³, current remaining {current_remaining_g:.2f}g, target volume {target_volume:.2f} mm³, max volume {max_volume:.2f} mm³")
		while target_volume <= max_volume:
			closest = closest_point(target_volume, extr_id)
			if closest is None:
				break
			layer = closest.layer

			eta_remaining, eta_progress = closest.minutes_remaining, closest.progress_percent
			runouts.append(
				RunoutEstimate(
					runout_index=runout_idx,
					target_volume_mm3=target_volume,
					estimated_minutes_remaining=eta_remaining,
					estimated_minutes_from_now=elapsed.minutes_remaining - eta_remaining,
					estimated_progress_percent=eta_progress,
					estimated_layer=layer,
					tool_number=extr_id
				)
			)
			runout_idx += 1
			target_volume += spool_size_g / (density / 1000.0) # Assuming single filament density for simplicity

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

	async def check_print_weight(self, virtual_tool_map: VirtualToolMap, filename: str) -> bool:
		"""
		Check if active spool has sufficient weight for the print job

		Args:
			filename: Path to gcode file (e.g., "gcodes/test.gcode")

		Returns:
			True if check passed or not applicable, False if failed
		"""
		if not self.enable_weight_check:
			return True

		if self.extracted_metadata.filament_weights is None:
			self.error_body.append("No filament weight requirements in file metadata, skipping weight check")
			return False

		if self.extracted_metadata.referenced_tools is None:
			self.error_body.append("A referenced tool index is required in file metadata for (single-spool) weight check, but not found. Skipping weight check.")
			return False
		elif len(self.extracted_metadata.referenced_tools) == 0:
			await self._log_to_console("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) weight check. Assuming only T0 usage.")
			self.extracted_metadata.referenced_tools = [0]  # Assume only T0 usage
		elif len(self.extracted_metadata.referenced_tools) > 1:
			self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool weight check. Skipping weight check.")
			return False
		tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

		# Check each tool/spool
		weight_ok = True
		for tool_index in tool_range:
			logging.info(f"Checking weight for tool index {tool_index}")
			current_spool_id = virtual_tool_map.get_spoolid_for_tool(tool_index)
			if current_spool_id == -1 or current_spool_id is None:
				self.error_body.append(f"No active spool for tool index {tool_index} (referenced by current file)")
				return False
			logging.info(f"Current spool ID for tool index {tool_index}: {current_spool_id}")
			spool_info = virtual_tool_map.get_spool_info_for_tool(tool_index)
			logging.info(f"Spool info for tool index {tool_index}: {spool_info}")

			# Get remaining weight from cached spool info
			remaining_weight = spool_info.get('remaining_weight')
			if remaining_weight is None:
				self.error_body.append("Spool has no remaining weight data, skipping check")
				return False

			required_weight = self.extracted_metadata.filament_weights[tool_index]
			# Perform check
			required_with_margin = required_weight + self.weight_margin
			sufficient = remaining_weight >= required_with_margin

			filament = spool_info.get('filament', {})
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

	async def check_filament_name_compliance(self, virtual_tool_map : VirtualToolMap, filename: str) -> bool:
		"""
		Check if referenced tool's spool filament name matches metadata filament name

		Args:
			filename: Path to gcode file

		Returns:
			True if compliant or check not applicable, False if error severity and mismatch
		"""
		if not self.enable_filament_name_check:
			return True

		if self.extracted_metadata.referenced_tools is None:
			self.error_body.append("A referenced tool index is required in file metadata for (single-spool) weight check, but not found. Skipping weight check.")
			return False
		elif len(self.extracted_metadata.referenced_tools) == 0:
			await self._log_to_console("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) weight check. Assuming only T0 usage.")
			self.extracted_metadata.referenced_tools = [0]  # Assume only T0 usage
		elif len(self.extracted_metadata.referenced_tools) > 1:
			self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool weight check. Skipping weight check.")
			return False
		tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

		check_passed = True

		for tool_index in tool_range:
			current_spool_id = virtual_tool_map.get_spoolid_for_tool(tool_index)
			if current_spool_id == -1 or current_spool_id is None:
				self.error_body.append(f"No active spool for tool index {tool_index} (referenced by current file)")
				return False
			spool_info = virtual_tool_map.get_spool_info_for_tool(tool_index)

			# Get spool filament name
			filament = spool_info.get('filament', {})
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

	async def check_spool_change_work_hours(self, virtual_tool_map : VirtualToolMap, filename: str) -> bool:
		"""
		Check if spool changes are within allowed work hours based on metadata and current time.

		Args:
			filename: Path to gcode file

		Returns:
			True if spool change is within allowed work hours, False otherwise.
		"""
		if not self.enable_spool_change_on_work_hours_check:
			return True

		if self.extracted_metadata.extrusion_sample_points is None:
			await self._log_to_console("No extrusion sample points found in metadata, skipping spool change work hours check", "warning")
			return False

		if self.extracted_metadata.referenced_tools is None:
			self.error_body.append("A referenced tool index is required in file metadata for (single-spool) weight check, but not found. Skipping weight check.")
			return False
		elif len(self.extracted_metadata.referenced_tools) == 0:
			await self._log_to_console("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) weight check. Assuming only T0 usage.")
			self.extracted_metadata.referenced_tools = [0]  # Assume only T0 usage
		elif len(self.extracted_metadata.referenced_tools) > 1:
			self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool weight check. Skipping weight check.")
			return False
		tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

		check_passed = True

		for tool_index in tool_range:
			current_spool_id = virtual_tool_map.get_spoolid_for_tool(tool_index)
			if current_spool_id == -1 or current_spool_id is None:
				self.error_body.append(f"No active spool for tool index {tool_index} (referenced by current file)")
				return False
			spool_info = virtual_tool_map.get_spool_info_for_tool(tool_index)

			# Get spool filament name
			filament = spool_info.get('filament', {})
			spool_filament_name = filament.get('name', '').strip()

			density = filament.get('density', None)
			if density is None:
				await self._log_to_console(f"Spool {current_spool_id} has no filament density data, skipping check", "info")
				continue
			remaining_weight = spool_info.get('remaining_weight', None)
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


			self.spool_runouts = self.estimate_runouts(
				current_remaining_g=remaining_weight,
				density=density,
				spool_size_g=weight,
				start_volume=0
			)
			oob_runouts = self._get_oob_runouts(self.spool_runouts)

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

	async def check_spoolman_setup(self, virtual_tool_map : VirtualToolMap, filename: str) -> bool:
		'''Check if the spoolman setup is correct for the given file. Returns True if setup is valid, False otherwise.'''
		# If only one referenced tool, spoolman setup should have 1 active spool assigned to the current {printer : <hostname>, gate: <gate_number>}
		# Get active spool and initialize data
		if self.extracted_metadata.referenced_tools is None:
			self.error_body.append("A referenced tool index is required in file metadata for (single-spool) weight check, but not found. Skipping weight check.")
			return False
		elif len(self.extracted_metadata.referenced_tools) == 0:
			await self._log_to_console("Referenced tool index list in file metadata is empty but a tool index is required for (single-spool) weight check. Assuming only T0 usage.")
			self.extracted_metadata.referenced_tools = [0]  # Assume only T0 usage
		elif len(self.extracted_metadata.referenced_tools) > 1:
			self.error_body.append("Multiple referenced tools found in file metadata but only one is supported for single-spool weight check. Skipping weight check.")
			return False
		tool_range = self.extracted_metadata.referenced_tools  # Single tool TN (where N is the tool index)

		check_passed = True

		for tool_index in tool_range:
			current_spool_id = virtual_tool_map.get_spoolid_for_tool(tool_index)
			if current_spool_id == -1 or current_spool_id is None:
				self.error_body.append(f"No active spool for tool index {tool_index} (referenced by current file)")
				return False
			spool_info = virtual_tool_map.get_spool_info_for_tool(tool_index)
			# check if spool id archived
			if spool_info.get('archived', False):
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} is archived, this should occur, but may indicate potential issues")
				await self._log_to_console(f"Spool {current_spool_id} is archived, watch out for potential issues", self.spoolman_bad_severity)

			# Get spool extra field
			extra = spool_info.get('extra', {})
			spool_printer_name = extra.get(MMU_NAME_FIELD, None)
			spool_mmu_gate = extra.get(MMU_GATE_FIELD, None)

			# First check if fields are present
			if not extra:
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} has no extra field")
					return False
				await self._log_to_console(f"Spool {current_spool_id} has no extra field, skipping check", self.spoolman_bad_severity)
			if spool_printer_name is None:
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} has no {MMU_NAME_FIELD} field")
					return False
				await self._log_to_console(f"Spool {current_spool_id} has no {MMU_NAME_FIELD} field", self.spoolman_bad_severity)
			if spool_mmu_gate is None:
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} has no {MMU_GATE_FIELD} field")
					return False
				await self._log_to_console(f"Spool {current_spool_id} has no {MMU_GATE_FIELD} field", self.spoolman_bad_severity)

			# Then check for field values coherency
			if spool_printer_name is None or spool_mmu_gate is None:
				return False

			spool_printer_name = spool_printer_name.strip('"').strip("'") if type(spool_printer_name) == str else spool_printer_name

			if not spool_printer_name:
				check_passed = False
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} is assigned to no printer.")
				await self._log_to_console(f"Spool {current_spool_id} is assigned to no printer.", self.spoolman_bad_severity)
			elif spool_printer_name != self.printer_hostname:
				check_passed = False
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} is assigned to printer `{spool_printer_name}` but the actual printer hostname is `{self.printer_hostname}`")
				await self._log_to_console(f"Spool {current_spool_id} is assigned to printer `{spool_printer_name}` but the actual printer hostname is `{self.printer_hostname}`", self.spoolman_bad_severity)

			expected_gate = virtual_tool_map.get_gate_for_tool(tool_index)
			if int(spool_mmu_gate) != expected_gate:
				check_passed = False
				if self.spoolman_bad_severity == 'error':
					self.error_body.append(f"Spool {current_spool_id} is assigned to MMU gate `{spool_mmu_gate}` but the actual expected gate for tool index is `{tool_index}` is `{expected_gate}`")
				await self._log_to_console(f"Spool {current_spool_id} is assigned to MMU gate `{spool_mmu_gate}` but the actual expected gate for tool index is `{tool_index}` is `{expected_gate}`", self.spoolman_bad_severity)

			if check_passed:
				msg = f"Spoolman setup check Check PASSED: Spool {current_spool_id} name and gate match"
				logging.info(msg)

		return check_passed

	async def _handle_uboe_metadata_update_started(self, filename: str) -> None:
		self._uboe_metadata_updates[filename] = {
			"event": asyncio.Event(),
			"success": False,
		}

	async def _handle_uboe_metadata_update_finished(
		self,
		filename: str,
		success: bool,
	) -> None:
		update = self._uboe_metadata_updates.setdefault(
			filename,
			{"event": asyncio.Event(), "success": False},
		)
		update["success"] = success
		update["event"].set()

	def _has_valid_extrusion_sample_points(self) -> bool:
		return (
			self.extracted_metadata.processed_by_uboe is True
			and self.extracted_metadata.extrusion_sample_points is not None
			and len(self.extracted_metadata.extrusion_sample_points) >= 2
		)

	async def _wait_for_processed_by_uboe(self) -> tuple[bool, float]:
		# Wait for the current metadata entry to contain valid enrichment.
		timeout = 30
		start_time = asyncio.get_event_loop().time()
		self.extracted_metadata.refresh_from_storage(
			self.metadata_storage,
			self.filename,
		)
		if self._has_valid_extrusion_sample_points():
			return True, asyncio.get_event_loop().time() - start_time

		update = self._uboe_metadata_updates.setdefault(
			self.filename,
			{"event": asyncio.Event(), "success": False},
		)
		remaining = timeout - (asyncio.get_event_loop().time() - start_time)
		try:
			await asyncio.wait_for(update["event"].wait(), max(0, remaining))
		except asyncio.TimeoutError:
			self.error_body.append(
				f"[{self.config.get_name()}]: Timeout ({timeout} seconds) "
				"waiting for uboe metadata processing."
			)
			return False, asyncio.get_event_loop().time() - start_time

		self.extracted_metadata.refresh_from_storage(
			self.metadata_storage,
			self.filename,
		)
		if not update["success"] or not self._has_valid_extrusion_sample_points():
			self.error_body.append(
				"uboe_metadata processing completed without valid extrusion sample points."
			)
			return False, asyncio.get_event_loop().time() - start_time
		return True, asyncio.get_event_loop().time() - start_time

	async def _prep_checks(self, tool_gate_map=None, referenced_tools=None) -> bool | VirtualToolMap:
		'''
		Prepare the environment for running pre-print checks.
		This includes verifying the availability of required components,
		initializing metadata extraction, and setting up tool mappings.
		'''
		if self.uboe_metadata is None:
			self.error_body.append(f"[{self.config.get_name()}]: uboe_metadata component is required for Additional Pre-Print Checks plugin, but not found.")
			return False

		if not self.spoolman:
			logging.warning("Spoolman component not available, skipping checks")
			self.error_body.append("Pre-print checks skipped: Spoolman not available")
			return False

		virtual_tool_map = VirtualToolMap(self, tool_gate_map)
		await virtual_tool_map.build()
		await virtual_tool_map.fetch_tool_mapping_spool_info()
		if len(virtual_tool_map.tool_to_spoolid_mapping) == 0:
			logging.warning("No spools mapped to tools, skipping checks")
			self.error_body.append("Pre-print checks skipped: No spools mapped to tools")
			return False

		# Get current filename from Klipper
		await self._get_current_filename()
		if not self.filename:
			logging.warning("No current filename available, skipping checks")
			self.error_body.append("Pre-print checks skipped: No filename available")
			return False

		_nb_errors = len(self.error_body)
		self.extracted_metadata = ExtractedMetadata(self.metadata_storage, self.filename, self.error_body)
		if referenced_tools is not None:
			logging.info(f"Referenced tools forced through GCODE CALL for pre-print checks: {referenced_tools}")
			self.extracted_metadata.referenced_tools = referenced_tools
		# wait for valid uboe metadata enrichment
		processed, elapsed_time = await self._wait_for_processed_by_uboe()
		if processed:
			logging.info(f"uboe_metadata has valid extrusion sample points. (Found in {elapsed_time:.2f} seconds)")

		if len(self.error_body) > _nb_errors:
			logging.error(f"Errors found during preparation checks: {self.error_body[_nb_errors:]}")
			return False

		return virtual_tool_map

	async def run_checks(self, tool_gate_map=None, referenced_tools=None) -> None:
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
			# Check if MMU mode
			await self._is_hh_enabled()
			detail = " "
			if self.is_hh:
				detail = " (HH mode enabled) "
			virtual_tool_map = await self._prep_checks(tool_gate_map, referenced_tools)

			try:
				if virtual_tool_map:
					mode = "Multi-tool" if virtual_tool_map.multi else "Single-spool"
					logging.info(f"Running {mode} pre-print checks{detail}for file: {self.filename}")
					await self._log_to_console(f"Running {mode} checks for: {self.filename}", "info")
					# Single-spool mode: check active spool
					if self.enable_weight_check:
						weight_ok = await self.check_print_weight(virtual_tool_map, self.filename)
					else:
						weight_ok = True
					if self.enable_spool_change_on_work_hours_check:
						spool_change_ok = await self.check_spool_change_work_hours(virtual_tool_map, self.filename)
					else:
						spool_change_ok = True
					# material_ok = await self.check_material_compliance(self.filename)
					if self.enable_filament_name_check:
						filament_name_ok = await self.check_filament_name_compliance(virtual_tool_map, self.filename)
					else:
						filament_name_ok = True

					# check spoolman setup
					if self.enable_spoolman_setup_check:
						spoolman_ok = await self.check_spoolman_setup(virtual_tool_map, self.filename)
					else:
						spoolman_ok = True

					all_ok = weight_ok and filament_name_ok and spool_change_ok and spoolman_ok
				else :
					all_ok = False

				if all_ok:
					msg = []
					msg.append("✓ All pre-print checks PASSED")
					if self.enable_spoolman_setup_check:
						msg.append("   ✓ spoolman setup check passed")
					if self.enable_filament_name_check:
						msg.append("   ✓ filament name compliance check passed")
					if self.enable_material_check:
						msg.append("   ✓ material compliance check passed")
					if self.enable_weight_check:
						msg.append("   ✓ sufficient filament available")
					if self.enable_spool_change_on_work_hours_check:
						if self.spool_runouts:
							msg.append(f"   ✓ spool change(s) within work hours ({self.work_hours_start} - {self.work_hours_end})")
						else :
							msg.append("   ✓ No spool runouts detected")
					await self._log_to_console(msg=(".\n".join(msg)), reason="Pre-Print Check Passed", severity="success")

				else:
					# Pause the print
					try:
						await self.klippy_apis.pause_print()
					except Exception as e:
						logging.error(f"Failed to pause print: {e}")
					# remove duplicate error messages
					self.error_body = list(dict.fromkeys(self.error_body))
					logging.error(f'Errors : {self.error_body}')
					await self._log_to_console(msg=(".\n".join(self.error_body)), reason="Pre-Print Check Failed", severity="error", popup=True)
			finally:
				# Clear cache after checks complete
				self._clear_spool_cache()
				self.error_body = []
				self.spool_runouts = None
		except Exception as e:
			msg = f"Unexpected error during pre-print checks: {e}.\n See moonraker.log for traceback."
			logging.error(msg)
			try:
				await self.klippy_apis.pause_print()
			except Exception as e:
				logging.error(f"Failed to pause print: {e}")
			await self._log_to_console(msg=msg, reason="Pre-Print Check Failed", severity="error", popup=True)
			raise e

def load_component(config: ConfigHelper) -> AdditionalPrePrintChecks:
	return AdditionalPrePrintChecks(config)
