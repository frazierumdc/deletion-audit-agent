"""Connectors and restores that re-created one of the case's resources ('repopulated downstream resource X')."""

import re

from deletion_audit.context import CaseView
from deletion_audit.linking.direct import add_link

_REPOP_RE = re.compile(r"repopulated downstream resource (\S+?)\.?$")


def repopulation_links(view: CaseView, links: dict) -> None:
    """Add the source process of any in-window repopulation of a linked resource."""
    for a in view.activity():
        m = _REPOP_RE.search(a.details)
        if m and m.group(1) in links:
            add_link(links, a.resource_id, f"repopulates:{a.activity_id}")
