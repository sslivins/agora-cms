"""Feature flags — registry, evaluation, and administration.

Flags exist so that "deployed to production" and "released to users" can be
separate decisions.  Work merges to ``main`` and ships to prod dark; turning it
on is a reversible click that doesn't involve a deploy.

## Registry in code, state in the database

Each flag is *declared* in :data:`REGISTRY` with its description, owner,
default, kind and (for temporary toggles) an expiry date.  Only the mutable
part — current state and targeting — lives in ``feature_flags`` rows.  A flag
with no row falls back to its declared default, so nothing has to be seeded.

## Evaluation

Three states, deliberately not a boolean plus a list.  ``enabled=True`` with an
empty allowlist is ambiguous — everyone, or nobody? — and that ambiguity is
what the original Assistant flag papered over with a documented convention:

``off``       nobody, including admins.
``targeted``  the listed users and roles only.
``on``        everyone.

Off means off.  Admin access to a targeted flag is **opt-in per flag**
(:attr:`Flag.include_admins`) rather than blanket, because a blanket hatch
would make every half-built feature permanently visible to admins in
production and would make the disabled experience impossible to test.  What we
must never lock ourselves out of — the flag administration UI — is protected by
the ``features:write`` permission on the route, independently of any flag.

## Caching

Evaluation is memoised **per request**, not per process.  A process-wide TTL
cache would let replicas disagree about a flag for up to the TTL, which is a
bad property for a kill-switch being flipped during an incident (and
``services/fleet_registry.py`` already declines caching for the same
invalidation reasons).  Per-request memoisation gives the win that actually
matters — a page checking five flags issues one query, not five — with no
staleness and nothing to invalidate.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.feature_flag import FeatureFlagState
from cms.models.user import Role, User
from cms.permissions import SETTINGS_WRITE
from cms.permissions import has_permission


logger = logging.getLogger(__name__)


class FlagState(str, Enum):
    """Who a feature is on for."""

    OFF = "off"
    TARGETED = "targeted"
    ON = "on"


class FlagKind(str, Enum):
    """Why a flag exists — governs whether it's expected to be removed."""

    # A temporary rollout toggle.  Must carry an expiry; a test fails once it
    # is overdue so release toggles can't quietly become permanent.
    RELEASE = "release"
    # An ongoing control: an operational kill-switch, or a feature that is
    # intentionally limited-access forever.  Exempt from expiry.
    PERMANENT = "permanent"


@dataclass(frozen=True)
class Flag:
    """Code-side declaration of a feature flag."""

    description: str
    owner: str
    default: FlagState = FlagState.OFF
    kind: FlagKind = FlagKind.RELEASE
    # Whether ``settings:write`` holders bypass targeting for this flag.
    # Default False: off means off, including for admins.
    include_admins: bool = False
    # Required for RELEASE flags; meaningless for PERMANENT ones.
    expires: date | None = None

    def __post_init__(self) -> None:
        if self.kind is FlagKind.RELEASE and self.expires is None:
            raise ValueError(
                "release flags must declare an expiry date so they can't "
                "silently become permanent; use FlagKind.PERMANENT for "
                "kill-switches and ongoing limited-access features"
            )


@dataclass
class FlagView:
    """A flag's declaration plus its current stored state, for the UI."""

    name: str
    description: str
    owner: str
    kind: FlagKind
    state: FlagState
    user_ids: list[str] = field(default_factory=list)
    role_ids: list[str] = field(default_factory=list)
    expires: date | None = None
    updated_at: datetime | None = None
    updated_by_id: uuid.UUID | None = None
    # True when the stored state is absent and the default is in effect.
    is_default: bool = False

    @property
    def is_overdue(self) -> bool:
        """A release toggle past its expiry, i.e. owed a cleanup."""
        if self.kind is not FlagKind.RELEASE or self.expires is None:
            return False
        return date.today() > self.expires


# ── Registry ─────────────────────────────────────────────────────────
#
# Every flag the code can ask about must be declared here.  Tests assert that
# each ``enabled("...")`` literal in the codebase resolves to an entry, so a
# typo fails CI rather than silently disabling a feature.
#
# Entries are added by the phase that starts *using* them, so the registry
# never contains a flag nothing reads.

REGISTRY: dict[str, Flag] = {}


# ── Evaluation ───────────────────────────────────────────────────────


async def _permissions_for(db: AsyncSession, user: User) -> list[str]:
    """Return ``user``'s permissions, loading the role if it isn't loaded.

    Call sites vary in whether they eager-loaded ``User.role``.  Touching an
    unloaded relationship on an async session raises ``MissingGreenlet``, so
    check first and fall back to an explicit fetch.
    """
    try:
        if "role" not in sa_inspect(user).unloaded and user.role is not None:
            return list(user.role.permissions or [])
    except Exception:  # pragma: no cover - defensive, inspection is cheap
        pass
    if user.role_id is None:
        return []
    role = await db.get(Role, user.role_id)
    return list(role.permissions or []) if role else []


async def _evaluate(
    db: AsyncSession,
    name: str,
    user: User | None,
    registry: dict[str, Flag],
) -> bool:
    flag = registry.get(name)
    if flag is None:
        # Fail closed.  A test catches unknown names before they ship, so in
        # production this means someone deleted a registry entry that is still
        # referenced — safer to hide the feature than to throw on a live page.
        logger.warning("Unknown feature flag %r requested; treating as off", name)
        return False

    row = await db.get(FeatureFlagState, name)
    try:
        state = FlagState(row.state) if row is not None else flag.default
    except ValueError:
        logger.warning(
            "Feature flag %r has unrecognised state %r; falling back to default %r",
            name, row.state, flag.default.value,
        )
        state = flag.default

    if state is FlagState.ON:
        return True
    if state is FlagState.OFF:
        return False

    # TARGETED
    if user is None:
        return False
    if row is not None:
        if str(user.id) in (row.user_ids or []):
            return True
        if user.role_id is not None and str(user.role_id) in (row.role_ids or []):
            return True
    if flag.include_admins:
        perms = await _permissions_for(db, user)
        if has_permission(perms, SETTINGS_WRITE):
            return True
    return False


async def enabled(
    db: AsyncSession,
    name: str,
    user: User | None = None,
    *,
    request=None,
    registry: dict[str, Flag] | None = None,
) -> bool:
    """Return whether feature ``name`` is on for ``user``.

    Pass ``request`` to memoise the result for the life of that request; a page
    that checks several flags then costs one query rather than one per check.
    """
    registry = REGISTRY if registry is None else registry
    cache_key = (name, str(user.id) if user is not None else None)

    cache = None
    if request is not None:
        cache = getattr(request.state, "feature_flag_cache", None)
        if cache is None:
            cache = {}
            request.state.feature_flag_cache = cache
        if cache_key in cache:
            return cache[cache_key]

    result = await _evaluate(db, name, user, registry)
    if cache is not None:
        cache[cache_key] = result
    return result


# ── Administration ───────────────────────────────────────────────────


def _view(name: str, flag: Flag, row: FeatureFlagState | None) -> FlagView:
    return FlagView(
        name=name,
        description=flag.description,
        owner=flag.owner,
        kind=flag.kind,
        expires=flag.expires,
        state=FlagState(row.state) if row is not None else flag.default,
        user_ids=list(row.user_ids or []) if row is not None else [],
        role_ids=list(row.role_ids or []) if row is not None else [],
        updated_at=row.updated_at if row is not None else None,
        updated_by_id=row.updated_by_id if row is not None else None,
        is_default=row is None,
    )


async def get_all(
    db: AsyncSession,
    registry: dict[str, Flag] | None = None,
) -> list[FlagView]:
    """Every declared flag with its current state, for the Features page.

    Driven by the registry rather than the table, so a flag appears the moment
    it is declared — without anyone having to seed a row first.
    """
    registry = REGISTRY if registry is None else registry
    out: list[FlagView] = []
    for name in sorted(registry):
        row = await db.get(FeatureFlagState, name)
        out.append(_view(name, registry[name], row))
    return out


async def get(
    db: AsyncSession,
    name: str,
    registry: dict[str, Flag] | None = None,
) -> FlagView | None:
    """One flag's declaration + state, or ``None`` when undeclared."""
    registry = REGISTRY if registry is None else registry
    flag = registry.get(name)
    if flag is None:
        return None
    return _view(name, flag, await db.get(FeatureFlagState, name))


async def set_state(
    db: AsyncSession,
    name: str,
    *,
    state: FlagState,
    user_ids: list[uuid.UUID] | None = None,
    role_ids: list[uuid.UUID] | None = None,
    actor: User | None = None,
    registry: dict[str, Flag] | None = None,
) -> FlagView:
    """Set a flag's state and targeting.  Does not commit.

    Raises ``KeyError`` for an undeclared flag: state for something the code
    can never ask about is a mistake worth surfacing, not a row to create.
    """
    registry = REGISTRY if registry is None else registry
    flag = registry.get(name)
    if flag is None:
        raise KeyError(name)

    # Targeting is only meaningful in the targeted state.  Clearing it on the
    # way to on/off keeps the stored row honest, so the UI never shows a stale
    # allowlist attached to a flag that is off for everyone.
    if state is FlagState.TARGETED:
        uids = [str(u) for u in (user_ids or [])]
        rids = [str(r) for r in (role_ids or [])]
    else:
        uids, rids = [], []

    row = await db.get(FeatureFlagState, name)
    if row is None:
        row = FeatureFlagState(name=name)
        db.add(row)
    row.state = state.value
    row.user_ids = uids
    row.role_ids = rids
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by_id = actor.id if actor is not None else None

    await db.flush()
    return _view(name, flag, row)
