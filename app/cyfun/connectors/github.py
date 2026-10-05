"""GitHub connector (REST API v3, personal access token or fine-grained token).

Organisation mode (GITHUB_ORG set): repositories, members, two-factor requirement,
Dependabot alerts, branch protection of default branches.
User mode (no GITHUB_ORG): repositories owned by the token's user, two-factor status
of that user, Dependabot alerts per repository.
"""

from __future__ import annotations

import httpx

from .base import FAIL, INFO, PASS, WARN, Check, Connector, InventoryItem, SyncResult

API = "https://api.github.com"
MAX_REPOS = 200


def evaluate_two_factor(enabled: bool | None, scope: str) -> Check:
    rid = ["PR.AA-03.2", "PR.AA-01.1"]
    if enabled is None:
        return Check(
            "github-2fa",
            "Two-factor authentication enforced",
            WARN,
            f"Could not read the two-factor setting for {scope} (token lacks the admin:org or user scope).",
            rid,
            {},
        )
    if enabled:
        return Check("github-2fa", "Two-factor authentication enforced", PASS, f"Two-factor authentication is required for {scope}.", rid, {"enabled": True})
    return Check("github-2fa", "Two-factor authentication enforced", FAIL, f"Two-factor authentication is not required for {scope}.", rid, {"enabled": False})


def evaluate_dependabot(alerts: list[dict], scope: str) -> Check:
    rid = ["ID.RA-01.1", "ID.AM-08.2"]
    by_sev: dict[str, int] = {}
    for a in alerts:
        sev = ((a.get("security_advisory") or {}).get("severity") or (a.get("security_vulnerability") or {}).get("severity") or "unknown").lower()
        by_sev[sev] = by_sev.get(sev, 0) + 1
    details = {"open_alerts": len(alerts), "by_severity": by_sev}
    crit = by_sev.get("critical", 0) + by_sev.get("high", 0)
    if not alerts:
        return Check("github-dependabot", "Open dependency vulnerability alerts", PASS, f"No open Dependabot alerts in {scope}.", rid, details)
    if crit:
        return Check(
            "github-dependabot",
            "Open dependency vulnerability alerts",
            FAIL,
            f"{len(alerts)} open Dependabot alerts in {scope}, {crit} critical or high.",
            rid,
            details,
        )
    return Check(
        "github-dependabot",
        "Open dependency vulnerability alerts",
        WARN,
        f"{len(alerts)} open Dependabot alerts in {scope} (none critical or high).",
        rid,
        details,
    )


def evaluate_branch_protection(results: dict[str, bool | None]) -> Check:
    rid = ["PR.AA-05.1", "PR.AA-05.3"]
    unprotected = [r for r, v in results.items() if v is False]
    unknown = [r for r, v in results.items() if v is None]
    details = {"checked": len(results), "unprotected": unprotected, "unknown": unknown}
    if not results:
        return Check("github-branch-protection", "Default branches protected", INFO, "No active repositories to check.", rid, details)
    if unprotected:
        return Check(
            "github-branch-protection",
            "Default branches protected",
            WARN,
            f"{len(unprotected)} of {len(results)} active repositories have no protection on the default branch.",
            rid,
            details,
        )
    return Check(
        "github-branch-protection", "Default branches protected", PASS, f"All {len(results)} active repositories protect their default branch.", rid, details
    )


class GitHubConnector(Connector):
    key = "github"
    name = "GitHub"
    description = "Repository inventory, two-factor enforcement, Dependabot alerts, branch protection, organisation admins."
    env_vars = ("GITHUB_TOKEN", "GITHUB_ORG")
    docs = "docs/connectors.md#github"

    def configured(self) -> bool:
        return bool(self.config.github_token)

    def _client(self) -> httpx.Client:
        return self.client(
            {
                "Authorization": f"Bearer {self.config.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cyfun-basic-spog",
            },
            API,
        )

    def test(self) -> str:
        org = self.config.github_org.strip()
        with self._client() as c:
            u = c.get("/user")
            u.raise_for_status()
            login = u.json().get("login", "")
            if org:
                o = c.get(f"/orgs/{org}")
                o.raise_for_status()
                return f"Token works for {login}; organisation {org} is readable."
        return f"Token works for {login} (user mode)."

    @staticmethod
    def _paged(c: httpx.Client, url: str, limit: int = 1000) -> list[dict]:
        items: list[dict] = []
        page = 1
        while len(items) < limit:
            r = c.get(url, params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def sync(self) -> SyncResult:
        res = SyncResult()
        org = self.config.github_org.strip()
        c = self._client()
        raw: dict = {"mode": "organisation" if org else "user", "org": org}

        # Scope and 2FA ---------------------------------------------------------------
        two_fa: bool | None = None
        scope = f"organisation {org}" if org else "the token user"
        try:
            if org:
                o = c.get(f"/orgs/{org}")
                o.raise_for_status()
                two_fa = o.json().get("two_factor_requirement_enabled")
                raw["organisation"] = {"login": o.json().get("login"), "name": o.json().get("name")}
            else:
                u = c.get("/user")
                u.raise_for_status()
                two_fa = u.json().get("two_factor_authentication")
                raw["user"] = {"login": u.json().get("login")}
                scope = f"user {u.json().get('login')}"
            res.checks.append(evaluate_two_factor(two_fa, scope))
        except Exception as exc:
            res.checks.append(self.error_check("github-2fa", "Two-factor authentication enforced", ["PR.AA-03.2"], exc))

        # Repositories ----------------------------------------------------------------
        repos: list[dict] = []
        try:
            repos = self._paged(c, f"/orgs/{org}/repos?type=all" if org else "/user/repos?affiliation=owner", MAX_REPOS)
            for r in repos:
                res.inventory.append(
                    InventoryItem(
                        "software",
                        r.get("full_name", ""),
                        f"repo:{r.get('id')}",
                        (r.get("description") or "")[:300],
                        "GitHub",
                        {
                            "private": r.get("private"),
                            "archived": r.get("archived"),
                            "default_branch": r.get("default_branch"),
                            "language": r.get("language"),
                            "pushed_at": (r.get("pushed_at") or "")[:10],
                            "url": r.get("html_url"),
                        },
                    )
                )
            active = [r for r in repos if not r.get("archived")]
            raw["repositories"] = {"total": len(repos), "active": len(active), "private": sum(1 for r in repos if r.get("private"))}
            res.checks.append(
                Check(
                    "github-repos",
                    "Repository inventory",
                    INFO,
                    f"{len(repos)} repositories ({len(active)} active, {raw['repositories']['private']} private).",
                    ["ID.AM-02.1"],
                    raw["repositories"],
                )
            )
        except Exception as exc:
            res.checks.append(self.error_check("github-repos", "Repository inventory", ["ID.AM-02.1"], exc))

        # Branch protection -----------------------------------------------------------
        try:
            results: dict[str, bool | None] = {}
            for r in [r for r in repos if not r.get("archived")][:100]:
                owner, name = r["full_name"].split("/", 1)
                branch = r.get("default_branch") or "main"
                resp = c.get(f"/repos/{owner}/{name}/branches/{branch}/protection")
                if resp.status_code == 200:
                    results[r["full_name"]] = True
                elif resp.status_code == 404:
                    rules = c.get(f"/repos/{owner}/{name}/rules/branches/{branch}")
                    results[r["full_name"]] = bool(rules.status_code == 200 and rules.json())
                else:
                    results[r["full_name"]] = None
            raw["branch_protection"] = results
            res.checks.append(evaluate_branch_protection(results))
        except Exception as exc:
            res.checks.append(self.error_check("github-branch-protection", "Default branches protected", ["PR.AA-05.1"], exc))

        # Dependabot alerts -----------------------------------------------------------
        try:
            alerts: list[dict] = []
            if org:
                alerts = self._paged(c, f"/orgs/{org}/dependabot/alerts?state=open", 2000)
            else:
                for r in [r for r in repos if not r.get("archived")][:100]:
                    resp = c.get(f"/repos/{r['full_name']}/dependabot/alerts", params={"state": "open", "per_page": 100})
                    if resp.status_code == 200:
                        alerts.extend(resp.json())
            raw["dependabot"] = {"open": len(alerts)}
            res.checks.append(evaluate_dependabot(alerts, scope))
        except Exception as exc:
            res.checks.append(self.error_check("github-dependabot", "Open dependency vulnerability alerts", ["ID.RA-01.1"], exc))

        # Organisation admins ---------------------------------------------------------
        if org:
            try:
                admins = self._paged(c, f"/orgs/{org}/members?role=admin")
                members = self._paged(c, f"/orgs/{org}/members")
                names = [a.get("login") for a in admins]
                raw["members"] = {"total": len(members), "admins": names}
                rid = ["PR.AA-05.4", "PR.AA-05.3"]
                if members and len(admins) == len(members) and len(members) > 1:
                    res.checks.append(
                        Check(
                            "github-admins",
                            "Organisation owners",
                            WARN,
                            f"All {len(members)} members are organisation owners; restrict ownership.",
                            rid,
                            raw["members"],
                        )
                    )
                else:
                    res.checks.append(
                        Check("github-admins", "Organisation owners", INFO, f"{len(admins)} owners out of {len(members)} members.", rid, raw["members"])
                    )
            except Exception as exc:
                res.checks.append(self.error_check("github-admins", "Organisation owners", ["PR.AA-05.4"], exc))

        res.raw = raw
        c.close()
        return res
