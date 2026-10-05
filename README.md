# CyFun Basic Single Pane of Glass

Self-hosted web application for one purpose: obtain and keep the **CyberFundamentals (CyFun) 2025 BASIC** label from the Centre for Cybersecurity Belgium (CCB). It runs the CCB risk assessment that selects the assurance level, the BASIC self-assessment with the exact CCB scoring, the evidence and document registers, remediation tracking, read-only connectors to live systems, and the exports needed for the self-declaration and the verification by a Conformity Assessment Body (CAB).

Two containers (application and TLS proxy), one SQLite file, Microsoft Entra ID single sign-on, all secrets in environment variables.

## What it does

| Module | Purpose | CyFun hook |
|---|---|---|
| Scope and journey | Organisation, legal entity, scope statement, CAB, self-assessment date; eight journey stages from scope to label | CAS self-declaration |
| Risk assessment | CCB "CyFun-Selection" method: 5 attack categories × 5 threat actors, sector defaults for all 16 NIS2 sector sheets, organisation size, score and resulting level (BASIC, IMPORTANT, ESSENTIAL) | assurance level selection, ID.RA-05.1 |
| Risk register | Threats, vulnerabilities, likelihood × impact, treatment, owner, review date | ID.RA-01.1, ID.RA-05.1 |
| Self-assessment | 34 requirements in 6 functions and 17 categories, 13 key measures; documentation and implementation maturity 1 to 5; one N/A allowed (never on a key measure); justification per requirement; guidance and typical evidence per requirement | the CCB BASIC self-assessment tool, scoring reproduced formula by formula |
| Assets | Hardware, software, services, data, network, cloud, identities; classification, criticality, primary/secondary, owner; connector-synced items retire automatically | ID.AM-01.1, ID.AM-02.1, ID.AM-05.1, ID.AM-07.1 |
| Documents | Policies, procedures, plans, records with version, approval, review dates and requirement mapping | GV.PO-01.1, documentation maturity |
| Evidence | Files (SHA-256 hashed, random storage names) and links mapped to requirements | verification |
| Actions | Remediation with owner, priority, due date, status | internal planning, excluded from exports |
| Connected systems | Microsoft 365 / Entra ID, GitHub, Railway, Cloudflare. Each run refreshes the inventory, produces checks mapped to requirements and stores a raw snapshot | ID.AM, PR.AA, PR.PS, PR.IR, DE.CM, DE.AE |
| Audit view and export | Read-only verification view per requirement; snapshots; fills the official CCB BASIC workbook; audit pack ZIP; JSON | self-declaration and CAB verification |
| Activity log | Append-only record of every change | traceability |

Scores stay a human judgement. Connector checks are evidence placed next to the requirement, not an automatic score.

## Scoring, as the CCB workbook computes it

* Each requirement: documentation score and implementation score, 1 to 5. A requirement marked not applicable counts 2,5 on both. BASIC allows one N/A; key measures cannot be N/A.
* Subcategory score = average of its requirements. Category score = average of its subcategories. Category maturity = average of documentation and implementation.
* Total maturity = average of the 17 category maturities.
* BASIC thresholds: total maturity ≥ 2,5 and every key measure ≥ 2,5.

The tests reproduce the CCB workbook's results for all categories and key measures, and the risk model reproduces the totals and levels of all 16 CCB sector sheets.

## Quick start (local, no SSO yet)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

The application refuses to serve pages without Entra ID single sign-on; there is no local account mode. Configure an app registration first (docs/entra-id-sso.md), then:

```bash
cp .env.example .env            # fill in APP_BASE_URL, AUTH_*, and any connector credentials
docker compose up -d --build
```

Caddy serves `https://$APP_HOSTNAME` with an internal CA by default (set `CADDY_TLS` to an e-mail address for Let's Encrypt). Open the URL, sign in with a Microsoft account that holds the Admin or Auditor app role.

## Documentation

| Document | Content |
|---|---|
| [docs/functionality.md](docs/functionality.md) | Functionality derived from the CyFun 2025 BASIC framework and the CAS |
| [docs/architecture.md](docs/architecture.md) | Components, data model, data flow, decisions |
| [docs/security.md](docs/security.md) | Threat model, controls, hardening checklist |
| [docs/entra-id-sso.md](docs/entra-id-sso.md) | Entra ID app registration, app roles, environment variables |
| [docs/connectors.md](docs/connectors.md) | Per connector: credentials, permissions, checks and their requirement mapping |
| [docs/deployment-proxmox.md](docs/deployment-proxmox.md) | Proxmox VM or LXC, Docker, DNS and TLS, backup and restore, updates |
| [docs/audit-verification.md](docs/audit-verification.md) | Using the tool for the self-declaration and during the CAB verification |

## Repository layout

```
app/cyfun/                 application package (FastAPI, Jinja2, SQLAlchemy, SQLite)
  framework/               basic_2025.json, risk_model.json (generated), guidance_basic_2025.json
  connectors/              microsoft, github, railway, cloudflare
  routers/                 one module per screen
  templates/ static/       server-rendered HTML, one stylesheet, vendored htmx
  export_xlsx.py           fills the official CCB workbook at XML level
  audit_pack.py            builds the audit ZIP
scripts/                   regenerate the framework JSON from the official CCB workbooks
tests/                     pytest suite (scoring, risk model, export, connectors, HTTP)
deploy/Caddyfile           TLS reverse proxy
Dockerfile, compose.yaml   containers
```

## Framework content and licence

Requirement identifiers and statements come from the CCB self-assessment tool (BASIC, tool version 2026-02-20, requirements version 2025-10-01) and are reproduced for non-commercial use with acknowledgement, as the CCB allows. The CCB workbooks themselves are not in this repository: regenerate the JSON with `scripts/build_framework.py` and `scripts/build_risk_sectors.py` from your own downloads, and upload your own copy of the BASIC workbook when exporting. See [NOTICE](NOTICE).

Code: MIT licence. CyFun is a trademark of the CCB; this project is independent of the CCB and of any CAB.
