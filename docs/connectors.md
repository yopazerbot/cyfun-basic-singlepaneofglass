# Connected systems

A connector is enabled when its environment variables are set; restart the application after changing them. Runs happen every `CONNECTOR_SYNC_HOURS` hours and on demand (Admin, "Run now"). Each run stores a raw snapshot under `/data/connector_snapshots/<connector>/` and the checks below. Statuses: `pass`, `fail`, `warn` (needs a look), `info` (fact, no judgement), `error` (the connector could not evaluate; usually a missing permission or licence).

All credentials are read-only. Never grant write scopes.

## Microsoft 365 / Entra ID

Create a second app registration ("CyFun Graph reader"), single tenant, no redirect URI. Certificates & secrets → client secret. API permissions → Microsoft Graph → **Application permissions**, then grant admin consent:

| Permission | Used for |
|---|---|
| User.Read.All | user inventory |
| Directory.Read.All | Global Administrator role members, devices, service principals |
| Policy.Read.All | Conditional Access policies, security defaults |
| AuditLog.Read.All | sign-in activity, MFA registration report, directory audit log |
| Device.Read.All | Entra registered and joined devices |
| Organization.Read.All | tenant name and verified domains |
| SecurityEvents.Read.All | Secure Score |
| DeviceManagementManagedDevices.Read.All | Intune managed devices (only if Intune is used) |

Sign-in activity and the MFA registration report need an Entra ID P1 licence; without it those two checks report `error` and everything else works.

```
MS_GRAPH_TENANT_ID=
MS_GRAPH_CLIENT_ID=
MS_GRAPH_CLIENT_SECRET=
```

| Check | Logic | Requirements |
|---|---|---|
| m365-tenant | tenant name and verified domains (inventory: domains) | ID.AM-02.1 |
| m365-users | members, guests, enabled accounts (inventory: identities) | PR.AA-01.1, PR.AA-05.1 |
| m365-stale-accounts | enabled accounts without sign-in for 90 days → warn | PR.AA-01.1, PR.AA-05.1 |
| m365-mfa-registration | all users MFA-registered → pass; admins missing or under 90 % → fail | PR.AA-03.2, PR.AA-01.1 |
| m365-mfa-policy | security defaults on, or an enabled Conditional Access policy requiring MFA (or an authentication strength) for all users and all apps → pass; partial policies → warn; none → fail | PR.AA-03.2, GV.RR-04.1 |
| m365-global-admins | 2 to 4 → pass; 1 → warn; more than 4 → fail | PR.AA-05.4, PR.AA-05.3 |
| m365-devices | Entra devices (inventory: hardware); managed devices all compliant → pass, non-compliant → fail, none managed → warn | ID.AM-01.1, ID.AM-08.2, DE.CM-01.2, PR.AA-05.2 |
| m365-intune | Intune devices (inventory: hardware) all compliant → pass | ID.AM-08.2, DE.CM-01.2, ID.AM-01.1 |
| m365-apps | non-Microsoft enterprise applications (inventory: software) | ID.AM-02.1, PR.AA-05.1 |
| m365-audit-log | directory audit entries present → pass | PR.PS-04.1, DE.AE-03.1 |
| m365-secure-score | current Secure Score (info) | GV.RM-03.1, ID.RA-01.1 |

## GitHub

Fine-grained personal access token (preferred) with read-only permissions: for an organisation, resource owner = the organisation; repository permissions Metadata, Contents, Administration (read, for branch protection), Dependabot alerts (read); organisation permissions Members (read), Administration (read, for the two-factor requirement). A classic token needs `read:org`, `repo` and `security_events`. Set `GITHUB_ORG` for organisation mode; leave it empty to read the token owner's repositories.

```
GITHUB_TOKEN=
GITHUB_ORG=
```

| Check | Logic | Requirements |
|---|---|---|
| github-2fa | organisation two-factor requirement (or user 2FA) → pass/fail; unreadable → warn | PR.AA-03.2, PR.AA-01.1 |
| github-repos | repositories (inventory: software) with visibility, archived, default branch, last push | ID.AM-02.1 |
| github-branch-protection | default branch protected (branch protection or ruleset) on active repositories, max 100 | PR.AA-05.1, PR.AA-05.3 |
| github-dependabot | open alerts; any critical or high → fail; others → warn; none → pass | ID.RA-01.1, ID.AM-08.2 |
| github-admins | organisation owners versus members; every member an owner → warn | PR.AA-05.4, PR.AA-05.3 |

## Railway

Account token (Account settings → Tokens) or a team token. Read access is enough.

```
RAILWAY_TOKEN=
```

| Check | Logic | Requirements |
|---|---|---|
| railway-inventory | projects (inventory: cloud) and services (inventory: service) with environments | ID.AM-01.1, ID.AM-02.1 |

## Cloudflare

API token (My Profile → API Tokens → Create Token, custom): Zone → Zone Read, DNS Read, Zone Settings Read, Zone WAF Read, for all zones. For the account-level checks add Account → Access: Apps and Policies Read, Zero Trust Read (Gateway), Logs Read (Logpush), Account Settings Read, and set `CLOUDFLARE_ACCOUNT_ID` (Overview page of any zone, right column).

```
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ACCOUNT_ID=
```

| Check | Logic | Requirements |
|---|---|---|
| cloudflare-zones | zones (inventory: network) | ID.AM-01.1, PR.DS-01.9 |
| cloudflare-dns | A, AAAA and CNAME records (inventory: service) | ID.AM-01.1, ID.AM-02.1 |
| cloudflare-tls | ssl full or strict, minimum TLS 1.2, always use HTTPS on every zone → pass; else warn | PR.IR-01.1, PR.AA-03.1 |
| cloudflare-waf | managed WAF ruleset deployed on every zone → pass; missing → warn | PR.IR-01.1, DE.CM-03.1 |
| cloudflare-access | Zero Trust Access applications (info) | PR.AA-03.2, PR.AA-05.2 |
| cloudflare-gateway | enabled Gateway DNS block policies → pass; none → warn | PR.PS-05.1 |
| cloudflare-audit-log | account audit log readable → pass; Logpush job count | PR.PS-04.1, DE.AE-03.1 |

## Interpreting results

A `fail` is a fact about the connected system, not a verdict on the maturity score. Decide, document the decision in the requirement's justification or open an action, and keep the snapshot. An `error` means the connector could not evaluate; fix the permission or accept that this check will not be used as evidence.

## Adding a connector

See docs/architecture.md, "Adding a connector".
