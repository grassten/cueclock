"""Stream Deck control surface (docs/hardware.md Part C).

`layout`, `faces`, and `client` are pure (no hardware imports) so their tests
run everywhere; all HID I/O lives in `daemon`, exercised live like the camera.
"""
