import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.amnezia import AmneziaAPI
from orchestrator.http import create_app
from orchestrator.models import ConnectionIntent, Node, PilotError
from orchestrator.service import Orchestrator
from orchestrator.store import Store


TOKEN = "backend-test-token-" * 3
CONFIG = "vpn://SYNTHETIC_PRIVATE_CONFIGURATION"


class FakeNodes:
    def __init__(self):
        self.created = []
        self.loads = {"a": 20, "b": 10}
        self.max_peers = 100
        self.down = set()
        self.post_failure = None
        self.protocols = ["amneziawg3"]
        self.wrong_identity = False
        self.started = threading.Event()
        self.resume = None

    def __call__(self, request):
        name = request.url.host.split(".")[0]
        assert request.headers["x-api-key"] == "test" * 8
        if request.url.path == "/server":
            if name in self.down:
                raise httpx.ConnectError("synthetic-node-secret", request=request)
            return httpx.Response(200, json={
                "id": "wrong-node" if self.wrong_identity else name,
                "protocols": self.protocols, "totalPeers": self.loads[name], "maxPeers": self.max_peers,
            })
        assert request.url.path == "/clients" and request.method == "POST"
        payload = json.loads(request.content)
        assert payload["protocol"] == "amneziawg3"
        assert payload["clientName"].startswith("uc_") and len(payload["clientName"]) <= 64
        self.created.append((name, payload))
        self.started.set()
        if self.resume:
            assert self.resume.wait(timeout=5)
        if self.post_failure == "timeout":
            raise httpx.ReadTimeout("synthetic-node-secret", request=request)
        if self.post_failure == "500":
            return httpx.Response(500, text="synthetic-node-secret")
        if self.post_failure == "malformed":
            return httpx.Response(200, json={"client": {"config": CONFIG}})
        if self.post_failure == "wrong_protocol":
            return httpx.Response(200, json={"client": {"id": "p", "config": CONFIG, "protocol": "xray"}})
        return httpx.Response(200, json={"client": {
            "id": "peer_" + name, "protocol": "amneziawg3", "config": CONFIG,
        }})


@pytest.fixture
def rig(tmp_path):
    directory = tmp_path / "state"
    key = Fernet.generate_key()
    store = Store(directory, key)
    nodes = [Node(name, f"https://{name}.invalid", name, "nl", 100, "test" * 8) for name in ("a", "b")]
    fake = FakeNodes()
    api = AmneziaAPI(httpx.MockTransport(fake))
    return Orchestrator(nodes, store, api), fake, nodes, key, directory


def intent(**changes):
    return ConnectionIntent(**({
        "owner_id": "owner_1", "device_id": "device_1", "expires_at": int(time.time()) + 3600,
    } | changes))


def assert_error(code, function):
    with pytest.raises(PilotError) as error:
        function()
    assert error.value.code == code


def test_repeat_after_restart_returns_same_config_and_revision(rig):
    service, fake, nodes, key, directory = rig
    grant = intent()
    first = service.connect(grant)
    assert first["node_id"] == "b"
    assert first["configuration"] == CONFIG
    assert fake.created[0][1]["expiresAt"] == grant.expires_at
    fake.down = {"a", "b"}
    restarted = Orchestrator(nodes, Store(directory, key), service.api)
    # Cached connection stays bound even when its management API is down.
    assert restarted.connect(grant) == first
    assert len(fake.created) == 1


def test_owner_and_expiry_conflicts_cannot_provision_or_export(rig):
    service, fake, *_ = rig
    grant = intent()
    service.connect(grant)
    assert_error("device_owner_conflict", lambda: service.connect(grant.model_copy(update={"owner_id": "other"})))
    assert_error("expiry_change_not_implemented", lambda: service.connect(grant.model_copy(update={"expires_at": grant.expires_at + 1})))
    assert len(fake.created) == 1


def test_expired_grant_never_provisions(rig):
    service, fake, *_ = rig
    assert_error("grant_expired", lambda: service.connect(intent(expires_at=1)))
    assert not fake.created


def test_region_is_a_selection_constraint_not_a_migration_request(rig):
    service, fake, *_ = rig
    grant = intent()
    first = service.connect(grant)
    assert service.connect(grant.model_copy(update={"region": "de"})) == first
    assert_error("no_eligible_node", lambda: service.connect(intent(device_id="new", region="de")))
    assert len(fake.created) == 1


def test_unavailable_node_is_excluded_before_first_create(rig):
    service, fake, *_ = rig
    fake.down = {"b"}
    assert service.connect(intent())["node_id"] == "a"


@pytest.mark.parametrize("reason", ["protocol", "identity", "full", "all_down"])
def test_no_eligible_node_does_not_create(rig, reason):
    service, fake, *_ = rig
    if reason == "protocol":
        fake.protocols = ["xray"]
    elif reason == "identity":
        fake.wrong_identity = True
    elif reason == "full":
        fake.max_peers = 1
    else:
        fake.down = {"a", "b"}
    assert_error("no_eligible_node", lambda: service.connect(intent()))
    assert not fake.created


def test_draining_retains_existing_but_disabled_blocks_export(rig):
    service, fake, nodes, *_ = rig
    grant = intent()
    first = service.connect(grant)
    draining = [replace(node, mode="draining") if node.id == "b" else node for node in nodes]
    service = Orchestrator(draining, service.store, service.api)
    assert service.connect(grant) == first
    assert service.connect(intent(device_id="new"))["node_id"] == "a"
    service.nodes["b"] = replace(nodes[1], mode="disabled")
    assert_error("assigned_node_disabled", lambda: service.connect(grant))
    assert len(fake.created) == 2


@pytest.mark.parametrize("failure", ["timeout", "500", "malformed", "wrong_protocol"])
def test_ambiguous_creation_is_durable_and_never_retried_or_failed_over(rig, failure):
    service, fake, nodes, key, directory = rig
    fake.post_failure = failure
    grant = intent()
    assert_error("provisioning_uncertain", lambda: service.connect(grant))
    assert service.store.get(grant.device_id)["state"] == "uncertain"
    restarted = Orchestrator(nodes, Store(directory, key), service.api)
    fake.post_failure = None
    assert_error("provisioning_uncertain", lambda: restarted.connect(grant))
    assert len(fake.created) == 1
    # Quarantine the uncertain node; a DIFFERENT paid device may use another.
    assert restarted.connect(intent(device_id="another"))["node_id"] == "a"


def test_crash_after_durable_reservation_does_not_trigger_another_post(rig):
    service, fake, nodes, key, directory = rig
    grant = intent()
    service.store.reserve(grant, [service.api.observe(nodes[0])])
    restarted = Orchestrator(nodes, Store(directory, key), service.api)
    assert_error("provisioning_uncertain", lambda: restarted.connect(grant))
    assert not fake.created


def test_disk_failure_after_remote_success_does_not_repeat_create(rig, monkeypatch):
    service, fake, *_ = rig
    grant = intent()

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(service.store, "finish", fail)
    assert_error("provisioning_uncertain", lambda: service.connect(grant))
    assert_error("provisioning_uncertain", lambda: service.connect(grant))
    assert len(fake.created) == 1


def test_competing_workers_share_one_durable_reservation(rig):
    service, fake, nodes, key, directory = rig
    second_worker = Orchestrator(nodes, Store(directory, key), service.api)
    fake.resume = threading.Event()
    grant = intent()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.connect, grant)
        assert fake.started.wait(timeout=3)
        try:
            assert_error("provisioning_uncertain", lambda: second_worker.connect(grant))
        finally:
            fake.resume.set()
        result = first.result(timeout=5)
    assert second_worker.connect(grant) == result
    assert len(fake.created) == 1


def test_stale_health_cannot_oversubscribe_capacity(rig):
    service, fake, nodes, *_ = rig
    one_slot_node = replace(nodes[0], capacity=1)
    fake.loads["a"] = 0
    observation = service.api.observe(one_slot_node)
    row, _ = service.store.reserve(intent(), [observation])
    service.store.finish(row, service.api.create(one_slot_node, row["operation_id"], row["expires_at"]))
    assert_error("no_eligible_node", lambda: service.store.reserve(intent(device_id="new"), [observation]))


def test_ciphertext_is_private_and_bound_to_assignment(rig):
    service, _, _, _, directory = rig
    grant = intent()
    service.connect(grant)
    service.connect(intent(device_id="other_device"))
    database = directory / "connections.sqlite3"
    assert CONFIG.encode() not in database.read_bytes()
    assert b"peer_b" not in database.read_bytes()
    assert database.stat().st_mode & 0o077 == 0
    assert directory.stat().st_mode & 0o077 == 0
    with sqlite3.connect(database) as db:
        db.execute("UPDATE connections SET ciphertext = (SELECT ciphertext FROM connections WHERE device_id='other_device') WHERE device_id='device_1'")
    assert_error("stored_configuration_unavailable", lambda: service.connect(grant))


def test_missing_encryption_key_never_creates_replacement(rig):
    service, fake, nodes, _, directory = rig
    grant = intent()
    service.connect(grant)
    restarted = Orchestrator(nodes, Store(directory, Fernet.generate_key()), service.api)
    assert_error("stored_configuration_unavailable", lambda: restarted.connect(grant))
    assert len(fake.created) == 1


def test_backend_api_auth_validation_and_secret_free_errors(rig):
    service, fake, *_ = rig
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_app(service, TOKEN)) as client:
        data = intent().model_dump()
        response = client.post("/internal/v1/connections", json=data)
        assert response.status_code == 401 and not fake.created
        response = client.post("/internal/v1/connections", json=data | {"unexpected": "synthetic-secret"}, headers=headers)
        assert response.status_code == 422 and "synthetic-secret" not in response.text
        fake.post_failure = "timeout"
        response = client.post("/internal/v1/connections", json=data, headers=headers)
        assert response.status_code == 409
        assert response.json() == {"error": "provisioning_uncertain"}
        assert response.headers["cache-control"] == "no-store"
        assert "secret" not in response.text


def test_successful_http_export_is_non_cacheable(rig):
    service, *_ = rig
    with TestClient(create_app(service, TOKEN)) as client:
        response = client.post("/internal/v1/connections", json=intent().model_dump(), headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["configuration"] == CONFIG
        assert "peer_id" not in response.json()


@pytest.mark.parametrize("url", ["http://node.invalid", "https://user:pass@node.invalid", "https://node.invalid?key=secret", "https://node.invalid/path"])
def test_node_origin_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        Node("a", url, "a", "nl", 100, "test" * 8)


def test_redirect_never_receives_api_key(rig):
    service, _, nodes, *_ = rig
    seen = []

    def redirect(request):
        seen.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://another.invalid/clients"})

    api = AmneziaAPI(httpx.MockTransport(redirect))
    assert_error("provisioning_uncertain", lambda: api.create(nodes[0], "operation", int(time.time()) + 60))
    assert seen == ["a.invalid"]


def test_duplicate_node_identity_is_rejected(rig):
    service, _, nodes, *_ = rig
    with pytest.raises(ValueError):
        Orchestrator([nodes[0], replace(nodes[0], id="duplicate")], service.store, service.api)


def test_replacing_node_under_same_alias_does_not_export_old_configuration(rig):
    service, fake, nodes, *_ = rig
    grant = intent()
    service.connect(grant)
    service.nodes["b"] = replace(nodes[1], server_id="replacement")
    assert_error("assigned_node_identity_changed", lambda: service.connect(grant))
    assert len(fake.created) == 1


def test_stale_observation_cannot_authorize_create(rig):
    service, _, nodes, *_ = rig
    observation = service.api.observe(nodes[0])
    stale = replace(observation, started_at=time.time() - 31)
    assert_error("no_eligible_node", lambda: service.store.reserve(intent(), [stale]))


def test_oversized_response_is_uncertain_and_not_exposed(rig):
    _, _, nodes, *_ = rig
    api = AmneziaAPI(httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (513 * 1024))))
    assert_error("provisioning_uncertain", lambda: api.create(nodes[0], "operation", int(time.time()) + 60))


def test_world_readable_state_directory_is_rejected(tmp_path):
    directory = tmp_path / "unsafe"
    directory.mkdir()
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        Store(directory, Fernet.generate_key())
