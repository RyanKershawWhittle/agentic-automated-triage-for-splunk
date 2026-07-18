"""Build a clickable Splunk Web deep link from an SPL query + time range.

Produces a URL the on-call engineer can click straight from the triage email to
land in Splunk Search with the exact query and time window pre-loaded.
"""

from __future__ import annotations

from urllib.parse import quote

from . import config


def splunk_search_link(spl: str, earliest: str = "-15m", latest: str = "now") -> str:
    search = spl if spl.lstrip().startswith("|") else f"search {spl}"
    q = quote(search, safe="")
    e = quote(earliest, safe="")
    l = quote(latest, safe="")
    return (
        f"{config.SPLUNK_WEB_BASE}/{config.SPLUNK_WEB_LOCALE}/app/search/search"
        f"?q={q}&earliest={e}&latest={l}"
    )
