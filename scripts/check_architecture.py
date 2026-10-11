#!/usr/bin/env python3
"""Check static runtime imports without importing the application or its settings."""

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def modules_in(root):
    modules = {}
    for path in sorted((root / "orchestrator").rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def layer(module, policy):
    if module == "orchestrator":
        return "package"
    matches = [
        (len(prefix), name)
        for name, prefixes in policy["layers"].items()
        for prefix in prefixes
        if module == prefix or module.startswith(prefix + ".")
    ]
    return max(matches)[1] if matches else None


def imports(module, path, modules):
    """Include function-local and TYPE_CHECKING imports and resolve relative imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    dynamic_names = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            dynamic_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name)
            and node.func.id in dynamic_names
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        ):
            # Runtime plugin loading is not part of the architecture contract.
            yield node.lineno, "<dynamic-import>"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                if node.level > len(parts):
                    yield node.lineno, "<invalid-relative-import>"
                    continue
                base = ".".join(parts[: len(parts) - node.level + 1])
                if node.module:
                    base += "." + node.module
            else:
                base = node.module or ""
            for alias in node.names:
                candidate = base + "." + alias.name
                yield node.lineno, candidate if candidate in modules else base


def check(root, policy):
    modules = modules_in(root)
    errors, observed, exceptions = [], set(), {}
    if not modules:
        return ["No runtime modules found"]
    for exception in policy["exceptions"]:
        key = exception["source"], exception["target"]
        if key in exceptions or not exception.get("reason") or not exception.get("remove_in"):
            errors.append(f"Invalid or duplicate exception: {key}")
        exceptions[key] = exception
    for source, path in modules.items():
        source_layer = layer(source, policy)
        if source_layer is None:
            errors.append(f"Unclassified runtime module: {source}")
            continue
        for line, target in imports(source, path, modules):
            location = f"{path.relative_to(root)}:{line}"
            if target.startswith("<"):
                errors.append(f"{location}: unsupported {target}")
                continue
            if target == "orchestrator" or target.startswith("orchestrator."):
                target_layer = layer(target, policy)
                if target not in modules or target_layer is None:
                    errors.append(f"{location}: unknown runtime dependency {target}")
                elif target_layer not in policy["allowed"][source_layer]:
                    edge = source, target
                    observed.add(edge)
                    if edge not in exceptions:
                        errors.append(f"{location}: forbidden dependency {source} -> {target}")
            elif source_layer in policy["external_allowlist"]:
                package = target.split(".")[0]
                if (
                    package not in sys.stdlib_module_names
                    and package not in policy["external_allowlist"][source_layer]
                ):
                    errors.append(f"{location}: forbidden external dependency {target}")
    for source, target in sorted(exceptions.keys() - observed):
        errors.append(f"Remove stale exception: {source} -> {target}")
    return errors


def main():
    policy = json.loads((ROOT / "contracts/architecture.json").read_text(encoding="utf-8"))
    errors = check(ROOT, policy)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(
        f"Architecture verified: {len(modules_in(ROOT))} modules; "
        f"{len(policy['exceptions'])} explicit migration exceptions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
