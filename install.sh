#!/bin/bash
###############################################################################
##
## 88        88 88
## 88        88 88
## 88        88 88
## 88        88 88,dPPYba,   ,adPPYba,   ,adPPYba,
## 88        88 88P'    "8a a8"     "8a a8P_____88
## 88        88 88       d8 8b       d8 8PP"""""""
## Y8a.    .a8P 88b,   ,a8" "8a,   ,a8" "8b,   ,aa
##  `"Y8888Y"'  `"8Ybbd8"'   `"YbbdP"'   `"Ybbd8"'
##
###############################################################################
## © Copyright 2023 Uboe S.A.S
## File:        install.sh
## Author(s):   Y.L.P.
## Description: Installation script to symlink Python files to Moonraker components
###############################################################################

set -e

MOONRAKER_DIR="${HOME}/moonraker/moonraker"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Linking .py files from ${SCRIPT_DIR} to ${MOONRAKER_DIR}/components"

# Find and symlink all .py files in the script directory (not subdirectories)
for f in "${SCRIPT_DIR}"/*.py ; do
	if [ -f "$f" ]; then
		base=$(basename "$f")
		rm -f "${MOONRAKER_DIR}/components/${base}"
		ln -sf "${SCRIPT_DIR}/${base}" "${MOONRAKER_DIR}/components/${base}"
		echo "Linked: ${base}"
		# Add the symlinked file to git's local exclude list to avoid it showing as untracked
		exclude_entry="moonraker/components/${base}"
		if ! grep -qF "${exclude_entry}" "${HOME}/moonraker/.git/info/exclude"; then
			echo "${exclude_entry}" >> "${HOME}/moonraker/.git/info/exclude"
			echo "Added to git exclude: ${exclude_entry}"
		fi
	fi
done
# rename original metadata.py file in moonraker/components to metadata_orig.py
if [ -f "${MOONRAKER_DIR}/components/file_manager/metadata.py" ]; then
	mv "${MOONRAKER_DIR}/components/file_manager/metadata.py" "${MOONRAKER_DIR}/components/file_manager/metadata_orig.py"
	echo "Renamed original metadata.py to metadata_orig.py"
	rm -f "${MOONRAKER_DIR}/components/file_manager/metadata.py"
	ln -sf "${SCRIPT_DIR}/file_manager/metadata.py" "${MOONRAKER_DIR}/components/file_manager/metadata.py"
	exclude_entry="moonraker/components/file_manager/metadata_orig.py"
	if ! grep -qF "${exclude_entry}" "${HOME}/moonraker/.git/info/exclude"; then
		echo "${exclude_entry}" >> "${HOME}/moonraker/.git/info/exclude"
		echo "Added to git exclude: ${exclude_entry}"
	fi
fi

# activate moonraker venv and install requirements.txt
if [ -f "${SCRIPT_DIR}/requirements.txt" ]; then
    "${HOME}/moonraker-env/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"
fi


echo "Installation complete!"
