"""Fixed systemd commands. Never build shell commands from operator input."""

import os
import subprocess
import sys
from orchestrator.domain.models import OrchestratorError

API = "vpn-orchestrator.service"
TIMERS = ("vpn-orchestrator-recovery.timer", "vpn-orchestrator-leases.timer")
WORKERS = ("vpn-orchestrator-recovery.service", "vpn-orchestrator-leases.service")


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise OrchestratorError("service_command_failed", 503)


def control(action):
    if sys.platform != "linux":
        raise OrchestratorError("systemd_requires_linux", 422)
    if action not in ("start", "stop", "restart", "status"):
        raise OrchestratorError("unsupported_operation", 422)
    if action == "status":
        result = subprocess.run(
            ["systemctl", "is-active", API, *TIMERS], capture_output=True, text=True, timeout=15
        )
        return {
            "services": dict(zip((API, *TIMERS), result.stdout.splitlines())),
            "running": result.returncode == 0,
        }
    if os.geteuid() != 0:
        raise OrchestratorError("root_required", 403)
    if action in ("stop", "restart"):
        command(["systemctl", "stop", *TIMERS])
        command(["systemctl", "stop", *WORKERS])
        command(["systemctl", "stop", API])
    if action in ("start", "restart"):
        command(["systemctl", "start", API])
        command(["systemctl", "start", WORKERS[1]])
        command(["systemctl", "start", *TIMERS])
    return {
        "status": action,
        "notice": "Long stops expire node leases and stop managed VPN access."
        if action == "stop"
        else None,
    }
