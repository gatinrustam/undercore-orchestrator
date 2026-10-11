"""Compatible worker entry point for existing systemd units."""

from orchestrator.application.leases import *  # noqa: F403

if __name__ == "__main__":
    import json
    from orchestrator.bootstrap import agent_service
    from orchestrator.application.leases import heartbeat_all

    service, _ = agent_service()
    try:
        print(json.dumps(heartbeat_all(service)))
    finally:
        service.close()
