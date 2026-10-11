"""CLI and panel share audited commands, revision checks and existing VPN lifecycle."""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from test_node_switch import pair
from test_node_management import managed, payload
from orchestrator.application.administration import Administration
from orchestrator.domain.administration import Command
from orchestrator.domain.models import OrchestratorError
from orchestrator.config.settings import Settings
from orchestrator.infrastructure.sqlite.administration import OperatorAudit
from orchestrator.infrastructure.policy_editor import PolicyFile
from orchestrator.infrastructure.sqlite.panel import PanelJournal
from orchestrator.infrastructure.events import EventTail
from orchestrator.interfaces.http.panel import create_panel_app
from orchestrator.interfaces.cli import main as cli

ORIGIN = "http://127.0.0.1:8793"
TOKEN = "p" * 48
AUTH = {"Authorization": "Bearer " + TOKEN, "Origin": ORIGIN}


@pytest.fixture
def operator(pair, tmp_path, monkeypatch):
    gateway, *_ = pair
    managed(gateway, tmp_path)
    monkeypatch.setattr(gateway, "close", lambda: None)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "state_directory": str(tmp_path),
                "backend_token_file": str(tmp_path / "backend.token"),
            }
        )
    )
    settings.chmod(0o600)
    admin = Administration(
        lambda: gateway, OperatorAudit(tmp_path), PolicyFile(settings, Settings.model_validate_json)
    )
    return admin, gateway, settings


def command(data, operation_id="change-1"):
    return Command(operation_id=operation_id, action="node.save", target=data["id"], data=data)


def test_cli_and_panel_share_revision_and_audit(operator, monkeypatch, tmp_path, capsys):
    admin, gateway, _ = operator
    data = payload(gateway.registry, gateway)
    data["mode"] = "draining"
    file = tmp_path / "command.json"
    file.write_text(command(data).model_dump_json())
    file.chmod(0o600)
    monkeypatch.setattr("orchestrator.bootstrap.administration_service", lambda: admin)
    assert cli.main(["operator", "execute", "--file", str(file)]) == 0
    assert gateway.nodes["a"].mode == "draining"
    assert admin.journal.recent()[0]["actor"] == "cli"
    assert "api_key" not in capsys.readouterr().out
    # Exact repeat returns the recorded outcome without reapplying the stale revision.
    assert admin.execute(command(data), "panel")["revision"] == 2
    assert len(admin.journal.recent()) == 1
    with pytest.raises(OrchestratorError, match="revision_conflict"):
        admin.execute(command(data, "change-2"), "panel")
    assert admin.journal.recent()[0]["outcome"] == "failed"
    assert gateway.nodes["a"].mode == "draining"


def test_audit_failure_prevents_mutation(operator, monkeypatch):
    admin, gateway, _ = operator
    data = payload(gateway.registry, gateway)
    data["mode"] = "disabled"

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(admin.journal, "begin", fail)
    with pytest.raises(OSError):
        admin.execute(command(data), "panel")
    assert gateway.nodes["a"].mode == "active"


def test_uncertain_intent_and_mismatched_retries_are_not_executed(operator):
    admin, gateway, _ = operator
    data = payload(gateway.registry, gateway)
    data["mode"] = "disabled"
    admin.journal.begin(command(data).model_dump(), "panel")
    with pytest.raises(OrchestratorError, match="review_required"):
        admin.execute(command(data), "panel")
    data["mode"] = "draining"
    with pytest.raises(OrchestratorError, match="operation_conflict"):
        admin.execute(command(data), "panel")
    assert gateway.nodes["a"].mode == "active"


def test_policy_validation_and_optimistic_atomic_save(operator):
    admin, _, settings = operator
    current = admin.policies.read()
    old = settings.read_bytes()
    invalid = {**current["policy"], "agents": {"operation_timeout_seconds": 0}}
    with pytest.raises(OrchestratorError, match="invalid_policy"):
        admin.execute(
            Command(
                operation_id="bad-policy",
                action="policy.set",
                target="runtime",
                data={"expected_revision": current["revision"], "policy": invalid},
            ),
            "panel",
        )
    assert settings.read_bytes() == old
    current["policy"]["recovery"]["cooldown_seconds"] = 2400
    request = Command(
        operation_id="policy-1",
        action="policy.set",
        target="runtime",
        data={"expected_revision": current["revision"], "policy": current["policy"]},
    )
    assert admin.execute(request, "panel")["status"] == "restart_required"
    assert admin.policies.read()["policy"]["recovery"]["cooldown_seconds"] == 2400
    assert settings.stat().st_mode & 0o777 == 0o600
    with pytest.raises(OrchestratorError, match="revision_conflict"):
        admin.execute(request.model_copy(update={"operation_id": "policy-2"}), "cli")


def test_node_mode_and_revoke_reuse_lifecycle(operator):
    admin, gateway, _ = operator
    result = admin.execute(
        Command(
            operation_id="mode-1",
            action="node.mode",
            target="a",
            data={"expected_revision": 1, "mode": "draining", "capacity": 12},
        ),
        "panel",
    )
    assert result["revision"] == 2 and gateway.nodes["a"].capacity == 12
    row = gateway.store.rows()[0]
    result = admin.execute(
        Command(operation_id="revoke-1", action="connection.disable", target=row["client_id"]),
        "panel",
    )
    assert result["status"] == "disable_requested"
    assert gateway.client(row["client_id"])["status"] == "disabled"
    assert admin.journal.recent()[0]["outcome"] == "succeeded"


def test_commands_are_opt_in_authenticated_and_do_not_echo_secrets(operator):
    admin, gateway, _ = operator
    reader = PanelJournal(gateway.store.path.parent)
    events = EventTail(gateway.store.path.parent)
    readonly = TestClient(create_panel_app(reader, events, TOKEN, ORIGIN), base_url=ORIGIN)
    assert readonly.post("/admin/v1/commands", headers=AUTH, json={}).status_code == 404
    assert not readonly.get("/admin/v1/capabilities", headers=AUTH).json()["management"]
    client = TestClient(create_panel_app(reader, events, TOKEN, ORIGIN, admin), base_url=ORIGIN)
    assert client.get("/admin/v1/capabilities", headers=AUTH).json()["management"]
    assert client.post("/admin/v1/commands", json={}, headers={"Origin": ORIGIN}).status_code == 401
    assert (
        client.post(
            "/admin/v1/commands", json={}, headers={"Authorization": AUTH["Authorization"]}
        ).status_code
        == 403
    )
    result = client.post("/admin/v1/commands", json={"api_key": "sensitive-fixture"}, headers=AUTH)
    assert result.status_code == 422 and "sensitive-fixture" not in result.text
    assert client.post("/admin/v1/commands", content=b"x" * 16385, headers=AUTH).status_code == 413
    data = payload(gateway.registry, gateway)
    data["api_key"] = gateway.nodes["a"].api_key
    result = client.post("/admin/v1/commands", json=command(data).model_dump(), headers=AUTH)
    assert result.status_code == 200
    result = client.get("/admin/v1/audit", headers=AUTH)
    assert data["api_key"] not in result.text and "api_key" not in result.text
    assert data["api_key"].encode() not in admin.journal.path.read_bytes()
    assert admin.journal.path.stat().st_mode & 0o777 == 0o600


def test_lost_audit_completion_requires_review_instead_of_replay(operator, monkeypatch):
    admin, gateway, _ = operator
    data = payload(gateway.registry, gateway)
    data["mode"] = "draining"

    def fail(*args):
        raise OSError("disk full after mutation")

    monkeypatch.setattr(admin.journal, "finish", fail)
    with pytest.raises(OSError):
        admin.execute(command(data), "panel")
    assert gateway.nodes["a"].mode == "draining"
    assert admin.journal.recent()[0]["outcome"] == "started"
    with pytest.raises(OrchestratorError, match="review_required"):
        admin.execute(command(data), "panel")


def test_backup_restores_operator_journal(tmp_path):
    from orchestrator.interfaces.cli.initialize import initialize
    from orchestrator.interfaces.cli.backup import backup, restore_backup
    from orchestrator.infrastructure.sqlite.assignments import Assignments

    installed = initialize(tmp_path / "config")
    settings = Settings.model_validate_json(Path(installed["settings"]).read_bytes())
    Assignments(settings.state_directory)
    audit = OperatorAudit(settings.state_directory)
    audit.begin(
        {"operation_id": "interrupted", "action": "node.probe", "target": "lab", "data": {}}, "cli"
    )
    backup(installed["settings"], tmp_path / "backup")
    restored = restore_backup(tmp_path / "backup", tmp_path / "restored")
    state = Settings.model_validate_json(Path(restored["settings"]).read_bytes()).state_directory
    assert OperatorAudit(state).recent()[0]["outcome"] == "started"
    assert (Path(state) / ".restore-review-required").exists()


def test_new_node_uses_existing_agent_identity_check_and_private_credential(operator):
    admin, gateway, _ = operator
    data = payload(gateway.registry, gateway, "b")
    data.update(expected_revision=0, api_key=gateway.nodes["b"].api_key, mode="draining")
    # Remove only the synthetic registry entry: its test agent remains available.
    with gateway.store.db() as db:
        db.execute("DELETE FROM node_registry WHERE id='b'")
    assert admin.execute(command(data, "add-b"), "panel")["revision"] == 1
    assert gateway.nodes["b"].mode == "draining"
    assert gateway.nodes["b"].server_id == "b-identity"
    assert "api_key" not in json.dumps(admin.journal.recent())


def test_concurrent_operator_changes_preserve_revision(operator):
    from concurrent.futures import ThreadPoolExecutor

    admin, gateway, _ = operator
    data = payload(gateway.registry, gateway)
    data["mode"] = "draining"

    def apply(number):
        try:
            return admin.execute(command(data, f"parallel-{number}"), "panel")["revision"]
        except OrchestratorError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, [1, 2]))
    assert sorted(map(str, results)) == ["2", "node_revision_conflict"]
    assert len(admin.journal.recent()) == 2
