from pathlib import Path
import importlib.util
import shutil
import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_gates_reject_mutable_actions_and_missing_attestation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / ".github", tmp_path / ".github")
    checker = load("check_workflows")
    checker.check(tmp_path)
    release = tmp_path / ".github/workflows/release.yml"
    original = release.read_text()
    release.write_text(
        original.replace(
            "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6", "actions/attest@v4"
        )
    )
    with pytest.raises(ValueError, match="Unpinned"):
        checker.check(tmp_path)
    release.write_text(original.replace("needs: verify", "needs: unrelated"))
    with pytest.raises(ValueError, match="Missing release gate"):
        checker.check(tmp_path)
