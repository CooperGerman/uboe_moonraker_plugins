#!/usr/bin/env python3
import sys
import os
import argparse
import logging
# Make it look like we are running in the file_manager directory
directory = os.path.dirname(os.path.abspath(__file__))
target_dir = directory + "/file_manager"
os.chdir(target_dir)
sys.path.insert(0, target_dir)

import metadata

# Define our custom class inheriting from PrusaSlicer
class SuperPrusaSlicer(metadata.PrusaSlicer):
    def parse_filament_weight_total(self) -> metadata.Optional[float]:
        # Try the original regex first (for "total filament used [g] = ...")
        total_weight = metadata.regex_find_float(
            r"total\sfilament\sused\s\[g\]\s=\s(%F)",
            self.footer_data
        )
        if total_weight is not None:
            return total_weight

        else :
            return metadata.regex_find_floats(
                r"filament\sused\s\[g\]\s=\s(%F)",
                self.footer_data
            )

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
