"""
osc/ — Bus → Ableton Live, remapped by data rather than by code.

  targets.py   the catalog of known AbletonOSC destinations — pure data
  routes.py    the mapping table: signal/detector → target, CRUD, named
               profiles under mappings/ (parallel to model/params.py)
  bridge.py    the bus subscriber: transform, rate cap, deadband, send
  live.py      the AbletonOSC conversation itself — send, reply socket,
               health probe, discovery of track/device/parameter names

Nothing here is imported by model/ — the bridge subscribes to the bus like any
other output (model/bus.py), and never the other way around.
"""
