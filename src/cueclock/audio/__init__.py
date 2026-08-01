"""Voice control: the word "extension" grants a shot-clock extension.

`spotter` is pure (no vosk/sounddevice imports) so its tests run everywhere;
all mic and model I/O lives in `listener`, exercised live like the camera.
Design: docs/hardware.md Part D.
"""
