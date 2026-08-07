"""FargoRate player search for the scorecard (issue #27).

A thin server-side proxy over the public dashboard search endpoint. It lives
on the server rather than in the control page because dashboard.fargorate.com
sends no CORS headers, so a browser can't call it directly. The rows are
normalized down to the four fields the operator needs to pick the right
person: name, location (the human disambiguator — "John Smith" returns 50
players), the effective rating, and robustness (games recorded; FargoRate
treats under ``ESTABLISHED_ROBUSTNESS`` as provisional, which the control
page surfaces so a 10-game rating isn't read as an established one).
"""

import httpx

SEARCH_URL = "https://dashboard.fargorate.com/api/indexsearch"
TIMEOUT_S = 5.0
# FargoRate's own threshold: ratings become "established" at 200 recorded
# games; below that the dashboard shows them as provisional.
ESTABLISHED_ROBUSTNESS = 200


async def _fetch(q: str) -> dict[str, object]:
    """The raw upstream call — split out so tests can fake the wire."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(SEARCH_URL, params={"q": q})
        resp.raise_for_status()
        data: dict[str, object] = resp.json()
        return data


async def search(q: str) -> list[dict[str, object]]:
    """Search FargoRate for players matching ``q``.

    Returns normalized rows ``{name, location, rating, established}``.
    Rows without a parseable rating are dropped — there is nothing to
    display for them. Raises ``httpx.HTTPError`` on any upstream failure;
    the endpoint turns that into a 502.
    """
    data = await _fetch(q)
    rows = data.get("value")
    players: list[dict[str, object]] = []
    if not isinstance(rows, list):
        return players
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = f"{row.get('firstName') or ''} {row.get('lastName') or ''}".strip()
        try:
            rating = int(float(str(row.get("effectiveRating"))))
            robustness = int(float(str(row.get("robustness") or 0)))
        except ValueError:
            continue
        if not name:
            continue
        players.append(
            {
                "name": name,
                "location": str(row.get("location") or ""),
                "rating": rating,
                "established": robustness >= ESTABLISHED_ROBUSTNESS,
            }
        )
    return players
