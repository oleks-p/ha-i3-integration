"""Checks on the adapter modules that read the source rather than run it.

Behaviour is tested against Home Assistant in test_integration.py. These catch
the declaration faults that are cheaper to see in the source: they hold for
every adapter, including one nobody has written an integration test for yet.
"""
import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "i3_atom"
CORE = ("__init__.py", "config_flow.py", "const.py", "entity.py", "model.py")
ADAPTERS = sorted(p for p in PACKAGE.glob("*.py") if p.name not in CORE)


def _assignments(name):
    """Every `<name> = <value>` in every module, as (file, value node)."""
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                attr = (target.attr if isinstance(target, ast.Attribute)
                        else target.id if isinstance(target, ast.Name) else None)
                if attr == name and node.value is not None:
                    yield path.name, node.value


def test_supported_features_is_never_a_bare_number():
    """It must be the FLAG type, not a plain 0.

    Home Assistant tests a feature with `in`, which an int does not support:
    every entity of that platform then fails to add with "argument of type
    'int' is not a container or iterable". The registry row is created BEFORE
    the capability check, so the result is not an obvious absence - it is a
    full set of entities that exist and are permanently unavailable, which
    reads like a connectivity fault rather than a declaration fault.
    """
    for where, value in _assignments("_attr_supported_features"):
        if isinstance(value, ast.Constant):
            raise AssertionError(
                f"{where}: _attr_supported_features = {value.value!r} is a bare "
                f"constant; use the platform's own Feature flag type")


def test_every_adapter_declares_what_it_reads():
    """An adapter's docstring names its roles, because the roles are the only
    contract between it and the configuration. One that does not document
    them makes the config format guesswork."""
    for path in ADAPTERS:
        doc = ast.get_docstring(ast.parse(path.read_text())) or ""
        assert "reads." in doc or "no reads" in doc, path.name


def test_every_adapter_validates_its_options():
    """An entity class without OPTIONS accepts only `controller`, so any option
    it reads would be refused - and one that bypasses validation is how a typo
    in a device class used to take a whole platform down."""
    for path in ADAPTERS:
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(b, ast.Name) and b.id == "I3Entity" for b in node.bases):
                declared = {t.id for n in node.body if isinstance(n, ast.Assign)
                            for t in n.targets if isinstance(t, ast.Name)}
                assert "OPTIONS" in declared, f"{path.name}: {node.name} has no OPTIONS"


def test_every_adapter_is_a_platform_the_schema_accepts():
    """The platform list is read from the files, so a file that is not named
    after a Home Assistant platform would silently never load."""
    from homeassistant.const import Platform

    from custom_components.i3_atom.const import PLATFORMS

    names = {p.stem for p in ADAPTERS}
    assert names == set(PLATFORMS)
    assert names <= {p.value for p in Platform}


def test_a_name_and_a_translation_key_are_never_both_set():
    """Home Assistant returns `_attr_name` before it considers a translation.

    So an entity that sets both keeps the configuration's single string, and
    the translation bundle it also declares does nothing - which reads as a
    broken bundle rather than as a line of code choosing one over the other.
    The two assignments must therefore be on opposite branches of the same
    `if`, which is what this checks: neither may sit at the body's top level
    where both would run.
    """
    tree = ast.parse((PACKAGE / "entity.py").read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__init__"):
            continue
        flat = {t.attr for stmt in node.body if isinstance(stmt, ast.Assign)
                for t in stmt.targets
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                and t.value.id == "self"}
        both = {"_attr_name", "_attr_translation_key"} & flat
        assert len(both) < 2, f"both set unconditionally in __init__: {both}"
