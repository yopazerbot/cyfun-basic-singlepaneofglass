# Connected systems

Enter the credentials on the Settings page (Administration, Settings). Secrets are encrypted in the database with `CYFUN_SECRET_KEY` and never shown again; "Test connection" checks typed or stored values with one read-only call before or after saving. A connector is enabled as soon as its credentials are set, without a restart. The environment variables listed per connector still work and take precedence over the page. Runs happen at the interval set on the Settings page (default 24 hours, `CONNECTOR_SYNC_HOURS` overrides it) and on demand (Admin, "Run now"). Each run stores a raw snapshot under `/data/connector_snapshots/<connector>/`, the checks below, and one automated evidence item per check that points at the snapshot and its SHA-256 hash. Statuses: `pass`, `fail`, `warn` (needs a look), `info` (fact, no judgement), `error` (the connector could not evaluate; usually a missing permission or licence).

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

Settings page fields, or these environment variables:

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

Settings page fields, or these environment variables:

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

Settings page fields, or these environment variables:

```
RAILWAY_TOKEN=
```

| Check | Logic | Requirements |
|---|---|---|
| railway-inventory | projects (inventory: cloud) and services (inventory: service) with environments | ID.AM-01.1, ID.AM-02.1 |

## Cloudflare

API token (My Profile → API Tokens → Create Token, custom): Zone → Zone Read, DNS Read, Zone Settings Read, Zone WAF Read, for all zones. For the account-level checks add Account → Access: Apps and Policies Read, Zero Trust Read (Gateway), Logs Read (Logpush), Account Settings Read, and set `CLOUDFLARE_ACCOUNT_ID` (Overview page of any zone, right column).

Settings page fields, or these environment variables:

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

## Notion

The document register follows one Notion database: each page is a policy, procedure, plan, register or record. A run copies the page metadata into the register. Those entries are marked Notion, are read-only in the application and are edited in Notion; a page that disappears from the database is set to retired. Page content stays in Notion; the register links to it.

Set-up:

1. In Notion, Settings, Connections, Develop or manage integrations (notion.so/profile/integrations): new internal integration for your workspace. Capabilities: Read content. Add Insert content only if the application should create the database. Copy the integration secret.
2. Share the page that holds (or will hold) the database with the integration: page menu, Connections, add the integration.
3. Settings page, group Notion: paste the secret. Either paste the database link, or paste the link of the parent page under "New database under this Notion page" and choose **Create database in Notion**. That creates "CyFun documented information" with the properties below and saves its ID.
4. **Test connection**, then run the connector on Connected systems.

Properties the register reads (names are not case-sensitive; other properties are ignored):

| Property | Notion type | Register field |
|---|---|---|
| any title property | title | Title |
| Type | select | policy, procedure, plan, register, record, otherwise other |
| Status | select or status | Approved → approved; Retired or Archived → retired; anything else → draft |
| Owner, Approved by | people, text or select | names |
| Version | text, number or select | version |
| Approved on, Last review, Next review | date | dates |
| Requirements | multi-select or text | CyFun requirement IDs such as GV.PO-01.1; unknown IDs are ignored |

Settings page fields, or these environment variables:

```
NOTION_TOKEN=
NOTION_DATABASE=        # database link or ID
```

| Check | Logic | Requirements |
|---|---|---|
| notion-documents | documents and approved documents per type; no approved document → warn | GV.PO-01.1 |
| notion-reviews | approved documents past Next review, or without a review or approval in the last two years → fail; due within 30 days → warn | GV.PO-01.1 |
| notion-mapping | current documents without a requirement → warn | GV.PO-01.1 |

## Interpreting results

A `fail` is a fact about the connected system, not a verdict on the maturity score. Decide, document the decision in the requirement's justification or open an action, and keep the snapshot. An `error` means the connector could not evaluate; fix the permission or accept that this check will not be used as evidence.

## Adding a connector

See docs/architecture.md, "Adding a connector".
