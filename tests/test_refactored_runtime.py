"""Independent operation, durable inventory and deployment boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.bootstrap import agent_app, agent_service
from orchestrator.config.policy import RuntimePolicy
from orchestrator.config.settings import load_settings
from orchestrator.config.validation import validate_configuration
from orchestrator.domain.models import OrchestratorError
from orchestrator.interfaces.cli.initialize import initialize
from orchestrator.interfaces.cli import main as cli
from orchestrator.infrastructure import systemd
from orchestrator.application.management import manage_node
from test_node_switch import pair
from test_node_management import managed, payload


def test_empty_installation_starts_without_undercore_or_admin_secret(tmp_path, monkeypatch):
    result = initialize(tmp_path / "installation")
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", result["settings"])
    settings = load_settings(result["settings"])
    token = Path(settings.backend_token_file).read_text()
    client = TestClient(agent_app())
    assert client.get("/v1/health", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert (
        client.get(
            "/internal/admin/nodes", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )
    assert validate_configuration(result["settings"])["nodes"] == 0
    # Restart uses the initialized empty SQLite inventory, not a fabricated server.
    assert agent_service()[0].nodes == {}
    with pytest.raises(FileExistsError):
        initialize(tmp_path / "installation")


def test_live_inventory_does_not_depend_on_retired_bootstrap_secret(pair, tmp_path, monkeypatch):
    gateway, *_ = pair
    registry = managed(gateway, tmp_path)
    original = registry.records()
    settings = tmp_path / "settings.json"
    backend = tmp_path / "backend.token"
    backend.write_text("b" * 40)
    backend.chmod(0o600)
    settings.write_text(
        json.dumps(
            {
                "state_directory": str(gateway.store.path.parent),
                "backend_token_file": str(backend),
                "nodes": [json.loads(r["configuration"]) for r in original],
            }
        )
    )
    data = payload(registry, gateway)
    data["api_key"] = gateway.nodes["a"].api_key
    registry.save(gateway, data)
    Path(json.loads(original[0]["configuration"])["api_key_file"]).unlink()
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", str(settings))
    assert validate_configuration(settings)["nodes"] == 2
    restarted, _ = agent_service()
    assert restarted.nodes["a"].api_key == data["api_key"]


@pytest.mark.parametrize(
    "policy",
    [
        {"agents": {"response_timeout_seconds": 0}},
        {"agents": {"connect_timeout_seconds": True}},
        {"leases": {"fence_grace_seconds": 1}},
        {"leases": {"max_node_lease_seconds": 119}},
        {"recovery": {"cooldown_seconds": -1}},
        {"imaginary_retry_setting": 3},
    ],
)
def test_invalid_policy_is_rejected(policy):
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate(policy)


def test_local_cli_edits_inventory_without_admin_http(pair, tmp_path, monkeypatch, capsys):
    gateway, *_ = pair
    registry = managed(gateway, tmp_path)
    data = payload(registry, gateway)
    data["mode"] = "draining"
    file = tmp_path / "change.json"
    file.write_text(json.dumps(data))
    file.chmod(0o600)
    monkeypatch.setattr("orchestrator.bootstrap.agent_service", lambda: (gateway, "b" * 40))
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace())
    assert cli.main(["update", "server", data["id"], "--file", str(file)]) == 0
    assert gateway.nodes[data["id"]].mode == "draining"
    assert json.loads(capsys.readouterr().out)["revision"] == 2
    # Stale file cannot overwrite the new revision.
    assert cli.main(["update", "server", data["id"], "--file", str(file)]) == 1
    assert "node_revision_conflict" in capsys.readouterr().out


def test_local_add_cannot_overwrite_existing_node(pair, tmp_path):
    gateway, *_ = pair
    registry = managed(gateway, tmp_path)
    data = payload(registry, gateway)
    data["expected_revision"] = 0
    file = tmp_path / "add.json"
    file.write_text(json.dumps(data))
    file.chmod(0o600)
    with pytest.raises(OrchestratorError, match="node_revision_conflict"):
        manage_node(gateway, "add", file)


def test_systemd_lifecycle_controls_api_and_both_workers(monkeypatch):
    calls = []
    monkeypatch.setattr(systemd.sys, "platform", "linux")
    monkeypatch.setattr(systemd.os, "geteuid", lambda: 0)
    monkeypatch.setattr(systemd, "command", lambda args: calls.append(args))
    systemd.control("restart")
    assert calls == [
        ["systemctl", "stop", *systemd.TIMERS],
        ["systemctl", "stop", *systemd.WORKERS],
        ["systemctl", "stop", systemd.API],
        ["systemctl", "start", systemd.API],
        ["systemctl", "start", systemd.WORKERS[1]],
        ["systemctl", "start", *systemd.TIMERS],
    ]


def test_systemd_error_never_discloses_subprocess_output(monkeypatch):
    monkeypatch.setattr(
        systemd.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"PRIVATE", stderr=b"SECRET"),
    )
    with pytest.raises(OrchestratorError, match="service_command_failed") as error:
        systemd.command(["systemctl", "start", systemd.API])
    assert "SECRET" not in str(error.value)


def test_backup_preserves_private_inventory_and_credentials(tmp_path, monkeypatch):
    from orchestrator.interfaces.cli.backup import backup
    import sqlite3

    result = initialize(tmp_path / "installation")
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", result["settings"])
    gateway, _ = agent_service()
    destination = tmp_path / "backup"
    assert backup(result["settings"], destination)["status"] == "backed_up"
    with sqlite3.connect(destination / "assignments.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT initialized FROM registry_meta").fetchone()[0] == 1
    mapping = json.loads((destination / "restore-map.json").read_text())
    assert len(mapping) == 1
    for name, original in mapping.items():
        assert (destination / name).read_bytes() == Path(original).read_bytes()
    assert all(p.stat().st_mode & 0o077 == 0 for p in destination.iterdir())
    with pytest.raises(FileExistsError):
        backup(result["settings"], destination)


def test_policy_reaches_http_and_recovery_runtime(tmp_path, monkeypatch):
    from orchestrator.application.recovery import Recovery

    result = initialize(tmp_path / "installation")
    file = Path(result["settings"])
    settings = json.loads(file.read_text())
    settings["policy"]["recovery"]["cooldown_seconds"] = 900
    settings["policy"]["agents"]["connect_timeout_seconds"] = 6
    file.write_text(json.dumps(settings))
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", str(file))
    gateway, _ = agent_service()
    assert Recovery(gateway).cooldown == 900
    assert gateway.api.policy.connect_timeout_seconds == 6


def test_architecture_domain_has_no_application_or_io_dependencies():
    import ast

    for source in Path("orchestrator/domain").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    (
                        "orchestrator.application",
                        "orchestrator.infrastructure",
                        "orchestrator.interfaces",
                        "fastapi",
                        "httpx",
                        "sqlite3",
                    )
                )
