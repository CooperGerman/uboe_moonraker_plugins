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
## File:        Makefile
## Author(s):   Y.L.P.
## Description: Automation
###############################################################################
SHELL=bash -e

MOONRAKER_DIR := /home/$(USER)/moonraker/moonraker
CURDIR := $(shell pwd)

default: all

all : setup

setup: symlinks

symlinks:
	$(info Setting up environment)
	mkdir -p $(MOONRAKER_DIR)/extras
	@echo "Linking .py files from $(CURDIR) to $(MOONRAKER_DIR)/extras"
	@for f in $(CURDIR)/*.py ; do \
		base=$$(basename $$f) ; \
		rm -f $(MOONRAKER_DIR)/extras/$$base ; \
		ln -sf $(CURDIR)/$$base $(MOONRAKER_DIR)/extras/$$base ; \
	done


REQUIRED_BINS :=
check_bins:
	$(info Looking for binaries: `$(REQUIRED_BINS)` in PATH)
	$(foreach bin,$(REQUIRED_BINS),\
		$(if $(shell command -v $(bin) 2> /dev/null),\
			$(info Found `$(bin)`),\
			$(info Error: Please install `$(bin)` or add it to PATH if already installed)))
env:
	mkdir -p ./work
	mkdir -p ./result

clean:
	rm -rf ./work
	rm -f ./result

super_clean: clean
	rm -rf ./env

# ./pip.sh check requirements.txt
help :
	@echo "make help                : prints this help"




