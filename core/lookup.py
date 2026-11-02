"""Named-entry lookup in a catalogue dict: one LookupError that lists what exists.
Every catalogue (materials, rails, tapes, presets, printers, ...) is a dict keyed by name; callers need the entry or a
clear error naming the valid keys.
Python:
  pick(table, key, what="entry") -> table[key]
      table: any Mapping; what: noun for the message ("material", "rail series").
      LookupError("unknown material 'xyz'; known: pla, petg, ...") when key is absent (keys listed in table order).
Contract: TypeError if table is not a Mapping. Never mutates the table; returns the stored value itself (not a copy).
Limits: exact key match only (normalise case/aliases before calling); the message lists at most 40 keys.
Used by: mech/materials.py, mech/hyperelastic.py, mech/sickle.py, cad/linear.py, cad/fab.py, cad/printing.py,
  cad/standards.py, cad/tabslot.py, cad/stock.py, cad/drives.py, robotics/vehicle.py, world/scenes.py,
  cad/flex/base.py. Depends on: nothing."""
from collections.abc import Mapping


def pick(table, key, what="entry"):
    if not isinstance(table, Mapping): raise TypeError(f"pick: table must be a Mapping, got {type(table).__name__}")
    try:
        return table[key]
    except KeyError:
        names = [str(k) for k in table]
        shown = ", ".join(names[:40]) + (f", ... ({len(names)} total)" if len(names) > 40 else "")
        raise LookupError(f"unknown {what} {key!r}; known: {shown or 'none'}") from None
