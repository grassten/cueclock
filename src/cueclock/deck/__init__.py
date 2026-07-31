"""Stream Deck control surface (docs/stream-deck.md).

`layout`, `faces`, and `client` are pure (no hardware imports) so their tests
run everywhere; all HID I/O lives in `daemon`, exercised live like the camera.
"""
