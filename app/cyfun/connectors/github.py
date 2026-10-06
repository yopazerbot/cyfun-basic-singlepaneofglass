"""GitHub connector (REST API v3, personal access token or fine-grained token).

Organisation mode (GITHUB_ORG set): repositories, members, two-factor requirement,
Dependabot alerts, branch protection of default branches.
User mode (no GITHUB_ORG): repositories owned by the token's user, two-factor status
of that user, Dependabot alerts per repository.
"""

from __future__ import annotations

import httpx

from .base import FAIL, INFO, PASS, WARN, Check, CheckSpec, Connector, InventoryItem, SyncResult

API = "https://api.github.com"
MAX_REPOS = 200

TWO_FACTOR = CheckSpec("github-2fa", "Two-factor authentication enforced", ("PR.AA-03.2", "PR.AA-01.1"))
DEPENDABOT = CheckSpec("github-dependabot", "Open dependency vulnerability alerts", ("ID.RA-01.1", "ID.AM-08.2"))
BRANCH_PROTECTION = CheckSpec("github-branch-protection", "Default branches protected", ("PR.AA-05.1", "PR.AA-05.3"))
REPOS = CheckSpec("github-repos", "Repository inventory", ("ID.AM-02.1",))
ADMINS = CheckSpec("github-admins", "Organisation owners", ("PR.AA-05.4", "PR.AA-05.3"))


def evaluate_two_factor(enabled: bool | None, scope: str) -> Check:
    if enabled is None:
        return TWO_FACTOR.result(WARN, f"Could not read the two-factor setting for {scope} (token lacks the admin:org or user scope).")
    if enabled:
        return TWO_FACTOR.result(PASS, f"Two-factor authentication is required for {scope}.", {"enabled": True})
    return TWO_FACTOR.result(FAIL, f"Two-factor authentication is not required for {scope}.", {"enabled": False})


def evaluate_dependabot(alerts: list[dict], scope: str) -> Check:
    by_sev: dict[str, int] = {}
    for a in alerts:
        sev = ((a.get("security_advisory") or {}).get("severity") or (a.get("security_vulnerability") or {}).get("severity") or "unknown").lower()
        by_sev[sev] = by_sev.get(sev, 0) + 1
    details = {"open_alerts": len(alerts), "by_severity": by_sev}
    crit = by_sev.get("critical", 0) + by_sev.get("high", 0)
    if not alerts:
        return DEPENDABOT.result(PASS, f"No open Dependabot alerts in {scope}.", details)
    if crit:
        return DEPENDABOT.result(FAIL, f"{len(alerts)} open Dependabot alerts in {scope}, {crit} critical or high.", details)
    return DEPENDABOT.result(WARN, f"{len(alerts)} open Dependabot alerts in {scope} (none critical or high).", details)


def evaluate_branch_protection(results: dict[str, bool | None]) -> Check:
    unprotected = [r for r, v in results.items() if v is False]
    unknown = [r for r, v in results.items() if v is None]
    details = {"checked": len(results), "unprotected": unprotected, "unknown": unknown}
    if not results:
        return BRANCH_PROTECTION.result(INFO, "No active repositories to check.", details)
    if unprotected:
        return BRANCH_PROTECTION.result(WARN, f"{len(unprotected)} of {len(results)} active repositories have no protection on the default branch.", details)
    return BRANCH_PROTECTION.result(PASS, f"All {len(results)} active repositories protect their default branch.", details)


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
            login = self.get_json(c, "/user").get("login", "")
            if org:
                self.get_json(c, f"/orgs/{org}")
                return f"Token works for {login}; organisation {org} is readable."
        return f"Token works for {login} (user mode)."

    @classmethod
    def _paged(cls, c: httpx.Client, url: str, limit: int = 1000) -> list[dict]:
        items: list[dict] = []
        page = 1
        while len(items) < limit:
            batch = cls.get_json(c, url, {"per_page": 100, "page": page})
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
        raw: dict = {"mode": "organisation" if org else "user", "org": org}
        with self._client() as c:
            # Scope and 2FA -----------------------------------------------------------
            scope = f"organisation {org}" if org else "the token user"
            try:
                if org:
                    o = self.get_json(c, f"/orgs/{org}")
                    two_fa = o.get("two_factor_requirement_enabled")
                    raw["organisation"] = {"login": o.get("login"), "name": o.get("name")}
                else:
                    u = self.get_json(c, "/user")
                    two_fa = u.get("two_factor_authentication")
                    raw["user"] = {"login": u.get("login")}
                    scope = f"user {u.get('login')}"
                res.checks.append(evaluate_two_factor(two_fa, scope))
            except Exception as exc:
                res.checks.append(TWO_FACTOR.error(exc))

            # Repositories ------------------------------------------------------------
            active: list[dict] = []
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
                private = sum(1 for r in repos if r.get("private"))
                raw["repositories"] = {"total": len(repos), "active": len(active), "private": private}
                res.checks.append(REPOS.result(INFO, f"{len(repos)} repositories ({len(active)} active, {private} private).", raw["repositories"]))
            except Exception as exc:
                res.checks.append(REPOS.error(exc))

            # Branch protection -------------------------------------------------------
            try:
                results: dict[str, bool | None] = {}
                for r in active[:100]:
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
                res.checks.append(BRANCH_PROTECTION.error(exc))

            # Dependabot alerts -------------------------------------------------------
            try:
                alerts: list[dict] = []
                if org:
                    alerts = self._paged(c, f"/orgs/{org}/dependabot/alerts?state=open", 2000)
                else:
                    for r in active[:100]:
                        resp = c.get(f"/repos/{r['full_name']}/dependabot/alerts", params={"state": "open", "per_page": 100})
                        if resp.status_code == 200:
                            alerts.extend(resp.json())
                raw["dependabot"] = {"open": len(alerts)}
                res.checks.append(evaluate_dependabot(alerts, scope))
            except Exception as exc:
                res.checks.append(DEPENDABOT.error(exc))

            # Organisation admins -----------------------------------------------------
            if org:
                try:
                    admins = self._paged(c, f"/orgs/{org}/members?role=admin")
                    members = self._paged(c, f"/orgs/{org}/members")
                    raw["members"] = {"total": len(members), "admins": [a.get("login") for a in admins]}
                    if members and len(admins) == len(members) and len(members) > 1:
                        res.checks.append(ADMINS.result(WARN, f"All {len(members)} members are organisation owners; restrict ownership.", raw["members"]))
                    else:
                        res.checks.append(ADMINS.result(INFO, f"{len(admins)} owners out of {len(members)} members.", raw["members"]))
                except Exception as exc:
                    res.checks.append(ADMINS.error(exc))

        res.raw = raw
        return res
