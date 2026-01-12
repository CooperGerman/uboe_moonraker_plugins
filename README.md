# Useful plugins used by uboe
This repo contains useful plugins used by uboe. Some of them are comfort plugins, some of them are useful for debugging and some of them are used to extend the functionality of Moonraker.

# Installation
1. Clone the repo preferably in your home directory:
```bash
cd ~
git clone https://github.com/CooperGerman/uboe_moonraker_plugins.git
cd uboe_moonraker_plugins
make setup
```
This will create symlinks for each plugin in the moonraker/moonraker/components folder in order for moonraker to "see" the plugins.

**You will need to restart the moonraker service for the changes to take effect.**

# Plugins

## Additional Pre-Print Checks
**Category:** Comfort / Safety
**File:** `additional_pre_print_checks.py`

A comprehensive pre-print validation system that checks filament compatibility and availability before starting a print job.

### Features
- ✅ **Weight Check** - Verifies sufficient filament weight against gcode requirements
- ✅ **Material Check** - Validates spool material matches sliced material
- ✅ **Filament Name Check** - Ensures correct filament profile is loaded
- ✅ **Configurable Severity** - Set checks to error (block), warning, or info levels
- ✅ **Efficient Caching** - Single spool data fetch per check session

### Quick Setup
Add to `moonraker.conf`:
```ini
[additional_pre_print_checks]
enable_weight_check: True
enable_material_check: True
enable_filament_name_check: False
weight_margin_grams: 5.0
material_mismatch_severity: warning
filament_name_mismatch_severity: info
```

Add to your `START_PRINT` macro:
```gcode
[gcode_macro START_PRINT]
gcode:
    # Run pre-print checks (will pause print if checks fail)
    { action_call_remote_method("pre_print_checks") }

    # Continue with normal print start sequence...
    G28  # Home
    # ... rest of your start sequence
```

### Requirements
- Moonraker with Spoolman component configured
- Active spool set in Spoolman
- Gcode files with slicer metadata (PrusaSlicer, OrcaSlicer, Bambu Studio, etc.)

📖 **[Full Documentation](PRE_PRINT_CHECKS_README.md)**

---

## Plugin Summary Table

| Plugin Name | Category | Description |
| ----------- | -------- | ----------- |
| additional_pre_print_checks | Comfort / Safety | Comprehensive pre-print validation system with weight, material, and filament name checks |
