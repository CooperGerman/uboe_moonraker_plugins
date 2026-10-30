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
## File:        update.sh
## Author(s):   Y.L.P.
## Description: Update script to pull latest changes and run install.sh for Uboe Moonraker plugins
###############################################################################

set -e

cd "${HOME}/uboe_moonraker_plugins"
git pull --rebase --autostash
./install.sh

echo "Uboe Moonraker plugins updated successfully!"
