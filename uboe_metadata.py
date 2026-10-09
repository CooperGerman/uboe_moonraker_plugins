#!/usr/bin/env python3
# Uboe Metadata Enrichment Plugin
#
# Backfills filament_weights/filament_name for Moonraker versions that
# predate built-in support, and provides a hook for custom gcode processors,
# without patching moonraker's file_manager/metadata.py subprocess script.
from __future__ import annotations
import sys
import re
import json
import logging
import asyncio
import pathlib
import shlex
import argparse
from packaging.version import Version
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..server import Server
    from .shell_command import ShellCommandFactory
    from .file_manager.file_manager import FileManager

MIN_NATIVE_WEIGHT_SUPPORT = Version("0.10.0")
MIN_NATIVE_OBJ_HEIGHT = None

def regex_find_float(pattern: str, data: str) -> Optional[float]:
    pattern = pattern.replace(r"(%F)", r"([0-9]*\.?[0-9]+)")
    match = re.search(pattern, data)
    val: Optional[float] = None
    if match:
        try:
            val = float(match.group(1))
        except Exception:
            return None
    return val

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
    def __init__(self, dict_init=None):
        self.points: list[ExtrusionSamplePoint] = []
        if dict_init is not None:
            for point_dict in dict_init:
                __temp = {}
                __temp['line_number'] = int(point_dict.get('line_number'))
                __temp['extruded_volume_mm3'] = float(point_dict.get('extruded_volume_mm3'))
                __temp['minutes_remaining'] = float(point_dict.get('minutes_remaining'))
                __temp['progress_percent'] = float(point_dict.get('progress_percent'))
                __temp['layer'] = int(point_dict.get('layer'))
                __temp['extr_id'] = int(point_dict.get('extr_id'))
                point = ExtrusionSamplePoint(**__temp)
                self.points.append(point)

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

    def has_point(self, extr_id: int, volume: float) -> bool:
        """Check if a point with the given extr_id and volume exists."""
        for i, point in enumerate(self.points):
            if int(point.extr_id) == int(extr_id) and float(point.extruded_volume_mm3) == float(volume):
                return True
        logging.info(f"No points found starting from extruder ID {extr_id} and volume being exactly {volume}.")
        return False

class UboeMetadata:
    def __init__(self, config: ConfigHelper) -> None:
        self.server: Server = config.get_server()
        moonraker_version = self.server.get_app_args()["software_version"]
        self.needs_weight_patch = False
        self.needs_obj_height_patch = False
        try:
            self.needs_weight_patch = Version(
                moonraker_version.split("-")[0]
            ) < MIN_NATIVE_WEIGHT_SUPPORT
            if MIN_NATIVE_OBJ_HEIGHT is not None:
                self.needs_obj_height_patch = Version(
                    moonraker_version.split("-")[0]
                ) < MIN_NATIVE_OBJ_HEIGHT
            else :
                self.needs_obj_height_patch = True
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
        if self.needs_obj_height_patch:
            logging.warning(
                f"UboeMetadata: Detected older Moonraker version "
                f"({moonraker_version}) with old BEFORE_LAYER_CHANGE built-in object height "
                "support, enriching metadata after extraction"
            )
        self.server.register_event_handler(
            "file_manager:metadata_processed", self._on_metadata_processed
        )
        self.file_manager: FileManager = self.server.lookup_component("file_manager")
        self.cmd_lock = asyncio.Lock()

    def _gen_spoolchange_est_cmd(
            self,
            gc_path: pathlib.Path,
        ) -> str:
            cmd = str(__file__)
            cmd = f"{sys.executable} {cmd} {shlex.quote(str(gc_path))}"
            return cmd

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
        gc_path = self.file_manager.get_directory()
        # only read the header and footer if we need to patch object height or filament weight
        if self.needs_obj_height_patch or self.needs_weight_patch:
            try:
                with open(f"{gc_path}/{fname}", "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 1024 * 1024))
                    footer_data = f.read().decode(errors="ignore")
                with open(f"{gc_path}/{fname}", "rb") as f:
                    f.seek(0, 0)
                    header_data = f.read(1024 * 1024).decode(errors="ignore")
            except OSError:
                logging.exception(f"UboeMetadata: Unable to read {fname}")
                return

        if self.needs_weight_patch:
            if metadata is None or metadata.get("slicer") != "PrusaSlicer":
                return
            if "filament_weights" in metadata and "filament_name" in metadata:
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

        if self.needs_obj_height_patch:
            line= regex_find_float(r"; max_z_height: (%F)", header_data)
            if not "object_height" in updated and line is not None:
                updated["object_height"] = line

        async with self.cmd_lock:
            scmd: ShellCommandFactory = self.server.lookup_component("shell_command")
            sc_est_cmd = self._gen_spoolchange_est_cmd(f"{gc_path}/{fname}")
            # catch the errors that could have occured running the command
            try:
                ret = await scmd.exec_cmd(sc_est_cmd, 60., log_stderr=True)
            except Exception as e:
                logging.error(f"UboeMetadata: Error executing uboe_metadata for {fname}")
                return
            # if the script exited with an exception, ret might not contain valid JSON
            # and json.loads(ret) could raise an error
            try:
                extrusion_sample_points = ExtrusionPoints(dict_init=json.loads(ret))
            except json.JSONDecodeError:
                logging.error(f"UboeMetadata: Failed to decode JSON from spool change estimate command output for {fname}: {ret}")
                return
            # Keep points with strictly increasing cumulative weight for interpolation.
            non_monotonic_points: list[ExtrusionSamplePoint] = []
            prev_point: ExtrusionSamplePoint = None
            for i, point in enumerate(extrusion_sample_points.points):
                if i > 0 and point.extr_id == prev_point.extr_id and point.extruded_volume_mm3 < prev_point.extruded_volume_mm3:
                    non_monotonic_points.append(point)
                    logging.warning(f"{point.to_dict()} is non-monotonic and will be ignored for interpolation.")
                    logging.warning(f"Previous point was {prev_point.to_dict()}.")
                prev_point = point

            if non_monotonic_points:
                logging.warning(f"Found {len(non_monotonic_points)} non-monotonic points in extrusion sample points. This should not happen. Please check the gcode file for inconsistencies.")

            if len(extrusion_sample_points.points) < 2:
                logging.warning(
                    "Could not extract enough paired points. Make sure OrcaSlicer runs "
                    "~/.local/bin/orca-post-proc-script and appends the "
                    "UBOE_EXTRUSION_SAMPLE_POINTS footer."
                )

        updated["extrusion_sample_points"] = extrusion_sample_points.to_dict()
        logging.info(f"UboeMetadata: Updated metadata with extrusion sample points for {fname}.")
        gc_metadata.insert(fname, updated)

def extract_extrusion_sample_points(gc_path: str) -> ExtrusionPoints:
    logging.info(f"Extracting extrusion sample points from the executable-block footer of {gc_path}.")
    sample_points_re = re.compile(
        r"^[ \t]*;[ \t]*UBOE_EXTRUSION_SAMPLE_POINTS:[ \t]*(\[[^\r\n]*\])[ \t]*$"
    )

    try:
        with open(gc_path, "r", errors="replace") as gcode_file:
            executable_block_end_found = False
            marker_match = None
            for line in gcode_file:
                if not executable_block_end_found:
                    if line.strip() == "; EXECUTABLE_BLOCK_END":
                        executable_block_end_found = True
                    continue

                match = sample_points_re.search(line)
                if match:
                    marker_match = match
    except OSError:
        raise OSError(f"Unable to read GCode file: {gc_path}")

    if not executable_block_end_found:
        raise ValueError(f"No ; EXECUTABLE_BLOCK_END marker found in {gc_path}")

    if marker_match is None:
        raise ValueError(
            f"No UBOE_EXTRUSION_SAMPLE_POINTS footer found after "
            f"; EXECUTABLE_BLOCK_END in {gc_path}"
        )

    try:
        point_data = json.loads(marker_match.group(1))
        if not isinstance(point_data, list):
            raise ValueError("sample point marker must contain a JSON list")
        extrusion_sample_points = ExtrusionPoints(dict_init=point_data)
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(f"Invalid UBOE_EXTRUSION_SAMPLE_POINTS footer in {gc_path}")

    return extrusion_sample_points


def load_component(config: ConfigHelper) -> UboeMetadata:
    return UboeMetadata(config)


def main(args) -> None:
    sys.stdout.write( json.dumps(extract_extrusion_sample_points(args.gc_path).to_dict()) + "\n" )

if __name__ == "__main__":
    # Parse start arguments
    parser = argparse.ArgumentParser(
        description="GCode Metadata Extraction Utility")
    parser.add_argument(
        "gc_path",
        default=None,
        help="Path to the GCode file to extract metadata from"
    )
    args = parser.parse_args()
    main(args)