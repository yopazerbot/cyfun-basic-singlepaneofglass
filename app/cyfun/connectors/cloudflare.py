"""Cloudflare connector (API v4, API token).

Zone level: zones, DNS records, TLS settings, managed WAF rulesets.
Account level (CLOUDFLARE_ACCOUNT_ID set): Zero Trust Access applications, Gateway
DNS policies, audit log availability, Logpush jobs.
"""

from __future__ import annotations

import httpx

from .base import INFO, PASS, WARN, Check, CheckSpec, Connector, InventoryItem, SyncResult

API = "https://api.cloudflare.com/client/v4"

ZONES = CheckSpec("cloudflare-zones", "DNS zone inventory", ("ID.AM-01.1", "PR.DS-01.9"))
DNS = CheckSpec("cloudflare-dns", "DNS record inventory", ("ID.AM-01.1", "ID.AM-02.1"))
TLS = CheckSpec("cloudflare-tls", "Edge TLS configuration", ("PR.IR-01.1", "PR.AA-03.1"))
WAF = CheckSpec("cloudflare-waf", "Managed WAF rules deployed", ("PR.IR-01.1", "DE.CM-03.1"))
ACCESS = CheckSpec("cloudflare-access", "Zero Trust Access applications", ("PR.AA-03.2", "PR.AA-05.2"))
GATEWAY = CheckSpec("cloudflare-gateway", "DNS filtering policies", ("PR.PS-05.1",))
AUDIT_LOG = CheckSpec("cloudflare-audit-log", "Account audit log available", ("PR.PS-04.1", "DE.AE-03.1"))


def evaluate_tls(zone_settings: dict[str, dict]) -> Check:
    """zone name -> {ssl, min_tls_version, always_use_https}"""
    weak = []
    for z, s in zone_settings.items():
        problems = []
        if s.get("ssl") not in ("full", "strict"):
            problems.append(f"ssl={s.get('ssl')}")
        if s.get("min_tls_version") not in ("1.2", "1.3"):
            problems.append(f"min_tls={s.get('min_tls_version')}")
        if s.get("always_use_https") != "on":
            problems.append("always_use_https=off")
        if problems:
            weak.append({"zone": z, "issues": problems})
    details = {"zones": zone_settings, "weak": weak}
    if not zone_settings:
        return TLS.result(INFO, "No zones found.", details)
    if weak:
        return TLS.result(
            WARN,
            f"{len(weak)} of {len(zone_settings)} zones have weak TLS settings (expect ssl full or strict, TLS 1.2 minimum, always HTTPS).",
            details,
        )
    return TLS.result(PASS, f"All {len(zone_settings)} zones use strict or full TLS with TLS 1.2 minimum and always HTTPS.", details)


def evaluate_waf(zone_waf: dict[str, bool | None]) -> Check:
    without = [z for z, v in zone_waf.items() if v is False]
    unknown = [z for z, v in zone_waf.items() if v is None]
    details = {"zones": zone_waf}
    if not zone_waf:
        return WAF.result(INFO, "No zones found.", details)
    if without:
        return WAF.result(
            WARN,
            f"{len(without)} of {len(zone_waf)} zones have no managed WAF ruleset deployed (the free managed ruleset applies automatically; paid managed rules add coverage).",
            details,
        )
    if unknown:
        return WAF.result(WARN, f"WAF status unknown for {len(unknown)} zones (token lacks Zone WAF read).", details)
    return WAF.result(PASS, f"Managed WAF rulesets deployed on all {len(zone_waf)} zones.", details)


class CloudflareConnector(Connector):
    key = "cloudflare"
    name = "Cloudflare"
    description = "Zones and DNS inventory, edge TLS and WAF posture, Zero Trust Access, Gateway filtering, audit logs."
    env_vars = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
    docs = "docs/connectors.md#cloudflare"

    def configured(self) -> bool:
        return bool(self.config.cloudflare_api_token)

    def _client(self) -> httpx.Client:
        return self.client({"Authorization": f"Bearer {self.config.cloudflare_api_token}"}, API)

    @staticmethod
    def _result(r: httpx.Response):
        r.raise_for_status()
        data = r.json()
        if not data.get("success", False):
            raise RuntimeError("; ".join(e.get("message", "") for e in data.get("errors", []))[:300])
        return data.get("result")

    def test(self) -> str:
        acc = self.config.cloudflare_account_id.strip()
        with self._client() as c:
            status = (self._result(c.get("/user/tokens/verify")) or {}).get("status", "")
            if status != "active":
                raise RuntimeError(f"token status is {status or 'unknown'}")
            if acc:
                name = (self._result(c.get(f"/accounts/{acc}")) or {}).get("name", acc)
                return f"Token is active; account {name} is readable."
        return "Token is active."

    def sync(self) -> SyncResult:
        res = SyncResult()
        raw: dict = {}
        with self._client() as c:
            zones: list[dict] = []
            try:
                zones = self._result(c.get("/zones", params={"per_page": 50})) or []
                raw["zones"] = [z.get("name") for z in zones]
                for z in zones:
                    plan = z.get("plan") or {}
                    res.inventory.append(
                        InventoryItem(
                            "network",
                            f"Zone {z['name']}",
                            f"zone:{z['id']}",
                            f"Cloudflare zone ({plan.get('name', '')})",
                            "Cloudflare",
                            {"status": z.get("status"), "plan": plan.get("name")},
                        )
                    )
                res.checks.append(ZONES.result(INFO, f"{len(zones)} zones managed on Cloudflare.", raw))
            except Exception as exc:
                res.checks.append(ZONES.error(exc))

            # DNS records, TLS settings, WAF per zone -----------------------------------
            tls: dict[str, dict] = {}
            waf: dict[str, bool | None] = {}
            dns_total = 0
            for z in zones:
                zid, zname = z["id"], z["name"]
                try:
                    records = self._result(c.get(f"/zones/{zid}/dns_records", params={"per_page": 500})) or []
                    dns_total += len(records)
                    for rec in records:
                        if rec.get("type") in ("A", "AAAA", "CNAME"):
                            res.inventory.append(
                                InventoryItem(
                                    "service",
                                    rec.get("name", ""),
                                    f"dns:{rec.get('id')}",
                                    f"{rec.get('type')} {rec.get('content', '')}"[:300],
                                    f"Cloudflare zone {zname}",
                                    {"type": rec.get("type"), "proxied": rec.get("proxied")},
                                )
                            )
                except Exception as exc:
                    res.checks.append(CheckSpec(f"cloudflare-dns-{zname}", f"DNS records of {zname}", ("ID.AM-01.1",)).error(exc))
                try:
                    tls[zname] = {
                        key: (self._result(c.get(f"/zones/{zid}/settings/{key}")) or {}).get("value") for key in ("ssl", "min_tls_version", "always_use_https")
                    }
                except Exception:
                    tls[zname] = {}
                try:
                    r = c.get(f"/zones/{zid}/rulesets/phases/http_request_firewall_managed/entrypoint")
                    if r.status_code == 200 and r.json().get("success"):
                        rules = (r.json().get("result") or {}).get("rules") or []
                        waf[zname] = any(rule.get("action") == "execute" for rule in rules)
                    elif r.status_code == 404:
                        waf[zname] = False
                    else:
                        waf[zname] = None
                except Exception:
                    waf[zname] = None
            if zones:
                res.checks.append(DNS.result(INFO, f"{dns_total} DNS records across {len(zones)} zones.", {"records": dns_total}))
                res.checks.append(evaluate_tls(tls))
                res.checks.append(evaluate_waf(waf))
            raw["tls"] = tls
            raw["waf"] = waf

            # Account level ---------------------------------------------------------------
            acc = self.config.cloudflare_account_id.strip()
            if acc:
                try:
                    apps = self._result(c.get(f"/accounts/{acc}/access/apps")) or []
                    names = [a.get("name") for a in apps]
                    raw["access_apps"] = names
                    if apps:
                        res.checks.append(ACCESS.result(INFO, f"{len(apps)} applications protected by Cloudflare Access.", {"applications": names}))
                    else:
                        res.checks.append(ACCESS.result(INFO, "No Cloudflare Access applications configured."))
                except Exception as exc:
                    res.checks.append(ACCESS.error(exc))
                try:
                    rules = self._result(c.get(f"/accounts/{acc}/gateway/rules")) or []
                    blocking = [r for r in rules if r.get("enabled") and r.get("action") == "block"]
                    dns_block = [r.get("name") for r in blocking if "dns" in (r.get("filters") or [])]
                    raw["gateway"] = {"rules": len(rules), "blocking": len(blocking), "dns_blocking": dns_block}
                    if dns_block:
                        res.checks.append(GATEWAY.result(PASS, f"{len(dns_block)} enabled Gateway DNS block policies.", raw["gateway"]))
                    else:
                        res.checks.append(GATEWAY.result(WARN, "No enabled Gateway DNS block policy found.", raw["gateway"]))
                except Exception as exc:
                    res.checks.append(GATEWAY.error(exc))
                try:
                    logs = self._result(c.get(f"/accounts/{acc}/audit_logs", params={"per_page": 1})) or []
                    try:
                        jobs = self._result(c.get(f"/accounts/{acc}/logpush/jobs")) or []
                    except Exception:
                        jobs = []
                    latest = (logs[0].get("when") if logs else "") or ""
                    raw["audit"] = {"latest": latest, "logpush_jobs": len(jobs)}
                    if logs:
                        res.checks.append(
                            AUDIT_LOG.result(
                                PASS,
                                f"Cloudflare audit log available (latest {latest[:16].replace('T', ' ')} UTC); {len(jobs)} Logpush jobs configured.",
                                raw["audit"],
                            )
                        )
                    else:
                        res.checks.append(AUDIT_LOG.result(WARN, "No audit log entries returned.", raw["audit"]))
                except Exception as exc:
                    res.checks.append(AUDIT_LOG.error(exc))

        res.raw = raw
        return res
