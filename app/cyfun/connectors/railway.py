"""Railway connector (public GraphQL API). Inventory of projects, environments and services."""

from __future__ import annotations

from .base import INFO, Check, Connector, InventoryItem, SyncResult

ENDPOINT = "https://backboard.railway.com/graphql/v2"

PROJECT_FIELDS = """
  id name createdAt updatedAt
  environments { edges { node { id name } } }
  services { edges { node { id name createdAt updatedAt } } }
"""

QUERY_ME = "query { me { id email projects { edges { node { " + PROJECT_FIELDS + " } } } } }"
QUERY_ROOT = "query { projects { edges { node { " + PROJECT_FIELDS + " } } } }"


def _edges(obj: dict | None, key: str) -> list[dict]:
    return [e["node"] for e in ((obj or {}).get(key) or {}).get("edges", []) if e.get("node")]


class RailwayConnector(Connector):
    key = "railway"
    name = "Railway"
    description = "Inventory of hosted projects, environments and services."
    env_vars = ("RAILWAY_TOKEN",)
    docs = "docs/connectors.md#railway"

    def configured(self) -> bool:
        return bool(self.settings.railway_token)

    def _query(self, c, query: str) -> dict:
        r = c.post(ENDPOINT, json={"query": query})
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "") for e in data["errors"])[:300])
        return data.get("data") or {}

    def sync(self) -> SyncResult:
        res = SyncResult()
        c = self.client({"Authorization": f"Bearer {self.settings.railway_token}", "Content-Type": "application/json"})
        projects: list[dict] = []
        owner = ""
        try:
            try:
                data = self._query(c, QUERY_ME)
                owner = (data.get("me") or {}).get("email") or ""
                projects = _edges(data.get("me"), "projects")
            except Exception:
                data = self._query(c, QUERY_ROOT)
                projects = _edges(data, "projects")
            n_services = 0
            n_envs = 0
            for p in projects:
                envs = _edges(p, "environments")
                services = _edges(p, "services")
                n_envs += len(envs)
                n_services += len(services)
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
            res.raw = {"owner": owner, "projects": [{"name": p["name"], "services": [s["name"] for s in _edges(p, "services")]} for p in projects]}
            res.checks.append(
                Check(
                    "railway-inventory",
                    "Hosted services inventory",
                    INFO,
                    f"{len(projects)} projects, {n_services} services, {n_envs} environments on Railway.",
                    ["ID.AM-01.1", "ID.AM-02.1"],
                    {"projects": len(projects), "services": n_services, "environments": n_envs},
                )
            )
        except Exception as exc:
            res.checks.append(self.error_check("railway-inventory", "Hosted services inventory", ["ID.AM-02.1"], exc))
        c.close()
        return res
