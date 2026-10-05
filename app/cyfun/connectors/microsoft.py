"""Microsoft 365 / Entra ID connector (Microsoft Graph, application permissions).

Required Graph application permissions (admin consent):
  User.Read.All, Directory.Read.All, Policy.Read.All, AuditLog.Read.All,
  Device.Read.All, SecurityEvents.Read.All, Organization.Read.All,
  DeviceManagementManagedDevices.Read.All (optional, Intune)

Some reports need an Entra ID P1 licence (sign-in activity, MFA registration report).
Checks that hit a missing permission or licence are reported as errors, not failures.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from .base import FAIL, INFO, PASS, WARN, Check, Connector, InventoryItem, SyncResult

GRAPH = "https://graph.microsoft.com/v1.0"
GLOBAL_ADMIN_TEMPLATE = "62e90394-69f5-4237-9190-012177145e10"
MICROSOFT_TENANT = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"


def evaluate_ca_policies(policies: list[dict], security_defaults: bool | None) -> Check:
    """Pure evaluation of Conditional Access policies for an MFA-for-all requirement."""
    rid = ["PR.AA-03.2", "GV.RR-04.1"]
    if security_defaults:
        return Check(
            "m365-mfa-policy",
            "MFA enforced for all users",
            PASS,
            "Security defaults are enabled (MFA required for all users).",
            rid,
            {"security_defaults": True},
        )
    strong = []
    partial = []
    for p in policies:
        if p.get("state") != "enabled":
            continue
        grant = p.get("grantControls") or {}
        controls = [c.lower() for c in (grant.get("builtInControls") or [])]
        requires_mfa = "mfa" in controls or bool(grant.get("authenticationStrength"))
        if not requires_mfa:
            continue
        users = (p.get("conditions") or {}).get("users") or {}
        apps = (p.get("conditions") or {}).get("applications") or {}
        all_users = "All" in (users.get("includeUsers") or [])
        all_apps = "All" in (apps.get("includeApplications") or [])
        (strong if all_users and all_apps else partial).append(p.get("displayName"))
    details = {"policies_all_users_all_apps": strong, "policies_partial": partial, "security_defaults": security_defaults}
    if strong:
        return Check(
            "m365-mfa-policy",
            "MFA enforced for all users",
            PASS,
            f"{len(strong)} enabled Conditional Access policy(ies) require MFA for all users and all apps.",
            rid,
            details,
        )
    if partial:
        return Check(
            "m365-mfa-policy",
            "MFA enforced for all users",
            WARN,
            f"MFA policies exist but none covers all users and all applications ({len(partial)} partial).",
            rid,
            details,
        )
    return Check(
        "m365-mfa-policy", "MFA enforced for all users", FAIL, "No enabled Conditional Access policy requires MFA and security defaults are off.", rid, details
    )


def evaluate_mfa_registration(rows: list[dict]) -> Check:
    rid = ["PR.AA-03.2", "PR.AA-01.1"]
    total = len(rows)
    registered = [r for r in rows if r.get("isMfaRegistered")]
    missing = [r.get("userPrincipalName") for r in rows if not r.get("isMfaRegistered")]
    admins_missing = [r.get("userPrincipalName") for r in rows if r.get("isAdmin") and not r.get("isMfaRegistered")]
    details = {"users": total, "mfa_registered": len(registered), "not_registered": missing[:50], "admins_not_registered": admins_missing}
    if total == 0:
        return Check("m365-mfa-registration", "MFA registration coverage", INFO, "No users in the registration report.", rid, details)
    pct = round(100 * len(registered) / total)
    if not missing:
        return Check("m365-mfa-registration", "MFA registration coverage", PASS, f"All {total} users have registered MFA.", rid, details)
    status = FAIL if admins_missing or pct < 90 else WARN
    return Check(
        "m365-mfa-registration",
        "MFA registration coverage",
        status,
        f"{len(registered)} of {total} users ({pct}%) registered MFA; {len(missing)} missing.",
        rid,
        details,
    )


def evaluate_global_admins(members: list[dict]) -> Check:
    rid = ["PR.AA-05.4", "PR.AA-05.3"]
    names = [m.get("userPrincipalName") or m.get("displayName") for m in members]
    details = {"global_administrators": names}
    n = len(names)
    if n == 0:
        return Check("m365-global-admins", "Global Administrator accounts", WARN, "No Global Administrator found (check permissions).", rid, details)
    if n == 1:
        return Check(
            "m365-global-admins", "Global Administrator accounts", WARN, "Only 1 Global Administrator; keep a second emergency access account.", rid, details
        )
    if n <= 4:
        return Check("m365-global-admins", "Global Administrator accounts", PASS, f"{n} Global Administrators (2 to 4 recommended).", rid, details)
    return Check(
        "m365-global-admins", "Global Administrator accounts", FAIL, f"{n} Global Administrators; reduce to at most 4 dedicated admin accounts.", rid, details
    )


def evaluate_stale_accounts(users: list[dict], days: int = 90) -> Check:
    rid = ["PR.AA-01.1", "PR.AA-05.1"]
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stale = []
    unknown = 0
    for u in users:
        if not u.get("accountEnabled"):
            continue
        act = (u.get("signInActivity") or {}).get("lastSignInDateTime")
        if not act:
            unknown += 1
            continue
        last = datetime.fromisoformat(act.replace("Z", "+00:00"))
        if last < cutoff:
            stale.append({"user": u.get("userPrincipalName"), "last_sign_in": act[:10]})
    details = {"stale_days": days, "stale_accounts": stale[:100], "no_sign_in_data": unknown}
    if stale:
        return Check(
            "m365-stale-accounts",
            "Enabled accounts without recent sign-in",
            WARN,
            f"{len(stale)} enabled accounts have not signed in for {days} days.",
            rid,
            details,
        )
    return Check(
        "m365-stale-accounts",
        "Enabled accounts without recent sign-in",
        PASS,
        f"All enabled accounts signed in within {days} days ({unknown} without sign-in data).",
        rid,
        details,
    )


class MicrosoftConnector(Connector):
    key = "microsoft"
    name = "Microsoft 365 / Entra ID"
    description = "Users, MFA, Conditional Access, privileged roles, devices, enterprise applications, audit log, Secure Score."
    env_vars = ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET")
    docs = "docs/connectors.md#microsoft-365--entra-id"

    def configured(self) -> bool:
        s = self.settings
        return bool(s.ms_graph_tenant_id and s.ms_graph_client_id and s.ms_graph_client_secret)

    def _token(self) -> str:
        s = self.settings
        r = httpx.post(
            f"https://login.microsoftonline.com/{s.ms_graph_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": s.ms_graph_client_id,
                "client_secret": s.ms_graph_client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    @staticmethod
    def _get_all(c: httpx.Client, url: str, limit: int = 5000) -> list[dict]:
        items: list[dict] = []
        while url and len(items) < limit:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink", "")
        return items

    def sync(self) -> SyncResult:
        res = SyncResult()
        c = self.client({"Authorization": f"Bearer {self._token()}", "ConsistencyLevel": "eventual"}, GRAPH)
        raw: dict = {}

        # Organisation and domains ----------------------------------------------------
        try:
            org = c.get("/organization").json().get("value", [{}])[0]
            domains = [d["name"] for d in org.get("verifiedDomains", [])]
            raw["organization"] = {"displayName": org.get("displayName"), "domains": domains}
            for d in domains:
                res.inventory.append(
                    InventoryItem("service", f"Domain {d}", f"domain:{d}", "Verified Microsoft 365 domain", "Microsoft 365", {"type": "domain"})
                )
            res.checks.append(
                Check(
                    "m365-tenant",
                    "Tenant identified",
                    INFO,
                    f"Tenant {org.get('displayName')} with {len(domains)} verified domains.",
                    ["ID.AM-02.1"],
                    raw["organization"],
                )
            )
        except Exception as exc:
            res.checks.append(self.error_check("m365-tenant", "Tenant identified", ["ID.AM-02.1"], exc))

        # Users -------------------------------------------------------------------------
        users: list[dict] = []
        try:
            users = self._get_all(c, "/users?$select=id,displayName,userPrincipalName,accountEnabled,userType,createdDateTime&$top=999")
            members = [u for u in users if u.get("userType", "Member") == "Member"]
            guests = [u for u in users if u.get("userType") == "Guest"]
            enabled = [u for u in users if u.get("accountEnabled")]
            raw["users"] = {"total": len(users), "members": len(members), "guests": len(guests), "enabled": len(enabled)}
            for u in users:
                res.inventory.append(
                    InventoryItem(
                        "identity",
                        u.get("userPrincipalName", ""),
                        f"user:{u['id']}",
                        u.get("displayName", ""),
                        "Entra ID",
                        {"enabled": u.get("accountEnabled"), "type": u.get("userType")},
                    )
                )
            res.checks.append(
                Check(
                    "m365-users",
                    "User account inventory",
                    INFO,
                    f"{len(users)} accounts: {len(members)} members, {len(guests)} guests, {len(enabled)} enabled.",
                    ["PR.AA-01.1", "PR.AA-05.1"],
                    raw["users"] | {"guest_accounts": [g.get("userPrincipalName") for g in guests][:50]},
                )
            )
        except Exception as exc:
            res.checks.append(self.error_check("m365-users", "User account inventory", ["PR.AA-01.1"], exc))

        # Sign-in activity (needs AuditLog.Read.All and Entra ID P1) -------------------
        try:
            act = self._get_all(c, "/users?$select=id,userPrincipalName,accountEnabled,signInActivity&$top=999")
            res.checks.append(evaluate_stale_accounts(act))
        except Exception as exc:
            res.checks.append(self.error_check("m365-stale-accounts", "Enabled accounts without recent sign-in", ["PR.AA-01.1"], exc))

        # MFA registration report -------------------------------------------------------
        try:
            rows = self._get_all(c, "/reports/authenticationMethods/userRegistrationDetails?$top=999")
            raw["mfa_registration"] = {"rows": len(rows)}
            res.checks.append(evaluate_mfa_registration(rows))
        except Exception as exc:
            res.checks.append(self.error_check("m365-mfa-registration", "MFA registration coverage", ["PR.AA-03.2"], exc))

        # Conditional Access and security defaults -------------------------------------
        try:
            policies = self._get_all(c, "/identity/conditionalAccess/policies")
            sd = None
            try:
                sd = c.get("/policies/identitySecurityDefaultsEnforcementPolicy").json().get("isEnabled")
            except Exception:
                sd = None
            raw["conditional_access"] = [{"name": p.get("displayName"), "state": p.get("state")} for p in policies]
            res.checks.append(evaluate_ca_policies(policies, sd))
        except Exception as exc:
            res.checks.append(self.error_check("m365-mfa-policy", "MFA enforced for all users", ["PR.AA-03.2"], exc))

        # Global administrators ---------------------------------------------------------
        try:
            roles = c.get(f"/directoryRoles?$filter=roleTemplateId eq '{GLOBAL_ADMIN_TEMPLATE}'").json().get("value", [])
            members = []
            if roles:
                members = self._get_all(c, f"/directoryRoles/{roles[0]['id']}/members?$select=id,displayName,userPrincipalName")
            raw["global_admins"] = [m.get("userPrincipalName") or m.get("displayName") for m in members]
            res.checks.append(evaluate_global_admins(members))
        except Exception as exc:
            res.checks.append(self.error_check("m365-global-admins", "Global Administrator accounts", ["PR.AA-05.4"], exc))

        # Devices (Entra registered/joined) ---------------------------------------------
        try:
            devices = self._get_all(
                c,
                "/devices?$select=id,displayName,operatingSystem,operatingSystemVersion,approximateLastSignInDateTime,isCompliant,isManaged,trustType,accountEnabled&$top=999",
            )
            for d in devices:
                res.inventory.append(
                    InventoryItem(
                        "hardware",
                        d.get("displayName", ""),
                        f"device:{d['id']}",
                        f"{d.get('operatingSystem', '')} {d.get('operatingSystemVersion', '')}".strip(),
                        "Entra ID device",
                        {
                            "compliant": d.get("isCompliant"),
                            "managed": d.get("isManaged"),
                            "trust": d.get("trustType"),
                            "last_sign_in": (d.get("approximateLastSignInDateTime") or "")[:10],
                        },
                    )
                )
            managed = [d for d in devices if d.get("isManaged")]
            compliant = [d for d in devices if d.get("isCompliant")]
            non_compliant = [d.get("displayName") for d in managed if d.get("isCompliant") is False]
            raw["devices"] = {"total": len(devices), "managed": len(managed), "compliant": len(compliant), "non_compliant": non_compliant[:50]}
            rid = ["ID.AM-01.1", "ID.AM-08.2", "DE.CM-01.2", "PR.AA-05.2"]
            if not devices:
                res.checks.append(Check("m365-devices", "Device inventory and compliance", WARN, "No devices registered in Entra ID.", rid, raw["devices"]))
            elif not managed:
                res.checks.append(
                    Check(
                        "m365-devices",
                        "Device inventory and compliance",
                        WARN,
                        f"{len(devices)} devices registered, none managed by device management; compliance cannot be evaluated.",
                        rid,
                        raw["devices"],
                    )
                )
            elif non_compliant:
                res.checks.append(
                    Check(
                        "m365-devices",
                        "Device inventory and compliance",
                        FAIL,
                        f"{len(non_compliant)} of {len(managed)} managed devices are non-compliant.",
                        rid,
                        raw["devices"],
                    )
                )
            else:
                res.checks.append(
                    Check(
                        "m365-devices",
                        "Device inventory and compliance",
                        PASS,
                        f"{len(devices)} devices, {len(managed)} managed, all compliant.",
                        rid,
                        raw["devices"],
                    )
                )
        except Exception as exc:
            res.checks.append(self.error_check("m365-devices", "Device inventory and compliance", ["ID.AM-01.1"], exc))

        # Intune managed devices (optional) --------------------------------------------
        try:
            md = self._get_all(
                c,
                "/deviceManagement/managedDevices?$select=id,deviceName,operatingSystem,osVersion,complianceState,lastSyncDateTime,userPrincipalName,model,manufacturer&$top=999",
            )
            for d in md:
                res.inventory.append(
                    InventoryItem(
                        "hardware",
                        d.get("deviceName", ""),
                        f"intune:{d['id']}",
                        f"{d.get('manufacturer', '')} {d.get('model', '')}".strip(),
                        "Intune",
                        {
                            "os": f"{d.get('operatingSystem', '')} {d.get('osVersion', '')}".strip(),
                            "compliance": d.get("complianceState"),
                            "user": d.get("userPrincipalName"),
                            "last_sync": (d.get("lastSyncDateTime") or "")[:10],
                        },
                    )
                )
            bad = [d.get("deviceName") for d in md if d.get("complianceState") not in ("compliant", "inGracePeriod")]
            raw["intune"] = {"devices": len(md), "non_compliant": bad[:50]}
            rid = ["ID.AM-08.2", "DE.CM-01.2", "ID.AM-01.1"]
            if md and not bad:
                res.checks.append(Check("m365-intune", "Intune device compliance", PASS, f"All {len(md)} Intune devices are compliant.", rid, raw["intune"]))
            elif md:
                res.checks.append(
                    Check("m365-intune", "Intune device compliance", FAIL, f"{len(bad)} of {len(md)} Intune devices are not compliant.", rid, raw["intune"])
                )
            else:
                res.checks.append(Check("m365-intune", "Intune device compliance", INFO, "No Intune managed devices.", rid, raw["intune"]))
        except Exception as exc:
            res.checks.append(self.error_check("m365-intune", "Intune device compliance", ["ID.AM-08.2"], exc))

        # Enterprise applications (third-party service principals) --------------------
        try:
            sps = self._get_all(
                c,
                "/servicePrincipals?$select=id,appDisplayName,appId,servicePrincipalType,accountEnabled,appOwnerOrganizationId,publisherName,createdDateTime&$top=999",
            )
            third = [p for p in sps if p.get("servicePrincipalType") == "Application" and p.get("appOwnerOrganizationId") != MICROSOFT_TENANT]
            for p in third:
                res.inventory.append(
                    InventoryItem(
                        "software",
                        p.get("appDisplayName", ""),
                        f"sp:{p['id']}",
                        p.get("publisherName") or "",
                        "Entra ID enterprise application",
                        {"enabled": p.get("accountEnabled"), "app_id": p.get("appId")},
                    )
                )
            raw["enterprise_apps"] = {"total": len(sps), "third_party": len(third)}
            res.checks.append(
                Check(
                    "m365-apps",
                    "Enterprise application inventory",
                    INFO,
                    f"{len(third)} non-Microsoft applications integrated with the tenant ({len(sps)} service principals in total).",
                    ["ID.AM-02.1", "PR.AA-05.1"],
                    raw["enterprise_apps"] | {"applications": [p.get("appDisplayName") for p in third][:100]},
                )
            )
        except Exception as exc:
            res.checks.append(self.error_check("m365-apps", "Enterprise application inventory", ["ID.AM-02.1"], exc))

        # Directory audit log ----------------------------------------------------------
        try:
            audit = c.get("/auditLogs/directoryAudits?$top=1").json().get("value", [])
            latest = (audit[0].get("activityDateTime") if audit else "") or ""
            rid = ["PR.PS-04.1", "DE.AE-03.1"]
            if audit:
                res.checks.append(
                    Check(
                        "m365-audit-log",
                        "Directory audit logging active",
                        PASS,
                        f"Directory audit log available; latest entry {latest[:16].replace('T', ' ')} UTC (retention 30 days free, longer with P1/P2 or export).",
                        rid,
                        {"latest": latest},
                    )
                )
            else:
                res.checks.append(Check("m365-audit-log", "Directory audit logging active", WARN, "No directory audit entries returned.", rid, {}))
        except Exception as exc:
            res.checks.append(self.error_check("m365-audit-log", "Directory audit logging active", ["PR.PS-04.1"], exc))

        # Secure Score -----------------------------------------------------------------
        try:
            ss = c.get("/security/secureScores?$top=1").json().get("value", [])
            if ss:
                cur, mx = ss[0].get("currentScore", 0), ss[0].get("maxScore", 0) or 1
                pct = round(100 * cur / mx)
                raw["secure_score"] = {"current": cur, "max": mx, "percent": pct, "date": ss[0].get("createdDateTime")}
                res.checks.append(
                    Check(
                        "m365-secure-score",
                        "Microsoft Secure Score",
                        INFO,
                        f"Secure Score {cur:.0f} of {mx:.0f} ({pct}%).",
                        ["GV.RM-03.1", "ID.RA-01.1"],
                        raw["secure_score"],
                    )
                )
        except Exception as exc:
            res.checks.append(self.error_check("m365-secure-score", "Microsoft Secure Score", ["GV.RM-03.1"], exc))

        res.raw = raw
        c.close()
        return res
