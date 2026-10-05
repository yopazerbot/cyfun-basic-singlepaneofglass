# Security

The application holds the organisation's security posture, evidence and read-only credentials to its core systems. It is treated as a confidential system.

## Threat model

| Threat | Control |
|---|---|
| Unauthenticated access | Every page and download requires a session; sign-in only through Microsoft Entra ID (OIDC code flow with PKCE, nonce, tenant check, signature validation against the tenant JWKS). No local accounts, no password form. |
| Account without entitlement | Role from Entra app roles (Admin, Auditor). No role: access denied unless `AUTH_DEFAULT_ROLE` is set deliberately. Enable "User assignment required" on the enterprise application so only assigned users can sign in at all. |
| Session theft | Opaque 256-bit token in an HttpOnly, Secure, SameSite=Lax cookie; only its SHA-256 hash is stored; idle and absolute expiry; logout deletes the server-side row. |
| Cross-site request forgery | All writes are POST; the middleware requires an `Origin` (or `Referer`) header matching `APP_BASE_URL`; SameSite=Lax cookie. |
| Cross-site scripting | Jinja2 autoescaping; Content-Security-Policy `default-src 'self'` with no inline scripts or styles; htmx runs with `allowEval` off and `selfRequestsOnly` on. |
| Clickjacking, MIME sniffing, referrer leaks | `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` on pages. |
| Open redirect after login | `next` must be a same-origin path. |
| Malicious uploads | Extension allow-list, size limit, random storage name outside the web root, downloads as attachments with nosniff, path containment check. |
| Credential exposure | Secrets only in environment variables (`.env`, excluded from git and from the image). Nothing in the database or in exports. Connector credentials are read-only by design. |
| Privilege escalation in the container | Non-root user, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, tmpfs for /tmp, only `/data` writable, application port not published. |
| Brute force on auth endpoints | In-memory rate limit per client address on `/auth/*`. |
| Transport | TLS terminated by Caddy; HSTS set by Caddy and by the application when `APP_BASE_URL` is https. |
| Supply chain | Pinned dependencies, Dependabot, image scan in CI (Trivy, fails on fixable critical and high). |
| Tampered evidence | SHA-256 per file recorded at upload and exported in the audit pack. |
| Loss of traceability | Append-only activity log with actor, action, entity and before/after details; no update or delete endpoint. |

## What the application does not protect against

* A compromised Entra tenant or Global Administrator: SSO trusts the tenant.
* Host compromise: anyone with Docker or root access to the Proxmox guest can read `/data` and `.env`. Use full-disk encryption on the guest, restrict SSH, keep the host patched.
* Secrets in `.env` on the host: protect the file (`chmod 600`), keep it out of backups that leave the organisation's control, rotate the client secret and connector tokens on a schedule.

## Operating checklist

1. Separate app registrations for sign-in (delegated, ID tokens) and for the Graph connector (application permissions, least privilege as listed in docs/connectors.md).
2. "User assignment required" on the sign-in application; assign named users to Admin or Auditor.
3. `AUTH_DEFAULT_ROLE` empty.
4. `APP_BASE_URL` https, matching the Caddy hostname and the Entra redirect URI.
5. DNS record for the hostname resolvable only where users are (LAN or VPN); do not publish the host to the internet unless the CAB needs remote access, and then only for the verification window.
6. Nightly backup of the `/data` volume (docs/deployment-proxmox.md). Test a restore.
7. Review the activity log and connector run errors monthly.
8. Update the image monthly (`docker compose pull && docker compose up -d --build`) and read the CI scan results.

## Reporting a vulnerability

See SECURITY.md.
