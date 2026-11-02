"""lighting/: photometry identities (hemisphere = 2π sr, inverse square, cosine law, lumens_needed inverts illuminance)."""
import sys, math; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check
from lighting import photometry as P

check(abs(P.solid_angle(180) - 2 * math.pi) < 1e-12, "180° beam = hemisphere")
check(abs(P.illuminance(1000, 30, 2) * 4 - P.illuminance(1000, 30, 1)) < 1e-9, "inverse square")
check(abs(P.illuminance(1000, 30, 1, 60) - P.illuminance(1000, 30, 1) / 2) < 1e-9, "cosine law")
e = P.illuminance(2500, 26, P.ft(25))
check(abs(P.lumens_needed(e, 26, P.ft(25)) - 2500) < 1e-6, "lumens_needed inverts illuminance")
check(P.illuminance(3000, 26, 7.6) / P.illuminance(3000, 120, 7.6) > 15, "26° par puts >15× the light of a 120° flood on a far target")
check(abs(P.colour_fraction("blue") - 0.0722) < 1e-9 and abs(P.spot_diameter(90, 1) - 2) < 1e-9, "colour share + spot size")
for bad in (lambda: P.solid_angle(0), lambda: P.illuminance(100, 30, 0), lambda: P.colour_fraction("plaid")):
    try: bad(); check(False, "bad input accepted")
    except ValueError: pass
print("ok: lighting photometry (hemisphere, inverse square, cosine law, inverse, colour shares, contracts)")
