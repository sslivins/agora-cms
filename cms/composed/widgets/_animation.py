"""Shared animated-text-effect allowlist + CSS builder.

Whole-text "motion" effects (iMessage-style: Big, Nod, Shake, …) for any
text-bearing widget.  Like ``_FONT_STACKS`` in :mod:`text`, the catalog
lives server-side so a malicious config can never inject raw CSS /
``@keyframes`` into a bundle — config only ever carries an *effect slug*
and a *speed slug*, both validated against the allowlists below.

Each effect is expressed purely with ``transform`` / ``opacity`` /
``filter`` / ``text-shadow`` / ``background-clip`` so it stays on the
Chromium GPU compositor on the Pi 5.  Effects loop continuously
(``infinite``) — signage mode.

Scoping rule (mirrors the per-instance CSS-class rule the rest of the
composed widgets follow): :func:`build_animation_css` scopes the
``@keyframes`` name by the caller-supplied ``instance_id`` so two
instances using the same effect never collide.

The editor's live in-browser preview mirrors this catalog in
``composed_editor.html`` (``cw-fx-*`` classes + ``_ANIM_SPEED_FACTORS``);
keep the two in sync — base durations and speed factors must match so the
WYSIWYG preview matches the published bundle.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Speed control ────────────────────────────────────────────────────
# A multiplier applied to each effect's base duration.  Larger factor =
# longer duration = slower motion.
_ANIM_SPEED_FACTORS: dict[str, float] = {
    "slow": 1.6,
    "normal": 1.0,
    "fast": 0.6,
}

ANIMATION_SPEEDS: tuple[str, ...] = ("slow", "normal", "fast")


@dataclass(frozen=True)
class _Effect:
    """One whole-text effect spec.

    ``frames`` is the body of the ``@keyframes`` block (the percentage
    rules).  ``extra`` is appended to the animated element's rule (e.g.
    a shimmer gradient or a glow base colour).  ``needs_3d`` flags the
    effects that rotate in Z and therefore want ``perspective`` on the
    containing box.
    """

    duration: float          # base duration, seconds (speed=normal)
    timing: str              # CSS timing-function
    frames: str              # @keyframes body
    extra: str = ""          # extra declarations on the animated element
    needs_3d: bool = False


# Allowlist of effect slug → spec.  "none" is implicit (absence of an
# entry / the default config value) and emits nothing.
_EFFECTS: dict[str, _Effect] = {
    "big": _Effect(
        duration=2.8,
        timing="ease-in-out",
        frames=(
            "0%,100%{transform:scale(1);}"
            "15%{transform:scale(1.45);}"
            "30%{transform:scale(0.92);}"
            "45%{transform:scale(1.08);}"
            "60%{transform:scale(1);}"
        ),
    ),
    "nod": _Effect(
        duration=2.8,
        timing="ease-in-out",
        frames=(
            "0%,100%{transform:rotateX(0deg);}"
            "20%{transform:rotateX(55deg);}"
            "40%{transform:rotateX(-15deg);}"
            "55%{transform:rotateX(8deg);}"
            "70%{transform:rotateX(0deg);}"
        ),
        needs_3d=True,
    ),
    "shake": _Effect(
        duration=0.9,
        timing="linear",
        frames=(
            "0%,100%{transform:translate(0,0) rotate(0deg);}"
            "10%{transform:translate(-3px,1px) rotate(-2deg);}"
            "20%{transform:translate(3px,-1px) rotate(2deg);}"
            "30%{transform:translate(-3px,0) rotate(-1deg);}"
            "40%{transform:translate(3px,1px) rotate(1.5deg);}"
            "50%{transform:translate(-2px,-1px) rotate(-1.5deg);}"
            "60%{transform:translate(2px,1px) rotate(1deg);}"
            "70%{transform:translate(-2px,0) rotate(-1deg);}"
            "80%{transform:translate(2px,-1px) rotate(1deg);}"
            "90%{transform:translate(-1px,1px) rotate(-0.5deg);}"
        ),
    ),
    "pulse": _Effect(
        duration=1.8,
        timing="ease-in-out",
        frames=(
            "0%,100%{transform:scale(1);opacity:0.85;}"
            "50%{transform:scale(1.12);opacity:1;}"
        ),
    ),
    "float": _Effect(
        duration=2.4,
        timing="ease-in-out",
        frames=(
            "0%,100%{transform:translateY(8px);}"
            "50%{transform:translateY(-8px);}"
        ),
    ),
    "glow": _Effect(
        duration=1.8,
        timing="ease-in-out",
        frames=(
            "0%,100%{text-shadow:0 0 4px rgba(124,131,255,0.4);}"
            "50%{text-shadow:0 0 18px rgba(124,131,255,0.95),"
            "0 0 36px rgba(124,131,255,0.6);}"
        ),
    ),
    "shimmer": _Effect(
        duration=2.6,
        timing="linear",
        frames="0%{background-position:-200% 0;}100%{background-position:200% 0;}",
        extra=(
            "background:linear-gradient(100deg,"
            "currentColor 0%,currentColor 38%,#7c83ff 50%,"
            "currentColor 62%,currentColor 100%);"
            "background-size:200% auto;"
            "-webkit-background-clip:text;background-clip:text;"
            "-webkit-text-fill-color:transparent;"
        ),
    ),
    "flip": _Effect(
        duration=3.2,
        timing="ease-in-out",
        frames="0%{transform:rotateY(0deg);}100%{transform:rotateY(360deg);}",
        needs_3d=True,
    ),
    "neon": _Effect(
        duration=3.0,
        timing="linear",
        frames=(
            "0%,19%,21%,23%,80%,100%{opacity:1;"
            "text-shadow:0 0 6px #ff4ecd,0 0 14px #ff4ecd,0 0 28px #b026ff;}"
            "20%,22%,60%{opacity:0.55;text-shadow:none;}"
        ),
    ),
    "bloom": _Effect(
        duration=3.0,
        timing="ease-out",
        frames=(
            "0%{transform:scale(0.6);filter:blur(14px);opacity:0;}"
            "40%{transform:scale(1.06);filter:blur(0);opacity:1;}"
            "70%{transform:scale(1);}"
            "100%{transform:scale(1);filter:blur(0);opacity:1;}"
        ),
    ),
}


ANIMATIONS: tuple[str, ...] = ("none", *_EFFECTS.keys())


def is_valid_animation(slug: str) -> bool:
    return slug == "none" or slug in _EFFECTS


def is_valid_speed(slug: str) -> bool:
    return slug in _ANIM_SPEED_FACTORS


def animation_needs_3d(slug: str) -> bool:
    """True if the effect rotates in 3D and wants ``perspective`` on the
    containing box.  Safe to call with ``"none"`` / unknown (→ False)."""
    eff = _EFFECTS.get(slug)
    return bool(eff and eff.needs_3d)


@dataclass(frozen=True)
class AnimationCSS:
    css: str            # scoped @keyframes + the rule on ``anim_selector``
    needs_3d: bool      # caller should add perspective to the box


def build_animation_css(
    slug: str,
    *,
    instance_id: str,
    anim_selector: str,
    speed: str = "normal",
) -> AnimationCSS | None:
    """Build the scoped CSS for an animated text element.

    ``anim_selector`` is the CSS selector for the element that should
    animate (e.g. ``".cw-text-anim-<id>"``).  Returns ``None`` for
    ``"none"`` / unknown slugs so callers emit nothing in the default
    (byte-identical) path.
    """
    eff = _EFFECTS.get(slug)
    if eff is None:
        return None

    factor = _ANIM_SPEED_FACTORS.get(speed, 1.0)
    duration = round(eff.duration * factor, 3)
    kf_name = f"cw-kf-{instance_id}"

    css = (
        f"@keyframes {kf_name} {{{eff.frames}}}\n"
        f"{anim_selector} {{\n"
        f"  animation: {kf_name} {duration}s {eff.timing} infinite;\n"
    )
    if eff.extra:
        css += f"  {eff.extra}\n"
    css += "}"

    return AnimationCSS(css=css, needs_3d=eff.needs_3d)
