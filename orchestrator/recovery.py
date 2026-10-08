"""Compatible worker entry point for existing systemd units."""

from orchestrator.application.recovery import *  # noqa: F403

if __name__ == "__main__":
    import json
    from orchestrator.bootstrap import agent_service
    from orchestrator.application.recovery import reconcile

    service, _ = agent_service()
    print(json.dumps(reconcile(service)))
