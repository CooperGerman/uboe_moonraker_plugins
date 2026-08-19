# Spool Change Estimator Plugin
# Moonraker plugin to perform spool change estimations during print
#
# This plugin updates the spool change estimation data in the database and provides an API to retrieve it.
from __future__ import annotations
import json
import logging
import asyncio
from logging import config, error
import os
from typing import TYPE_CHECKING, Dict, Any, Optional, List
from packaging.version import Version
from datetime import datetime, time, timedelta
from .additional_pre_print_checks import AdditionalPrePrintChecks
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

class SpoolChangeEstimator:
	def __init__(self, config: ConfigHelper):
		self.config = config
		self.server = config.get_server()

	async def component_init(self) -> None:
		"""Initialize component"""
		try:
			self.additional_pre_print_checks = self.server.lookup_component("additional_pre_print_checks")
		except Exception as e:
			raise self.config.error(f"[{self.config.get_name()}]: {e}")
		logging.info("Spool Change Estimator component initialized")


	async def _log_to_console(self, msg: str, severity: str = "info", reason: str = "Pre-Print Check Failed") -> None:
		self.additional_pre_print_checks._log_to_console(msg, severity, reason)

def load_component(config: ConfigHelper) -> SpoolChangeEstimator:
	return SpoolChangeEstimator(config)