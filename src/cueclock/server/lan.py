"""The address relatives put in their phones, and the code that saves typing it.

The scoring page is served on the LAN so everybody keeps score on their own
phone with nothing to install (issue #43 §6). The only thing between that and a
room full of people is somebody reading an IP address out loud eleven times, so
the wall shows a QR code of it instead.

**The page cannot work this out for itself.** The bracket overlay is loaded by
OBS as ``http://127.0.0.1:8000/bracket``, so its ``location.host`` is the
loopback address and useless to a phone. The *port* is right — it is the one
port this server listens on — which is why only the host is discovered here and
the port comes off the request.
"""

from __future__ import annotations

import socket

import segno

# Reserved documentation address (RFC 5737 TEST-NET-1). Nothing is sent to it;
# see `lan_host`.
_PROBE = ("192.0.2.1", 9)


def lan_host() -> str | None:
    """This machine's address on the LAN, or ``None`` if it has no route out.

    Connecting a UDP socket transmits nothing — it only asks the routing table
    which interface would carry traffic to that address, which is the interface
    a phone on the same wifi reaches us on. Resolving the hostname instead
    answers ``127.0.0.1`` as often as not, and a QR code of the loopback address
    is a QR code of nothing.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(_PROBE)
            host = str(sock.getsockname()[0])
    except OSError:
        return None  # no network: the room is offline, and the page says so
    return None if host.startswith("127.") else host


def join_url(port: int, path: str = "/control") -> str | None:
    """The scoring page's address as a phone would have to type it."""
    host = lan_host()
    return None if host is None else f"http://{host}:{port}{path}"


def qr_data_uri(text: str) -> str:
    """``text`` as an SVG QR code, inline, ready for an ``<img src>``.

    A data URI rather than a file or an endpoint of its own: the overlay pages
    load nothing from anywhere else, and a QR code that depends on a second
    request is a QR code that can be missing from the wall.
    """
    # Error correction M (15%) is the usual screen default — enough to survive a
    # camera at an angle across a room without the code growing denser than a TV
    # can show. `dark` is the overlay's ink so it looks like it belongs, on white
    # because a scanner needs the contrast and the quiet zone to be light.
    code = segno.make(text, error="m")
    return str(code.svg_data_uri(scale=4, border=2, dark="#0B0E13", light="#FFFFFF"))
