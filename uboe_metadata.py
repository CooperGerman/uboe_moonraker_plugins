# Uboe Metadata Enrichment Plugin
#
# Backfills filament_weights/filament_name for Moonraker versions that
# predate built-in support, and provides a hook for custom gcode processors,
# without patching moonraker's file_manager/metadata.py subprocess script.
from __future__ import annotations
import re
import json
import logging
from packaging.version import Version
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Iterable

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..server import Server
    from .file_manager.file_manager import FileManager

MIN_NATIVE_WEIGHT_SUPPORT = Version("0.10.0")


def regex_find_floats(pattern: str, data: str) -> List[float]:
    pattern = pattern.replace(r"(%F)", r"([0-9]*\.?[0-9]+)")
    matches = re.findall(pattern, data)
    try:
        return [float(m) for m in matches]
    except Exception:
        return []


def regex_find_string(pattern: str, separators: str, data: str) -> List[str]:
    match = re.search(pattern.replace(r"(%S)", r"(.*)"), data)
    if not match or not match.group(1):
        return []
    sep_esc = re.escape(separators)
    parsed: List[str] = []
    for m in re.finditer(rf'\s*(")(?:\\"|[^"])*"\s*|[^{sep_esc}]+', match.group(1)):
        val, sep = m.group(0, 1)
        val = val.strip()
        if sep:
            val = val[1:-1].replace(rf'\{sep}', sep).strip()
        if val:
            parsed.append(val)
    return parsed

class ExtrusionSamplePoint:
	def __init__(self, line_number: int, extruded_volume_mm3: float, minutes_remaining: float, progress_percent: float, layer: int, extr_id: int):
		self.line_number = line_number
		self.extruded_volume_mm3 = extruded_volume_mm3
		self.minutes_remaining = minutes_remaining
		self.progress_percent = progress_percent
		self.layer = layer
		self.extr_id = extr_id

	def to_dict(self) -> dict:
		return self.__dict__

class ExtrusionPoints:
	def __init__(self):
		self.points: list[ExtrusionSamplePoint] = []

	def add_point(self, point: ExtrusionSamplePoint):
		self.points.append(point)

	def last(self) -> ExtrusionSamplePoint:
		if not self.points:
			raise ValueError("No points available.")
		return self.points[-1]

	def first(self) -> ExtrusionSamplePoint:
		if not self.points:
			raise ValueError("No points available.")
		return self.points[0]

	def __len__(self) -> int:
		return len(self.points)

	def __getitem__(self, index: int) -> ExtrusionSamplePoint:
		return self.points[index]

	def to_dict(self) -> list[dict]:
		return [point.to_dict() for point in self.points]

class UboeMetadata:
    def __init__(self, config: ConfigHelper) -> None:
        self.server: Server = config.get_server()
        moonraker_version = self.server.get_app_args()["software_version"]
        self.needs_weight_patch = False
        try:
            self.needs_weight_patch = Version(
                moonraker_version.split("-")[0]
            ) < MIN_NATIVE_WEIGHT_SUPPORT
        except Exception:
            logging.info(
                f"UboeMetadata: Unable to parse Moonraker version "
                f"'{moonraker_version}', skipping legacy weight patch"
            )
        if self.needs_weight_patch:
            logging.warning(
                f"UboeMetadata: Detected older Moonraker version "
                f"({moonraker_version}) without built-in filament weight "
                "support, enriching metadata after extraction"
            )
        self.server.register_event_handler(
            "file_manager:metadata_processed", self._on_metadata_processed
        )
        self.extrusion_sample_points : ExtrusionPoints = ExtrusionPoints()
        self.file_manager: FileManager = self.server.lookup_component("file_manager")

    async def _on_metadata_processed(self, fname: str) -> None:
        '''
        Patch metadata to include filament weight and name if missing.
        Also extract volumes at layers for spool change estimations.
        The additional_pre_print_checks plugin will use this to check if the
        spool changes happen during presence times and other stuff.
        '''
        gc_metadata = self.file_manager.get_metadata_storage()
        metadata = gc_metadata.get(fname)
        updated = dict(metadata)
        if self.needs_weight_patch:
            if metadata is None or metadata.get("slicer") != "PrusaSlicer":
                return
            if "filament_weights" in metadata and "filament_name" in metadata:
                return
            try:
                gc_path = self.file_manager.get_directory()
                with open(f"{gc_path}/{fname}", "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 1024 * 1024))
                    footer_data = f.read().decode(errors="ignore")
            except OSError:
                logging.exception(f"UboeMetadata: Unable to read {fname}")
                return
            if "filament_weights" not in updated:
                line = regex_find_string(
                    r'filament\sused\s\[g\]\s=\s(%S)\n', ",;", footer_data
                )
                weights = regex_find_floats(r"(%F)", " ".join(line))
                if weights:
                    updated["filament_weights"] = weights
            if "filament_name" not in updated:
                names = regex_find_string(
                    r";\sfilament_settings_id\s=\s(%S)", ",;", footer_data
                )
                if len(names) > 1:
                    updated["filament_name"] = json.dumps(names)
                elif names:
                    updated["filament_name"] = names[0]

        eventloop = self.server.get_event_loop()
        await eventloop.run_in_thread( self.extract_extrusion_sample_points, fname )

        updated["extrusion_sample_points"] = self.extrusion_sample_points.to_dict()

        gc_metadata.insert(fname, updated)

    async def extract_extrusion_sample_points(self, fname: str) -> None:
        # function should work with mock files for testing, so we define a helper function to iterate over lines
        def _iter_lines() -> Iterable[tuple[int, str]]:
            gc_path = self.file_manager.get_directory()
            with open(f"{gc_path}/{fname}", "r") as f:
                for idx, line in enumerate(f, start=1):
                    yield idx, line
        logging.debug("Extracting extrusion sample points from gcode file...")
        LAY = re.compile(r'BEFORE_LAYER_CHANGE\b[^\n\r]*\bHEIGHT\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*LAYER\s*=\s*([0-9]+)')
        CHG_EST_CMD = re.compile(r"UBOE_SPOOL_CHANGE_ESTIMATE\b[^\n\r]*\bEXTR_ID\s*[:=]\s*([0-9]+)\s*\b[^\n\r]*\bV\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)")
        M73_RE = re.compile(r"\bM73\b(?:[^\n\r]*\bP([0-9]+(?:\.[0-9]+)?))?[^\n\r]*\bR([0-9]+(?:\.[0-9]+)?)")

        pending_volume: float = None
        pending_extr_id: int = None
        current_layer: int = 0

        for line_number, line in _iter_lines():
            lay_match = LAY.search(line)
            if lay_match:
                current_layer = int(lay_match.group(2))
                logging.debug(f"Line {line_number}: Found layer change to layer {current_layer}.")

            volume_match = CHG_EST_CMD.search(line)
            if volume_match:
                pending_extr_id = int(volume_match.group(1))
                pending_volume = float(volume_match.group(2))
                logging.debug(f"Line {line_number}: Found volume {pending_volume} mm³ for extruder ID {pending_extr_id}.")

            m73_match = M73_RE.search(line)
            if m73_match:
                last_progress = float(m73_match.group(1)) if m73_match.group(1) else 0.0
                last_remaining = float(m73_match.group(2))
                logging.debug(f"Line {line_number}: Found M73 progress {last_progress}% with remaining {last_remaining} minutes.")

            if pending_volume is not None:
                self.extrusion_sample_points.add_point(
                    ExtrusionSamplePoint(
                        line_number=line_number,
                        extruded_volume_mm3=pending_volume,
                        minutes_remaining=last_remaining,
                        progress_percent=last_progress,
                        layer=current_layer,
                        extr_id=pending_extr_id
                    )
                )
                pending_volume = None  # Clear the pending volume for this extruder ID

        # Keep points with strictly increasing cumulative weight for interpolation.
        non_monotonic_points: list[ExtrusionSamplePoint] = []
        prev_point: ExtrusionSamplePoint = None
        for i, point in enumerate(self.extrusion_sample_points.points):
            if i > 0 and point.extruded_volume_mm3 < prev_point.extruded_volume_mm3:
                non_monotonic_points.append(point)
                logging.warning(f"{point.to_dict()} is non-monotonic and will be ignored for interpolation.")
                logging.warning(f"Previous point was {prev_point.to_dict()}.")
            prev_point = point

        if non_monotonic_points:
            logging.critical(f"Found {len(non_monotonic_points)} non-monotonic points in extrusion sample points. This should not happen. Please check the gcode file for inconsistencies.")

        if len(self.extrusion_sample_points.points) < 2:
            logging.warning(
                "Could not extract enough paired points. Make sure the file contains both "
                "UBOE_SPOOL_CHANGE_ESTIMATE V=..., M73 R... entries and _ON_LAYER_CHANGE LAYER=... entries."
            )


def load_component(config: ConfigHelper) -> UboeMetadata:
    return UboeMetadata(config)
