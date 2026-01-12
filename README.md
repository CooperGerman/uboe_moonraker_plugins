# Useful plugins used by uboe
This repo contains useful plugins used by uboe. Some of them are comfort plugins, some of them are useful for debugging and some of them are used to extend the functionality of Moonraker.
# Installation
1. Clone the repo where preferrably in your home directory.:
```bash
cd ~
git clone https://github.com/CooperGerman/uboe_moonraker_plugins.git
cd uboe_moonraker_plugins
make
```
This will create symlinks for each plugin in the moonraker/moonraker/extras folder in order for moonraker to "seee" the plugins.

**You will need to restart the moonraker service for the changes to take effect.**

# Plugins

The table below lists the plugins and their functionality.
| Plugin Name | feature | Description |
| ----------- | ------- | ----------- |
|  |  |  |
| aadditional_pre_print_checks | Comfort | Adds additional pre-print checks to moonraker especially remaining filament weight check against print usage. |
