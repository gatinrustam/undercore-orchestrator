"""Operator commands. Never print transport credentials or configuration bodies."""

import argparse
import json
import os
import re
from urllib.parse import urlsplit
import httpx
from orchestrator.domain.models import OrchestratorError
from orchestrator.bootstrap import load_settings
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.interfaces.cli.node_input import load_node_input

CAPABILITIES = [{"protocol": "amneziawg", "configuration_version": 1}]


def safe_response(value):
    if isinstance(value, list):
        return [safe_response(v) for v in value]
    if not isinstance(value, dict):
        return value
    allowed = {
        "status",
        "service",
        "version",
        "contract_version",
        "nodes",
        "id",
        "node_id",
        "region",
        "mode",
        "protocol",
        "assigned",
        "pending",
        "capacity",
        "notice",
        "running",
        "services",
        "lease_enabled",
        "lease_verified",
        "fenced",
        "api_url",
        "server_id",
        "schema_version",
        "connection_id",
        "device_id",
        "state",
        "expires_at",
        "revision",
    }
    return {k: safe_response(v) for k, v in value.items() if k in allowed}


def request(base_url, token, method, path, body=None):
    url = urlsplit(base_url)
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost"))
        )
    ):
        raise ValueError("HTTPS or loopback required")
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        response = client.request(
            method,
            base_url.rstrip("/") + path,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            json=body,
        )
        if response.status_code != 200:
            raise OrchestratorError("request_failed", response.status_code)
        if (
            "no-store" not in response.headers.get("cache-control", "")
            or len(response.content) > 4 * 1024 * 1024
        ):
            raise ValueError("Invalid response")
        return safe_response(response.json())


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise argparse.ArgumentTypeError("Invalid identifier")
    return value


def parser():
    p = argparse.ArgumentParser(prog="undercore-orchestrator")
    p.add_argument(
        "--settings",
        default=os.environ.get("ORCHESTRATOR_SETTINGS", "/etc/vpn-orchestrator/settings.json"),
    )
    p.add_argument("--url", default="http://127.0.0.1:8792")
    sub = p.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--output", required=True)
    panel = sub.add_parser("panel")
    panel.add_argument("--token-file", required=True)
    panel.add_argument("--port", type=int, default=8793)
    panel.add_argument("--manage", action="store_true", help="Enable audited operator commands")
    operator = sub.add_parser("operator").add_subparsers(dest="operator_action", required=True)
    execute = operator.add_parser("execute")
    execute.add_argument("--file", required=True)
    operator.add_parser("policy")
    operator.add_parser("audit")
    init = sub.add_parser("init")
    init.add_argument("--directory", required=True)
    init.add_argument("--state-directory")
    for action in ("start", "stop", "restart", "status"):
        sub.add_parser(action)
    config = sub.add_parser("config").add_subparsers(dest="config_operation", required=True)
    config_check = config.add_parser("validate")
    config_check.add_argument("--structure-only", action="store_true")
    config_check.add_argument("--probe", action="store_true")
    for verb in ("add", "update", "check", "restore", "list"):
        kind = sub.add_parser(verb).add_subparsers(dest="resource", required=True)
        if verb == "restore":
            restore = kind.add_parser("backup")
            restore.add_argument("--input", required=True)
            restore.add_argument("--output", required=True)
        server = kind.add_parser("servers" if verb == "list" else "server")
        if verb in ("add", "update"):
            server.add_argument("--file", required=True)
        if verb in ("update", "check", "restore"):
            server.add_argument("node_id", type=identifier)
    check = sub.add_parser("check-config")
    check.add_argument("--structure-only", action="store_true")
    check.add_argument("--probe", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8792)
    for name in ("health", "nodes", "reconcile"):
        sub.add_parser(name)
    node = sub.add_parser("node").add_subparsers(dest="node_operation", required=True)
    node.add_parser("list")
    save = node.add_parser("save")
    save.add_argument("--file", required=True)
    for action in ("probe", "restore"):
        node.add_parser(action).add_argument("node_id", type=identifier)
    connection = sub.add_parser("connection").add_subparsers(dest="operation", required=True)
    for name in ("create", "show", "renew", "disable", "enable", "recover"):
        op = connection.add_parser(name)
        if name != "create":
            op.add_argument("connection_id", type=identifier)
        op.add_argument("--device", required=True, type=identifier)
        if name == "create":
            op.add_argument("--name", required=True)
        if name in ("create", "renew"):
            op.add_argument("--expires-at", required=True)
        if name == "renew":
            op.add_argument("--key", required=True, type=identifier)
        if name == "recover":
            op.add_argument("--revision", required=True, type=int)
    return p


def run(args):
    os.environ["ORCHESTRATOR_SETTINGS"] = args.settings
    if args.command == "panel":
        from orchestrator.bootstrap import panel_app
        import uvicorn

        if not 1024 <= args.port <= 65535:
            raise ValueError("Invalid panel port")
        uvicorn.run(
            panel_app(args.token_file, args.port, args.manage),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            proxy_headers=False,
        )
        return None
    if args.command == "operator":
        from orchestrator.bootstrap import administration_service
        from orchestrator.domain.administration import Command

        admin = administration_service()
        if args.operator_action == "policy":
            return admin.policies.read()
        if args.operator_action == "audit":
            return {"entries": admin.journal.recent()}
        return admin.execute(Command.model_validate_json(read_secret(args.file)), "cli")
    if args.command == "backup":
        from orchestrator.interfaces.cli.backup import backup

        return backup(args.settings, args.output)
    if args.command == "restore" and args.resource == "backup":
        from orchestrator.interfaces.cli.backup import restore_backup

        return restore_backup(args.input, args.output)
    if args.command == "init":
        from orchestrator.interfaces.cli.initialize import initialize

        return initialize(args.directory, args.state_directory)
    if args.command in ("start", "stop", "restart", "status"):
        from orchestrator.infrastructure.systemd import control

        return control(args.command)
    if args.command == "config":
        args.command = "check-config"
    if args.command in ("add", "update", "check", "restore", "list"):
        verb = args.command
        args.command = "node"
        args.node_operation = {
            "add": "add",
            "update": "update",
            "check": "probe",
            "restore": "restore",
            "list": "list",
        }[verb]

    if args.command == "check-config":
        from orchestrator.bootstrap import validate_configuration

        return validate_configuration(args.settings, args.structure_only, args.probe)
    if args.command == "serve":
        load_settings(args.settings)
        import uvicorn

        uvicorn.run(
            "orchestrator.runtime:agent_app",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
        )
        return None
    if args.command == "reconcile":
        from orchestrator.bootstrap import agent_service
        from orchestrator.application.recovery import reconcile

        service, _ = agent_service()
        try:
            return reconcile(service)
        finally:
            service.close()
    settings = load_settings(args.settings)
    if args.command == "node":
        from orchestrator.bootstrap import agent_service
        from orchestrator.application.management import manage_node

        if args.node_operation == "list":
            service, _ = agent_service()
            try:
                return manage_node(service, "list")
            finally:
                service.close()
        from orchestrator.bootstrap import administration_service
        from orchestrator.domain.administration import Command
        import uuid

        data = load_node_input(getattr(args, "file", None)) or {}
        target = getattr(args, "node_id", None) or data.get("id")
        if args.node_operation == "add":
            if data.get("expected_revision", 0) != 0:
                raise OrchestratorError("invalid_request", 422)
            data["expected_revision"] = 0
        if args.node_operation == "update" and (
            data.get("id") != target
            or type(data.get("expected_revision")) is not int
            or data["expected_revision"] < 1
        ):
            raise OrchestratorError("invalid_request", 422)
        action = (
            "node.save"
            if args.node_operation in ("add", "update", "save")
            else "node." + args.node_operation
        )
        return administration_service().execute(
            Command(operation_id=uuid.uuid4().hex, action=action, target=target, data=data), "cli"
        )
    token = read_secret(settings.backend_token_file).decode()
    if args.command in ("health", "nodes"):
        return request(
            args.url,
            token,
            "GET",
            "/v1/health" if args.command == "health" else "/internal/v1/nodes",
        )
    prefix = "/internal/v2/connections"
    if args.operation == "show":
        return request(
            args.url, token, "GET", prefix + "/" + args.connection_id + "?device_id=" + args.device
        )
    body = {"device_id": args.device}
    if args.operation in ("create", "recover"):
        body.update(schema_version=1, capabilities=CAPABILITIES)
    if args.operation == "create":
        body.update(name=args.name, expires_at=args.expires_at)
    if args.operation == "renew":
        body.update(expires_at=args.expires_at, idempotency_key=args.key)
    if args.operation == "recover":
        body.update(expected_revision=args.revision)
    path = (
        prefix
        if args.operation == "create"
        else prefix + "/" + args.connection_id + "/" + args.operation
    )
    return request(args.url, token, "POST", path, body)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if result is not None:
            print(
                json.dumps(
                    safe_response(result) if args.command == "node" else result, ensure_ascii=False
                )
            )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Third-party exceptions can contain response bodies or credentials.
        print(
            json.dumps(
                {
                    "error": error.code
                    if isinstance(error, OrchestratorError)
                    else "command_failed",
                    "status": error.status if isinstance(error, OrchestratorError) else None,
                }
            )
        )
        return 1
