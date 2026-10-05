# Security

The application holds the organisation's security posture, evidence and read-only credentials to its core systems. It is treated as a confidential system.

## Sign-in options

| Option | When | Notes |
|---|---|---|
| Microsoft Entra ID (OIDC, PKCE) | Recommended for all staff | Roles from Entra app roles; "user assignment required" keeps unassigned accounts out entirely. |
| Local accounts (`AUTH_LOCAL_ENABLED`, default on) | First setup, environments without a tenant, an external auditor | Passwords hashed with scrypt (random salt), minimum 12 characters, common passwords refused, forced change at first login, 5 failures lock the account for 15 minutes, per-address rate limit on all sign-in endpoints. The default `admin` / `admin` account exists only until its first login completes. |

Set `AUTH_LOCAL_ENABLED=false` once Entra ID works. Local accounts then cannot sign in and their sessions end.

## Threat model

| Threat | Control |
|---|---|
| Unauthenticated access | Every page and download requires a session. Sign-in through Entra ID (code flow with PKCE, nonce, tenant check, signature validation against the tenant JWKS) or a local account. |
| Account without entitlement | Entra: role from app roles (Admin, Auditor); no role means access denied unless `AUTH_DEFAULT_ROLE` is set deliberately. Local: role set by an administrator at creation. |
| Password guessing | scrypt hashes; lockout after `LOGIN_MAX_FAILURES`; rate limit per client address; constant-time comparison; identical error message for unknown user and wrong password; failed attempts logged. |
| Default credentials | The default local administrator must change its password before any other request is served; the change ends all other sessions of the account. |
| Session theft | Opaque 256-bit token in an HttpOnly, Secure, SameSite=Lax cookie; only its SHA-256 hash is stored; idle and absolute expiry; logout, password change, reset and disable end sessions server-side. |
| Cross-site request forgery | All writes are POST; the middleware requires an `Origin` (or `Referer`) header matching `APP_BASE_URL`; SameSite=Lax cookie. |
| Cross-site scripting | Jinja2 autoescaping; Content-Security-Policy `default-src 'self'` with no inline scripts or styles; htmx runs with `allowEval` off and `selfRequestsOnly` on. |
| Clickjacking, MIME sniffing, referrer leaks | `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` on pages. |
| Open redirect after login | `next` must be a same-origin path. |
| Malicious uploads | Extension allow-list, size limit, random storage name outside the web root, downloads as attachments with nosniff, path containment check. |
| Credential exposure | Secrets only in environment variables (`.env`, excluded from git and from the image). Nothing in the database or in exports. Connector credentials are read-only by design. Temporary passwords are shown once in the response body, never in URLs or logs. |
| Privilege escalation in the container | Non-root user, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, tmpfs for /tmp, only `/data` writable, application port not published, pip removed from the runtime image. |
| Transport | TLS terminated by Caddy; HSTS set by Caddy and by the application when `APP_BASE_URL` is https. |
| Supply chain | Pinned dependencies, Dependabot, image scan in CI (Trivy, fails on fixable critical and high). |
| Tampered evidence | SHA-256 per file recorded at upload and exported in the audit pack. |
| Loss of traceability | Append-only activity log with actor, action, entity and before/after details, including sign-ins, failed sign-ins, password changes and user administration. |

## What the application does not protect against

* A compromised Entra tenant or Global Administrator: SSO trusts the tenant.
* Host compromise: anyone with Docker or root access to the Proxmox guest can read `/data` and `.env`. Use full-disk encryption on the guest, restrict SSH, keep the host patched.
* Secrets in `.env` on the host: protect the file (`chmod 600`), keep it out of backups that leave the organisation's control, rotate the client secret and connector tokens on a schedule.
* A weak local password chosen by a user: the policy enforces length and blocks common passwords, nothing more. Prefer Entra ID with MFA.

## Operating checklist

1. Complete the first login of `admin` and set a strong password, or create your own local administrator and delete `admin`.
2. Separate app registrations for sign-in (delegated, ID tokens) and for the Graph connector (application permissions, least privilege as listed in docs/connectors.md).
3. "User assignment required" on the sign-in application; assign named users to Admin or Auditor. `AUTH_DEFAULT_ROLE` empty.
4. Once Entra ID works: `AUTH_LOCAL_ENABLED=false`, or keep one local account for the external auditor and delete it after the verification.
5. `APP_BASE_URL` https, matching the Caddy hostname and the Entra redirect URI.
6. DNS record for the hostname resolvable only where users are (LAN or VPN); do not publish the host to the internet unless the CAB needs remote access, and then only for the verification window.
7. Nightly backup of the `/data` volume (docs/deployment-proxmox.md). Test a restore.
8. Review the activity log (failed sign-ins, user changes) and connector run errors monthly.
9. Update the image monthly (`docker compose pull && docker compose up -d --build`) and read the CI scan results.

## Reporting a vulnerability

See SECURITY.md.
