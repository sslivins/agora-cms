"""Feature-flag governance (phase 5 of the flag rollout).

Flags rot. The failure mode isn't dramatic -- it's that three years later the
registry holds forty entries, nobody remembers which are load-bearing, and
every one of them is a branch that has to be reasoned about. These tests are
the maintenance pressure that stops that, and they add no production code:
everything here checks what ``cms/services/feature_flags.py`` already declares.

Two things are enforced.

**Call sites must resolve to a declared flag.** Flags are looked up by string
literal, and an unknown name deliberately fails *closed* -- ``enabled()``
returns ``False`` and logs a warning rather than raising, so a live page can't
500 because of a typo. That is the right runtime behaviour and a terrible
debugging experience: a renamed or deleted flag makes the feature quietly
vanish for everyone, with nothing louder than a log line. Catching it
statically is the trade that makes fail-closed affordable.

**Release flags must not outlive their expiry.** A ``RELEASE`` flag is a
promise to come back and delete it; ``Flag.__post_init__`` already forces one
to name a date. Nothing acted on that date until now. Once it passes, CI fails
and the choice has to be made explicitly: remove the flag and keep the code, or
remove both.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from cms.services.feature_flags import REGISTRY, FlagKind


REPO_ROOT = Path(__file__).parent.parent

# Everything that could plausibly evaluate a flag. Tests are excluded on
# purpose: they declare throwaway registries via the `registry=` parameter.
SOURCE_ROOTS = ("cms", "shared", "worker", "mcp")

FLAG_MODULE = "cms.services.feature_flags"

# `enabled` is the only lookup that fails silently, so it's the one that needs
# static checking. `get` returns None and `set_state` raises KeyError -- both
# surface an unknown name on their own.
LOOKUP_FUNC = "enabled"


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        files.extend(
            p
            for p in base.rglob("*.py")
            if "__pycache__" not in p.parts and not p.name.startswith(".")
        )
    return sorted(files)


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings.

    Needed because the sanctioned way to name a flag is a constant, not a bare
    literal -- ``assistant_flag.py`` calls ``enabled(db, ASSISTANT_FLAG_KEY)``.
    Without this the checker would skip precisely the call sites written the
    tidy way.
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value, str
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _lookup_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names by which this module can reach ``feature_flags.enabled``.

    Returns ``(module_aliases, direct_names)`` -- the former called as
    ``<alias>.enabled(...)``, the latter as ``<name>(...)``.
    """
    module_aliases: set[str] = set()
    direct_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == FLAG_MODULE:
                    module_aliases.add(a.asname or a.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module == FLAG_MODULE:
                for a in node.names:
                    if a.name == LOOKUP_FUNC:
                        direct_names.add(a.asname or a.name)
            elif node.module == "cms.services":
                for a in node.names:
                    if a.name == "feature_flags":
                        module_aliases.add(a.asname or a.name)
    return module_aliases, direct_names


def _flag_name_arg(call: ast.Call) -> ast.expr | None:
    """The ``name`` argument of an ``enabled(db, name, ...)`` call."""
    for kw in call.keywords:
        if kw.arg == "name":
            return kw.value
    if len(call.args) >= 2:
        return call.args[1]
    return None


class _CallSite:
    def __init__(self, path: Path, lineno: int, name: str | None, raw: str):
        self.path = path
        self.lineno = lineno
        self.name = name  # resolved flag name, or None if not static
        self.raw = raw

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT).as_posix()}:{self.lineno}"


def _collect_call_sites() -> list[_CallSite]:
    sites: list[_CallSite] = []

    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue

        module_aliases, direct_names = _lookup_aliases(tree)
        if not module_aliases and not direct_names:
            continue

        consts = _module_string_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            is_lookup = (
                isinstance(func, ast.Attribute)
                and func.attr == LOOKUP_FUNC
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            ) or (isinstance(func, ast.Name) and func.id in direct_names)
            if not is_lookup:
                continue

            arg = _flag_name_arg(node)
            if arg is None:
                sites.append(_CallSite(path, node.lineno, None, "<no name argument>"))
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                sites.append(_CallSite(path, node.lineno, arg.value, repr(arg.value)))
            elif isinstance(arg, ast.Name) and arg.id in consts:
                sites.append(
                    _CallSite(path, node.lineno, consts[arg.id], arg.id)
                )
            else:
                sites.append(
                    _CallSite(path, node.lineno, None, ast.unparse(arg))
                )

    return sites


# ── The checker itself ──
#
# A scanner that silently finds nothing is worse than no scanner, because it
# reports success. These two tests guard the checker before the checker guards
# the registry.


def test_source_scan_finds_files():
    files = _source_files()
    assert files, "source scan matched no files - SOURCE_ROOTS is wrong"


def test_scanner_detects_a_known_call_site():
    """The Assistant shim must be found.

    It is the only ``enabled()`` caller today, and it exercises the awkward
    path: imported by name, called with a module-level constant rather than a
    literal. If the scanner can't see that one, it can't see anything.
    """
    sites = _collect_call_sites()
    shim = [s for s in sites if s.path.name == "assistant_flag.py"]
    assert shim, (
        "scanner found no enabled() call in cms/services/assistant_flag.py. "
        "Either the shim changed or alias detection is broken - fix the "
        "scanner, don't delete this test."
    )
    assert any(s.name == "assistant" for s in shim), (
        "found the call but couldn't resolve its flag name to 'assistant'; "
        f"got {[s.raw for s in shim]}"
    )


# ── Registry / call-site coverage ──


def test_every_call_site_names_a_declared_flag():
    unknown = [s for s in _collect_call_sites() if s.name and s.name not in REGISTRY]
    assert not unknown, (
        "these enabled() calls name a flag that isn't in REGISTRY. They fail "
        "closed at runtime, so the feature would silently vanish rather than "
        "error:\n"
        + "\n".join(f"  {s.where}: {s.raw}" for s in unknown)
    )


def test_flag_names_are_statically_resolvable():
    """No computed flag names.

    A name built at runtime defeats the check above, and there is no reason to
    build one: the registry is a fixed set known at import time. If a genuine
    need appears, this test is the place to record the exception.
    """
    dynamic = [s for s in _collect_call_sites() if s.name is None]
    assert not dynamic, (
        "these enabled() calls don't use a literal or a module-level string "
        "constant, so their flag name can't be checked against the registry:\n"
        + "\n".join(f"  {s.where}: {s.raw}" for s in dynamic)
    )


# ── Expiry ──


def test_no_release_flag_is_past_its_expiry():
    today = date.today()
    overdue = [
        (name, flag.expires)
        for name, flag in sorted(REGISTRY.items())
        if flag.kind is FlagKind.RELEASE
        and flag.expires is not None
        and today > flag.expires
    ]
    assert not overdue, (
        "these release flags are past the date they were meant to be removed. "
        "Delete the flag and keep the code, or delete both - don't just push "
        "the date out:\n"
        + "\n".join(f"  {name}: expired {expires}" for name, expires in overdue)
    )


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_permanent_flags_do_not_set_an_expiry(name: str):
    """An expiry on a permanent flag is silently ignored.

    ``FlagView.is_overdue`` only considers ``RELEASE`` flags, so a date on a
    permanent one reads like a commitment that nothing will ever enforce.
    """
    flag = REGISTRY[name]
    if flag.kind is FlagKind.PERMANENT:
        assert flag.expires is None, (
            f"{name} is permanent but sets expires={flag.expires!r}, which is "
            "never checked. Drop the date, or make the flag a release flag."
        )


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_flag_declares_a_description_and_owner(name: str):
    """Both are shown on the Features page.

    Whoever finds this flag in two years needs to know what it gates and who
    to ask before touching it.
    """
    flag = REGISTRY[name]
    assert flag.description and flag.description.strip(), f"{name}: no description"
    assert flag.owner and flag.owner.strip(), f"{name}: no owner"
