"""Cloudflare connector (API v4, API token).

Zone level: zones, DNS records, TLS settings, managed WAF rulesets.
Account level (CLOUDFLARE_ACCOUNT_ID set): Zero Trust Access applications, Gateway
DNS policies, audit log availability, Logpush jobs.
"""

from __future__ import annotations

import httpx

from .base import INFO, PASS, WARN, Check, Connector, InventoryItem, SyncResult

API = "https://api.cloudflare.com/client/v4"


def evaluate_tls(zone_settings: dict[str, dict]) -> Check:
    """zone name -> {ssl, min_tls_version, always_use_https}"""
    rid = ["PR.IR-01.1", "PR.AA-03.1"]
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
        return Check("cloudflare-tls", "Edge TLS configuration", INFO, "No zones found.", rid, details)
    if weak:
        return Check(
            "cloudflare-tls",
            "Edge TLS configuration",
            WARN,
            f"{len(weak)} of {len(zone_settings)} zones have weak TLS settings (expect ssl full or strict, TLS 1.2 minimum, always HTTPS).",
            rid,
            details,
        )
    return Check(
        "cloudflare-tls",
        "Edge TLS configuration",
        PASS,
        f"All {len(zone_settings)} zones use strict or full TLS with TLS 1.2 minimum and always HTTPS.",
        rid,
        details,
    )


def evaluate_waf(zone_waf: dict[str, bool | None]) -> Check:
    rid = ["PR.IR-01.1", "DE.CM-03.1"]
    without = [z for z, v in zone_waf.items() if v is False]
    unknown = [z for z, v in zone_waf.items() if v is None]
    details = {"zones": zone_waf}
    if not zone_waf:
        return Check("cloudflare-waf", "Managed WAF rules deployed", INFO, "No zones found.", rid, details)
    if without:
        return Check(
            "cloudflare-waf",
            "Managed WAF rules deployed",
            WARN,
            f"{len(without)} of {len(zone_waf)} zones have no managed WAF ruleset deployed (the free managed ruleset applies automatically; paid managed rules add coverage).",
            rid,
            details,
        )
    if unknown:
        return Check(
            "cloudflare-waf", "Managed WAF rules deployed", WARN, f"WAF status unknown for {len(unknown)} zones (token lacks Zone WAF read).", rid, details
        )
    return Check("cloudflare-waf", "Managed WAF rules deployed", PASS, f"Managed WAF rulesets deployed on all {len(zone_waf)} zones.", rid, details)


class CloudflareConnector(Connector):
    key = "cloudflare"
    name = "Cloudflare"
    description = "Zones and DNS inventory, edge TLS and WAF posture, Zero Trust Access, Gateway filtering, audit logs."
    env_vars = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
    docs = "docs/connectors.md#cloudflare"

    def configured(self) -> bool:
        return bool(self.settings.cloudflare_api_token)

    def _client(self) -> httpx.Client:
        return self.client({"Authorization": f"Bearer {self.settings.cloudflare_api_token}"}, API)

    @staticmethod
    def _result(r: httpx.Response):
        r.raise_for_status()
        data = r.json()
        if not data.get("success", False):
            raise RuntimeError("; ".join(e.get("message", "") for e in data.get("errors", []))[:300])
        return data.get("result")

    def sync(self) -> SyncResult:
        res = SyncResult()
        c = self._client()
        raw: dict = {}
        zones: list[dict] = []
        try:
            zones = self._result(c.get("/zones", params={"per_page": 50})) or []
            raw["zones"] = [z.get("name") for z in zones]
            for z in zones:
                res.inventory.append(
                    InventoryItem(
                        "network",
                        f"Zone {z['name']}",
                        f"zone:{z['id']}",
                        f"Cloudflare zone ({(z.get('plan') or {}).get('name', '')})",
                        "Cloudflare",
                        {"status": z.get("status"), "plan": (z.get("plan") or {}).get("name")},
                    )
                )
            res.checks.append(
                Check("cloudflare-zones", "DNS zone inventory", INFO, f"{len(zones)} zones managed on Cloudflare.", ["ID.AM-01.1", "PR.DS-01.9"], raw)
            )
        except Exception as exc:
            res.checks.append(self.error_check("cloudflare-zones", "DNS zone inventory", ["ID.AM-01.1"], exc))

        # DNS records, TLS settings, WAF per zone ---------------------------------------
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
                res.checks.append(self.error_check(f"cloudflare-dns-{zname}", f"DNS records of {zname}", ["ID.AM-01.1"], exc))
            try:
                settings = {}
                for key in ("ssl", "min_tls_version", "always_use_https"):
                    settings[key] = (self._result(c.get(f"/zones/{zid}/settings/{key}")) or {}).get("value")
                tls[zname] = settings
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
            res.checks.append(
                Check(
                    "cloudflare-dns",
                    "DNS record inventory",
                    INFO,
                    f"{dns_total} DNS records across {len(zones)} zones.",
                    ["ID.AM-01.1", "ID.AM-02.1"],
                    {"records": dns_total},
                )
            )
            res.checks.append(evaluate_tls(tls))
            res.checks.append(evaluate_waf(waf))
        raw["tls"] = tls
        raw["waf"] = waf

        # Account level ----------------------------------------------------------------
        acc = self.settings.cloudflare_account_id.strip()
        if acc:
            try:
                apps = self._result(c.get(f"/accounts/{acc}/access/apps")) or []
                names = [a.get("name") for a in apps]
                raw["access_apps"] = names
                rid = ["PR.AA-03.2", "PR.AA-05.2"]
                if apps:
                    res.checks.append(
                        Check(
                            "cloudflare-access",
                            "Zero Trust Access applications",
                            INFO,
                            f"{len(apps)} applications protected by Cloudflare Access.",
                            rid,
                            {"applications": names},
                        )
                    )
                else:
                    res.checks.append(
                        Check("cloudflare-access", "Zero Trust Access applications", INFO, "No Cloudflare Access applications configured.", rid, {})
                    )
            except Exception as exc:
                res.checks.append(self.error_check("cloudflare-access", "Zero Trust Access applications", ["PR.AA-03.2"], exc))
            try:
                rules = self._result(c.get(f"/accounts/{acc}/gateway/rules")) or []
                blocking = [r for r in rules if r.get("enabled") and r.get("action") == "block"]
                dns_block = [r.get("name") for r in blocking if "dns" in (r.get("filters") or [])]
                raw["gateway"] = {"rules": len(rules), "blocking": len(blocking), "dns_blocking": dns_block}
                rid = ["PR.PS-05.1"]
                if dns_block:
                    res.checks.append(
                        Check(
                            "cloudflare-gateway", "DNS filtering policies", PASS, f"{len(dns_block)} enabled Gateway DNS block policies.", rid, raw["gateway"]
                        )
                    )
                else:
                    res.checks.append(
                        Check("cloudflare-gateway", "DNS filtering policies", WARN, "No enabled Gateway DNS block policy found.", rid, raw["gateway"])
                    )
            except Exception as exc:
                res.checks.append(self.error_check("cloudflare-gateway", "DNS filtering policies", ["PR.PS-05.1"], exc))
            try:
                logs = self._result(c.get(f"/accounts/{acc}/audit_logs", params={"per_page": 1})) or []
                jobs = []
                try:
                    jobs = self._result(c.get(f"/accounts/{acc}/logpush/jobs")) or []
                except Exception:
                    jobs = []
                rid = ["PR.PS-04.1", "DE.AE-03.1"]
                latest = (logs[0].get("when") if logs else "") or ""
                raw["audit"] = {"latest": latest, "logpush_jobs": len(jobs)}
                if logs:
                    res.checks.append(
                        Check(
                            "cloudflare-audit-log",
                            "Account audit log available",
                            PASS,
                            f"Cloudflare audit log available (latest {latest[:16].replace('T', ' ')} UTC); {len(jobs)} Logpush jobs configured.",
                            rid,
                            raw["audit"],
                        )
                    )
                else:
                    res.checks.append(Check("cloudflare-audit-log", "Account audit log available", WARN, "No audit log entries returned.", rid, raw["audit"]))
            except Exception as exc:
                res.checks.append(self.error_check("cloudflare-audit-log", "Account audit log available", ["PR.PS-04.1"], exc))

        res.raw = raw
        c.close()
        return res
