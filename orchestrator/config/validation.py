"""Read-only configuration and pinned identity checks."""

from pathlib import Path
from orchestrator.config.settings import load_settings
from orchestrator.infrastructure.sqlite.inventory import read_inventory


def validate_configuration(path, structure_only=False, probe=False):
    settings = load_settings(path, secrets=not structure_only)
    if probe and structure_only:
        raise ValueError("Probe requires credentials")
    saved_inventory = read_inventory(Path(settings.state_directory) / "assignments.sqlite3")
    nodes = {n.id: n for n in (saved_inventory if saved_inventory is not None else settings.nodes)}
    # Check pinned assignments without opening a writer or initializing a journal.
    if not structure_only:
        import sqlite3

        journal = Path(settings.state_directory) / "assignments.sqlite3"
        if journal.exists():
            nodes = {n.id: n for n in settings.nodes}
            with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='node_registry'").fetchone():
                    from orchestrator.config.settings import NodeSettings

                    saved = [
                        NodeSettings.model_validate_json(r[0])
                        for r in db.execute("SELECT configuration FROM node_registry")
                    ]
                    nodes = {n.id: n for n in saved}
                    for n in saved:
                        n.node()
                for node_id, server_id, protocol in db.execute(
                    "SELECT node_id,server_id,protocol FROM assignments"
                ):
                    if node_id not in nodes or (
                        nodes[node_id].server_id,
                        nodes[node_id].protocol,
                    ) != (server_id, protocol):
                        raise ValueError("Configuration removes or changes a pinned node")
                for node_id, server_id in db.execute(
                    "SELECT target_node,target_server FROM switches WHERE state NOT IN ('complete','cancelled')"
                ):
                    if node_id not in nodes or nodes[node_id].server_id != server_id:
                        raise ValueError("Configuration removes or changes a pending target")
    if probe:
        from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI

        for node in nodes.values():
            AgentAPI().verify(node.node())
    return {"valid": True, "nodes": len(nodes), "secrets_checked": not structure_only}
