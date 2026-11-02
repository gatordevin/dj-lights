"""Beam photometry for aiming lights: how much light a fixture puts on a target (lux) and what a target needs (lumens).
Model: a fixture puts `lumens` uniformly into a cone of full angle `beam_deg` (the "field"/beam angle on spec sheets).
Python (SI inside; feet helpers given):
  solid_angle(beam_deg) -> sr                 2π(1 - cos(θ/2))
  intensity(lumens, beam_deg) -> cd           lumens / solid_angle
  illuminance(lumens, beam_deg, distance_m, incidence_deg=0) -> lux     I·cos(incidence)/d²
  spot_diameter(beam_deg, distance_m) -> m    2·d·tan(θ/2)
  lumens_needed(lux, beam_deg, distance_m) -> lm   inverse of illuminance (normal incidence)
  colour_fraction(name) -> float              share of a white-mix's lumens left when an RGB fixture shows one saturated
       colour, from Rec.709 luminance weights (R .2126, G .7152, B .0722): "white" 1, "green" .72, "amber" .57
       (R + G/2), "red" .21, "purple" .28 (R + B), "blue" .07, "cyan" .79, "magenta" .28
  ft(x) / m(x)                                feet -> metres / metres -> feet
Contract: ValueError for beam_deg outside (0, 180], non-positive distance/lumens, unknown colour names.
Limits: real beams are brighter in the centre (peak ≈ 1.5-2× this average for LED pars) and have soft edges; foliage
  reflects ~10-20% and is not a flat surface; marketing lumens ("800W equivalent", "10000LM") are usually white-only or
  inflated — treat them as upper bounds. Good for comparing fixtures and sizing, not for lighting-design certification.
Used by: tasks/halloween_lighting.py. Depends on: math."""
import math

COLOURS = {"white": 1.0, "green": 0.7152, "amber": 0.2126 + 0.7152 / 2, "red": 0.2126, "purple": 0.2126 + 0.0722,
           "blue": 0.0722, "cyan": 0.7152 + 0.0722, "magenta": 0.2126 + 0.0722}


def _beam(beam_deg):
    if not isinstance(beam_deg, (int, float)) or not 0 < beam_deg <= 180: raise ValueError(f"beam_deg must be in (0, 180]: {beam_deg!r}")
    return math.radians(beam_deg) / 2


def _pos(name, v):
    if not isinstance(v, (int, float)) or v <= 0: raise ValueError(f"{name} must be > 0: {v!r}")


def solid_angle(beam_deg):
    return 2 * math.pi * (1 - math.cos(_beam(beam_deg)))


def intensity(lumens, beam_deg):
    _pos("lumens", lumens); return lumens / solid_angle(beam_deg)


def illuminance(lumens, beam_deg, distance_m, incidence_deg=0.0):
    _pos("distance_m", distance_m)
    return intensity(lumens, beam_deg) * math.cos(math.radians(incidence_deg)) / distance_m ** 2


def spot_diameter(beam_deg, distance_m):
    _pos("distance_m", distance_m); return 2 * distance_m * math.tan(_beam(beam_deg)) if beam_deg < 180 else float("inf")


def lumens_needed(lux, beam_deg, distance_m):
    _pos("lux", lux); _pos("distance_m", distance_m); return lux * solid_angle(beam_deg) * distance_m ** 2


def colour_fraction(name):
    try: return COLOURS[name]
    except KeyError: raise ValueError(f"unknown colour {name!r}; known: {sorted(COLOURS)}") from None


def ft(x): return x * 0.3048
def m(x): return x / 0.3048
