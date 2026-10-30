# Spool Change Estimator Plugin
# Moonraker plugin to perform spool change estimations during print
#
# This plugin updates the spool change estimation data in the database and provides an API to retrieve it.
from __future__ import annotations
import json
import logging
import asyncio
import shlex
from logging import config, error
import os
from typing import TYPE_CHECKING, Dict, Any, Optional, List, Callable
from packaging.version import Version
from datetime import datetime, time, timedelta
from .additional_pre_print_checks import AdditionalPrePrintChecks
from ..common import JobEvent
from ..components.uboe_metadata import ExtrusionPoints

if TYPE_CHECKING:
	from ..components.spoolman import SpoolManager
	from ..components.mmu_server import MmuServer
	from ..confighelper import ConfigHelper, ConfigError
	from ..components.klippy_apis import KlippyAPI as APIComp
	from ..components.klippy_connection import KlippyConnection
	from ..components.shell_command import ShellCommandFactory
	from ..components.file_manager.file_manager import FileManager
	from ..components.file_manager.metadata import MetadataStorage
	from ..components.database import MoonrakerDatabase

class UboeHasUpdates:
	def __init__(self, config: ConfigHelper):
		self.config = config
		self.server = config.get_server()
		# Accept repos separated by newlines and/or commas, optionally quoted
		raw_repos = config.get("repos", "")
		self.repos : List[str] = [
			os.path.expanduser(r.strip().strip("\"'").strip())
			for r in raw_repos.replace(",", "\n").splitlines()
			if r.strip().strip("\"'").strip()
		]
		self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
		self.shell_command: ShellCommandFactory

		self.server.register_remote_method(
			"uboe_has_updates",
			self.cmd_UBOE_HAS_UPDATES
		)
		self.server.register_remote_method(
			"uboe_update_all",
			self.cmd_UBOE_UPDATE_ALL
		)
		self.outdated_repos: List[str] = []
		self.update_lock = asyncio.Lock()

	async def _handle_klippy_ready(self) -> None:
		pass

	async def cmd_UBOE_HAS_UPDATES(self) -> None:
		"""
  		Iterate over all repos and check if they have available updates.
		If some are found display them in a popup window prompting to resume or update them.
  		"""
		# Iterate over all repos and check if they have available updates
		updates_available = []
		check_failures = []
		self.outdated_repos = updates_available
		for repo in self.repos:
			try:
				if await self.check_for_updates(repo):
					updates_available.append(repo)
			except self.shell_command.error as e:
				logging.error("Unable to check repository %s for updates: %s", repo, e)
				check_failures.append(repo)

		if updates_available or check_failures:
			message = ""
			if updates_available:
				message = "The following repos have updates available:\n" + "\n".join(updates_available)
			if check_failures:
				if message:
					message += "\n\n"
				message += "Unable to check these repos:\n" + "\n".join(check_failures)
			await self._build_custom_popup(
				title="Updates Available" if updates_available else "Update Check Failed",
				message=message,
				custom_msg=(
					"Please update them before resuming the print."
					if updates_available else
					"Check the Moonraker log for details."
				),
				buttons=[
					{"label": "UPDATE ALL", "action": "UBOE_UPDATE_ALL", "severity": "warning"},
					{"label": "RESUME", "action": "RESUME", "severity": "primary"},
				] if updates_available else None
			)
		else:
			msg: List[str] = []
			for repo in self.repos:
				# get repo basename
				repo_basename = os.path.basename(repo)
				msg.append(f"No updates available for {repo_basename}.")
			await self._log_to_console(
				msg="\n".join(msg),
				severity="success",
				reason="Update Check",
				popup=False
			)

	async def cmd_UBOE_UPDATE_ALL(self) -> None:
		"""
		Update every repo found outdated by the last check, then restart
		klipper and moonraker like the builtin update_manager does.
		"""
		if self.update_lock.locked():
			return
		async with self.update_lock:
			repos = list(self.outdated_repos)
			if not repos:
				for repo in self.repos:
					try:
						if await self.check_for_updates(repo):
							repos.append(repo)
					except self.shell_command.error as e:
						logging.error("Unable to check repository %s for updates: %s", repo, e)
			updated = []
			failed = []
			for repo in repos:
				try:
					await self.update_repo(repo)
					updated.append(repo)
				except self.shell_command.error as e:
					logging.error("Unable to update repository %s: %s", repo, e)
					failed.append(repo)
			self.outdated_repos = [r for r in self.outdated_repos if r not in updated]
			if failed:
				await self._build_custom_popup(
					title="Update Failed",
					message="Unable to update these repos:\n" + "\n".join(failed),
					custom_msg="Check the Moonraker log for details."
				)
				return
			if updated:
				await self._restart_services()

	async def update_repo(self, repo: str) -> None:
		update_script = os.path.join(repo, "update.sh")
		if os.path.isfile(update_script):
			await self.shell_command.exec_cmd(
				f"bash {shlex.quote(update_script)}",
				timeout=600., attempts=1, log_stderr=True, cwd=repo
			)
			return
		git_command = f"git -C {shlex.quote(repo)}"
		dirty = await self.shell_command.exec_cmd(
			f"{git_command} status --porcelain --untracked-files=no",
			timeout=20., attempts=1, log_stderr=True
		)
		if dirty.strip():
			raise self.shell_command.error(
				f"Repo {repo} has local modifications, update aborted", None
			)
		await self.shell_command.exec_cmd(
			f"{git_command} pull --ff-only --progress",
			timeout=300., attempts=1, log_stderr=True
		)

	async def _restart_services(self) -> None:
		machine = self.server.lookup_component("machine")
		kconn = self.server.lookup_component("klippy_connection")
		await machine.do_service_action("restart", kconn.unit_name)
		# Launched async so this request can return before the server restarts
		machine.restart_moonraker_service()

	async def check_for_updates(self, repo: str) -> bool:
		git_command = f"git -C {shlex.quote(repo)}"
		await self.shell_command.exec_cmd(
			f"{git_command} fetch --prune",
			timeout=300.,
			attempts=1,
			log_stderr=True
		)
		commits = await self.shell_command.exec_cmd(
			f"{git_command} rev-parse HEAD @{{upstream}}",
			timeout=20.,
			attempts=1,
			log_stderr=True
		)
		current_commit, upstream_commit = commits.splitlines()
		return current_commit != upstream_commit


	async def component_init(self) -> None:
		"""Initialize component"""
		try:
			self.shell_command = self.server.lookup_component("shell_command")
			self.additional_pre_print_checks : AdditionalPrePrintChecks = self.server.lookup_component("additional_pre_print_checks")
		except Exception as e:
			raise self.config.error(f"[{self.config.get_name()}]: {e}")

	async def _log_to_console(self, msg: str = "Empty message", severity: str = "info", reason: str = "Spool Change Estimate", popup: bool = False) -> None:
		await self.additional_pre_print_checks._log_to_console(msg, severity, reason, popup=popup)

	async def _build_custom_popup(self, title: str, message: str, custom_msg: str = "", buttons: Optional[List[Dict[str, Any]]] = None) -> None:
		'''
		Create a custom popup window with the given title, message, and optional buttons. see example below
		{% set message = params.MSG|string %}
		{% set reason = params.REASON|string %}
		RESPOND TYPE=command MSG="action:prompt_begin Uboe Error Notice"
		{% for line in message.split('\\n') %}
			RESPOND TYPE=command MSG='{"action:prompt_text %s" % line}'
		{% endfor %}
		{% if not custom_msg == "" %}
			{% for line in custom_msg %}
				RESPOND TYPE=command MSG='{"action:prompt_text %s" % line}'
			{% endfor %}
		{% else %}
			RESPOND TYPE=command MSG="action:prompt_text After fixing, call RESUME to continue printing"
		{% endif %}
		RESPOND TYPE=command MSG="action:prompt_button_group_start"
		RESPOND TYPE=command MSG="action:prompt_button ABORT|CANCEL_PRINT|error"
		RESPOND TYPE=command MSG="action:prompt_button RESUME|RESUME|warning"
		RESPOND TYPE=command MSG="action:prompt_button_group_end"
		RESPOND TYPE=command MSG="action:prompt_show"
		{% set custom_msg = "" %}
  		'''
		# Create the G-code commands used to display the custom popup.
		commands = []
		commands.append(f'RESPOND TYPE=command MSG="action:prompt_begin {title}"')
		for line in message.split('\n'):
			commands.append(f'RESPOND TYPE=command MSG="action:prompt_text {line}"')
		if not custom_msg == "":
			for line in custom_msg.split('\n'):
				commands.append(f'RESPOND TYPE=command MSG="action:prompt_text {line}"')
		else:
			commands.append('RESPOND TYPE=command MSG="action:prompt_text After fixing, call RESUME to continue printing"')
		if buttons:
			commands.append('RESPOND TYPE=command MSG="action:prompt_button_group_start"')
			for button in buttons or []:
				commands.append(f'RESPOND TYPE=command MSG="action:prompt_button {button["label"]}|{button["action"]}|{button["severity"]}"')
			commands.append('RESPOND TYPE=command MSG="action:prompt_button_group_end"')

		commands.append('RESPOND TYPE=command MSG="action:prompt_show"')
		for cmd in commands:
			await self.klippy_apis.run_gcode(cmd)

def load_component(config: ConfigHelper) -> UboeHasUpdates:
	return UboeHasUpdates(config)