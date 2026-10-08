"""Operator commands. Never print transport credentials or configuration bodies."""
import argparse
import json
import os
import re
from urllib.parse import urlsplit
import httpx
from .models import PilotError
from .settings import load_settings, read_secret

CAPABILITIES = [{'protocol': 'amneziawg', 'configuration_version': 1}]


def safe_response(value):
    if isinstance(value, list): return [safe_response(v) for v in value]
    if not isinstance(value, dict): return value
    allowed = {'status', 'service', 'version', 'contract_version', 'nodes', 'id', 'node_id',
               'region', 'mode', 'protocol', 'assigned', 'pending', 'capacity',
               'schema_version', 'connection_id', 'device_id', 'state', 'expires_at', 'revision'}
    return {k: safe_response(v) for k, v in value.items() if k in allowed}


def request(base_url, token, method, path, body=None):
    url = urlsplit(base_url)
    if (url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
        or (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1', 'localhost')))):
        raise ValueError('HTTPS or loopback required')
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        response = client.request(method, base_url.rstrip('/') + path,
                                  headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, json=body)
        if response.status_code != 200:
            raise PilotError('request_failed', response.status_code)
        if 'no-store' not in response.headers.get('cache-control','') or len(response.content) > 4*1024*1024:
            raise ValueError('Invalid response')
        return safe_response(response.json())


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise argparse.ArgumentTypeError('Invalid identifier')
    return value


def parser():
    p = argparse.ArgumentParser(prog='orchestratorctl')
    p.add_argument('--settings', default=os.environ.get('ORCHESTRATOR_SETTINGS', '/etc/vpn-orchestrator/settings.json'))
    p.add_argument('--url', default='http://127.0.0.1:8792')
    sub = p.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check-config');check.add_argument('--structure-only', action='store_true');check.add_argument('--probe', action='store_true')
    serve = sub.add_parser('serve');serve.add_argument('--host', default='127.0.0.1');serve.add_argument('--port', type=int, default=8792)
    for name in ('health', 'nodes', 'reconcile'): sub.add_parser(name)
    connection = sub.add_parser('connection').add_subparsers(dest='operation', required=True)
    for name in ('create', 'show', 'renew', 'disable', 'enable', 'recover'):
        op = connection.add_parser(name)
        if name != 'create': op.add_argument('connection_id', type=identifier)
        op.add_argument('--device', required=True, type=identifier)
        if name == 'create': op.add_argument('--name', required=True)
        if name in ('create', 'renew'): op.add_argument('--expires-at', required=True)
        if name == 'renew': op.add_argument('--key', required=True, type=identifier)
        if name == 'recover': op.add_argument('--revision', required=True, type=int)
    return p


def run(args):
    os.environ['ORCHESTRATOR_SETTINGS'] = args.settings
    if args.command == 'check-config':
        settings = load_settings(args.settings, secrets=not args.structure_only)
        if args.probe:
            if args.structure_only: raise ValueError('Probe requires credentials')
            from .node_agent import AgentAPI
            for node in settings.validate_nodes(): AgentAPI().verify(node)
        # Check pinned assignments without opening a writer or initializing a journal.
        if not args.structure_only:
            import sqlite3
            from pathlib import Path
            journal = Path(settings.state_directory) / 'assignments.sqlite3'
            if journal.exists():
                nodes = {n.id: n for n in settings.nodes}
                with sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True) as db:
                    for node_id, server_id, protocol in db.execute('SELECT node_id,server_id,protocol FROM assignments'):
                        if node_id not in nodes or (nodes[node_id].server_id,nodes[node_id].protocol) != (server_id,protocol):
                            raise ValueError('Configuration removes or changes a pinned node')
                    for node_id, server_id in db.execute("SELECT target_node,target_server FROM switches WHERE state NOT IN ('complete','cancelled')"):
                        if node_id not in nodes or nodes[node_id].server_id != server_id:
                            raise ValueError('Configuration removes or changes a pending target')
        return {'valid': True, 'nodes': len(settings.nodes), 'secrets_checked': not args.structure_only}
    if args.command == 'serve':
        load_settings(args.settings)
        import uvicorn
        uvicorn.run('orchestrator.runtime:agent_app', factory=True, host=args.host, port=args.port, access_log=False)
        return None
    if args.command == 'reconcile':
        from .runtime import agent_service
        from .recovery import reconcile
        service, _ = agent_service()
        return reconcile(service)
    settings = load_settings(args.settings)
    token = read_secret(settings.backend_token_file).decode()
    if args.command in ('health','nodes'):
        return request(args.url, token, 'GET', '/v1/health' if args.command=='health' else '/internal/v1/nodes')
    prefix = '/internal/v2/connections'
    if args.operation == 'show':
        return request(args.url, token, 'GET', prefix+'/'+args.connection_id+'?device_id='+args.device)
    body = {'device_id': args.device}
    if args.operation in ('create', 'recover'):
        body.update(schema_version=1, capabilities=CAPABILITIES)
    if args.operation == 'create': body.update(name=args.name, expires_at=args.expires_at)
    if args.operation == 'renew': body.update(expires_at=args.expires_at, idempotency_key=args.key)
    if args.operation == 'recover': body.update(expected_revision=args.revision)
    path = prefix if args.operation == 'create' else prefix+'/'+args.connection_id+'/'+args.operation
    return request(args.url, token, 'POST', path, body)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if result is not None: print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Third-party exceptions can contain response bodies or credentials.
        print(json.dumps({'error': error.code if isinstance(error, PilotError) else 'command_failed',
                          'status': error.status if isinstance(error, PilotError) else None}))
        return 1
