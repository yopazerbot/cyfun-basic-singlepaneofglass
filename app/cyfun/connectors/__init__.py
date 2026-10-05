"""Connector registry. A connector is available when its environment variables are set."""

from __future__ import annotations

from .base import Check, Connector, InventoryItem, SyncResult
from .cloudflare import CloudflareConnector
from .github import GitHubConnector
from .microsoft import MicrosoftConnector
from .railway import RailwayConnector

ALL: tuple[type[Connector], ...] = (MicrosoftConnector, GitHubConnector, RailwayConnector, CloudflareConnector)


def registry(settings) -> dict[str, Connector]:
    return {cls.key: cls(settings) for cls in ALL}


__all__ = ["ALL", "Check", "Connector", "InventoryItem", "SyncResult", "registry"]
