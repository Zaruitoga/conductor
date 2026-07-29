"""
model/ — The interpretation layer: sensor packets → artistic signals.

Everything downstream of the wire and upstream of the outputs lives here.  The
package is deliberately free of I/O: it is fed samples and it publishes objects.
That is what lets the same code run live, replayed at 4×, or in a batch bench,
with byte-identical results.

  clock.py       the model's only time source (unwrapped ESP micros)
  types.py       Frame / Event / Meta — what the model emits
  bus.py         in-process fan-out, per-subscriber loss policy
"""
