"""Railway connector (public GraphQL API). Inventory of projects, environments and services."""

from __future__ import annotations

import httpx

from .base import INFO, CheckSpec, Connector, InventoryItem, SyncResult

ENDPOINT = "https://backboard.railway.com/graphql/v2"

PROJECT_FIELDS = """
  id name createdAt updatedAt
  environments { edges { node { id name } } }
  services { edges { node { id name createdAt updatedAt } } }
"""

QUERY_ME = "query { me { id email projects { edges { node { " + PROJECT_FIELDS + " } } } } }"
QUERY_ROOT = "query { projects { edges { node { " + PROJECT_FIELDS + " } } } }"


INVENTORY = CheckSpec("railway-inventory", "Hosted services inventory", ("ID.AM-01.1", "ID.AM-02.1"))


def _edges(obj: dict | None, key: str) -> list[dict]:
    return [e["node"] for e in ((obj or {}).get(key) or {}).get("edges", []) if e.get("node")]


class RailwayConnector(Connector):
    key = "railway"
    name = "Railway"
    description = "Inventory of hosted projects, environments and services."
    env_vars = ("RAILWAY_TOKEN",)
    docs = "docs/connectors.md#railway"

    def configured(self) -> bool:
        return bool(self.config.railway_token)

    def _client(self) -> httpx.Client:
        return self.client({"Authorization": f"Bearer {self.config.railway_token}", "Content-Type": "application/json"})

    @staticmethod
    def _query(c: httpx.Client, query: str) -> dict:
        r = c.post(ENDPOINT, json={"query": query})
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "") for e in data["errors"])[:300])
        return data.get("data") or {}

    def _projects(self, c: httpx.Client) -> tuple[str, list[dict]]:
        """Owner e-mail (empty for team tokens) and the visible projects."""
        try:
            me = self._query(c, QUERY_ME).get("me")
            return (me or {}).get("email") or "", _edges(me, "projects")
        except Exception:  # noqa: BLE001 - team tokens have no "me"; try the root query
            return "", _edges(self._query(c, QUERY_ROOT), "projects")

    def test(self) -> str:
        with self._client() as c:
            _, projects = self._projects(c)
        return f"Token works; {len(projects)} projects visible."

    def sync(self) -> SyncResult:
        res = SyncResult()
        with self._client() as c:
            try:
                owner, projects = self._projects(c)
                n_services = 0
                n_envs = 0
                raw_projects = []
                for p in projects:
                    envs = _edges(p, "environments")
                    services = _edges(p, "services")
                    n_envs += len(envs)
                    n_services += len(services)
                    raw_projects.append({"name": p["name"], "services": [s["name"] for s in services]})
                    res.inventory.append(
                        InventoryItem(
                            "cloud",
                            f"Railway project {p['name']}",
                            f"project:{p['id']}",
                            f"{len(services)} services, {len(envs)} environments",
                            "Railway",
                            {"environments": [e["name"] for e in envs], "updated_at": (p.get("updatedAt") or "")[:10]},
                        )
                    )
                    for s in services:
                        res.inventory.append(
                            InventoryItem(
                                "service",
                                f"{p['name']} / {s['name']}",
                                f"service:{s['id']}",
                                "Railway service",
                                "Railway",
                                {"project": p["name"], "updated_at": (s.get("updatedAt") or "")[:10]},
                            )
                        )
                res.raw = {"owner": owner, "projects": raw_projects}
                res.checks.append(
                    INVENTORY.result(
                        INFO,
                        f"{len(projects)} projects, {n_services} services, {n_envs} environments on Railway.",
                        {"projects": len(projects), "services": n_services, "environments": n_envs},
                    )
                )
            except Exception as exc:
                res.checks.append(INVENTORY.error(exc, ["ID.AM-02.1"]))
        return res
