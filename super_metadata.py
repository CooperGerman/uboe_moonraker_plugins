#!/usr/bin/env python3
from __future__ import annotations
import json
import argparse
import re
import os
import sys
import base64
import traceback
import tempfile
import zipfile
import shutil
import uuid
import logging
from PIL import Image

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Dict,
    List,
    Tuple,
    Type,
)
if TYPE_CHECKING:
    pass
# Make it look like we are running in the file_manager directory
directory = os.path.dirname(os.path.abspath(__file__))
target_dir = directory + "/file_manager"
os.chdir(target_dir)
sys.path.insert(0, target_dir)

import metadata

def regex_find_strings(pattern: str, separators: str, data: str) -> List[str]:
    pattern = pattern.replace(r"(%S)", r"(.*)")
    match = re.search(pattern, data)
    if match and match.group(1):
        separators = re.escape(separators)
        pattern = rf'\s*(")(?:\\"|[^"])*"\s*|[^{separators}]+'
        parsed_matches: List[str] = []
        for m in re.finditer(pattern, match.group(1)):
            (val, sep) = m.group(0, 1)
            val = val.strip()
            if sep:
                val = val[1:-1].replace(rf'\{sep}', sep).strip()
            if val:
                parsed_matches.append(val)
        return parsed_matches
    return []

# Define our custom class inheriting from PrusaSlicer
class SuperPrusaSlicer(metadata.PrusaSlicer):

    def _verify_need_for_patch(self, method_name):
        # verify that the methods we are adding are actually needed (no already present)
        metadata.logger.info(f"Verifying need for patch method: {method_name}")
        if method_name in metadata.PrusaSlicer.__dict__:
            metadata.logger.error(f"Method {method_name} already exists in PrusaSlicer, patch might not be needed anymore!")

    def parse_filament_weights(self) -> Optional[List[float]]:
        self._verify_need_for_patch("parse_filament_weights")
        line = metadata.regex_find_string(r'filament\sused\s\[g\]\s=\s(%S)\n', self.footer_data)
        if line:
            weights = metadata.regex_find_floats(
                r"(%F)", line
            )
            if weights:
                return weights
        return None

    def parse_filament_name(self) -> Optional[str]:
        self._verify_need_for_patch("parse_filament_name")
        result = regex_find_strings(
            r";\sfilament_settings_id\s=\s(%S)", ",;", self.footer_data
        )
        if len(result) > 1:
            return json.dumps(result)
        elif result:
            return result[0]
        return None

# Monkey-patch the SUPPORTED_SLICERS list in the metadata module
# We replace the original PrusaSlicer with our SuperPrusaSlicer
new_supported_slicers = []
for slicer in metadata.SUPPORTED_SLICERS:
    # Check if it is exactly PrusaSlicer (not a subclass like Slic3rPE)
    if slicer is metadata.PrusaSlicer:
        new_supported_slicers.append(SuperPrusaSlicer)
    else:
        new_supported_slicers.append(slicer)

metadata.SUPPORTED_SLICERS = new_supported_slicers
metadata.SUPPORTED_DATA.append("filament_weights")

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Supercharged GCode Metadata Extraction Utility")
    parser.add_argument(
        "-f", "--filename", metavar='<filename>',
        help="name gcode file to parse")
    parser.add_argument(
        "-p", "--path", default=os.path.abspath(os.path.dirname(__file__)),
        metavar='<path>',
        help="optional absolute path for file"
    )
    parser.add_argument(
        "-u", "--ufp", metavar="<ufp file>", default=None,
        help="optional path of ufp file to extract"
    )
    parser.add_argument(
        "-o", "--check-objects", dest='check_objects', action='store_true',
        help="process gcode file for exclude opbject functionality")

    args = parser.parse_args()

    # Call the original main function with our patched environment
    metadata.main(args.path, args.filename, args.ufp, args.check_objects)
