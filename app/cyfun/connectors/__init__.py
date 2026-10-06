"""Connector registry. A connector is available when its credentials are set (Settings page or environment)."""

from __future__ import annotations

from .base import Check, Connector, InventoryItem, SyncResult
from .cloudflare import CloudflareConnector
from .github import GitHubConnector
from .microsoft import MicrosoftConnector
from .notion import NotionConnector
from .railway import RailwayConnector

ALL: tuple[type[Connector], ...] = (MicrosoftConnector, GitHubConnector, RailwayConnector, CloudflareConnector, NotionConnector)


def registry(config) -> dict[str, Connector]:
    return {cls.key: cls(config) for cls in ALL}


__all__ = ["ALL", "Check", "Connector", "InventoryItem", "SyncResult", "registry"]
