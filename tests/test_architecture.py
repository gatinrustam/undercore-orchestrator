"""Regression tests for the guardrail, using synthetic modules and no runtime imports."""

import json
from pathlib import Path

import pytest

from scripts.check_architecture import check

ROOT = Path(__file__).resolve().parents[1]


def policy():
    value = json.loads((ROOT / "contracts/architecture.json").read_text())
    value["exceptions"] = []
    return value


def source(root, module, text=""):
    path = root.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize(
    "statement",
    [
        "import orchestrator.infrastructure.secrets as secret_store",
        "from orchestrator.infrastructure import secrets as secret_store",
        "from ..infrastructure import secrets",
        "def delayed():\n    from orchestrator.infrastructure.secrets import read_secret",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..infrastructure import secrets",
    ],
)
def test_domain_cannot_import_infrastructure(tmp_path, statement):
    source(tmp_path, "orchestrator.domain.example", statement)
    source(tmp_path, "orchestrator.infrastructure.secrets")
    assert any("forbidden dependency" in error for error in check(tmp_path, policy()))


def test_ports_and_domain_models_are_allowed_without_importing_them(tmp_path):
    source(tmp_path, "orchestrator.domain.models", "raise RuntimeError('must not execute')")
    source(tmp_path, "orchestrator.application.ports", "from ..domain import models")
    source(
        tmp_path, "orchestrator.infrastructure.drivers.example", "from ...application import ports"
    )
    assert check(tmp_path, policy()) == []


def test_compatibility_facade_cannot_bypass_layer_boundary(tmp_path):
    source(tmp_path, "orchestrator.application.example", "from orchestrator import runtime")
    source(tmp_path, "orchestrator.runtime")
    assert any("forbidden dependency" in error for error in check(tmp_path, policy()))


def test_exception_is_exact_and_must_be_removed_when_dependency_disappears(tmp_path):
    value = policy()
    value["exceptions"] = [
        {
            "source": "orchestrator.application.example",
            "target": "orchestrator.infrastructure.secrets",
            "reason": "Synthetic migration fixture",
            "remove_in": "test",
        }
    ]
    source(tmp_path, "orchestrator.application.example", "from ..infrastructure import secrets")
    source(tmp_path, "orchestrator.infrastructure.secrets")
    assert check(tmp_path, value) == []
    source(tmp_path, "orchestrator.application.another", "from ..infrastructure import secrets")
    assert any("forbidden dependency" in error for error in check(tmp_path, value))
    source(tmp_path, "orchestrator.application.example")
    assert any("stale exception" in error for error in check(tmp_path, value))


@pytest.mark.parametrize(
    "statement",
    [
        "__import__('orchestrator.infrastructure.secrets')",
        "import importlib as loader\nloader.import_module('orchestrator.infrastructure.secrets')",
        "from importlib import import_module as load\nload('orchestrator.infrastructure.secrets')",
    ],
)
def test_dynamic_imports_require_explicit_architecture_change(tmp_path, statement):
    source(tmp_path, "orchestrator.domain.example", statement)
    assert any("dynamic-import" in error for error in check(tmp_path, policy()))


def test_framework_dependencies_and_unclassified_modules_are_rejected(tmp_path):
    source(tmp_path, "orchestrator.domain.example", "import httpx")
    source(tmp_path, "orchestrator.new_facade")
    errors = check(tmp_path, policy())
    assert any("forbidden external dependency" in error for error in errors)
    assert any("Unclassified runtime module" in error for error in errors)


def test_runtime_respects_architecture_contract():
    value = json.loads((ROOT / "contracts/architecture.json").read_text())
    assert check(ROOT, value) == []
