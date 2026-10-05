# Microsoft Entra ID single sign-on

The application authenticates users with OpenID Connect against one Entra ID tenant and takes the role from Entra app roles. Configuration is entirely through environment variables. Entra ID is optional: local accounts (`AUTH_LOCAL_ENABLED`, default on) work without it, and both can be active at the same time; the sign-in page then shows a Microsoft button and the local form.

## 1. App registration

Entra admin center → Identity → Applications → App registrations → New registration.

| Field | Value |
|---|---|
| Name | CyFun Basic (or any name) |
| Supported account types | Accounts in this organizational directory only (single tenant) |
| Redirect URI | Platform **Web**, `https://<APP_HOSTNAME>/auth/callback` |

After creation note the **Application (client) ID** and the **Directory (tenant) ID**.

Authentication → Implicit grant and hybrid flows: tick **ID tokens**. (The code flow with PKCE is used; the ID token is returned from the token endpoint, so this setting is what Entra requires for `openid` scope responses.)

Certificates & secrets → New client secret. Copy the value into `AUTH_CLIENT_SECRET`. Set a reminder for its expiry.

Token configuration: nothing to add. The ID token carries `oid`, `tid`, `preferred_username`, `name` and `roles` by default.

## 2. App roles

App roles → Create app role, twice:

| Display name | Allowed member types | Value | Description |
|---|---|---|---|
| Admin | Users/Groups | `Admin` | Full access: scores, registers, connectors, exports |
| Auditor | Users/Groups | `Auditor` | Read-only access to every screen and export |

Values are matched case-insensitively.

## 3. Assign users

Enterprise applications → CyFun Basic → Properties → **Assignment required? Yes**. Then Users and groups → Add user/group, pick the user or group and the role. A user without an assignment cannot sign in at all; a user with an assignment but without a known role is refused by the application ("No role assigned").

Groups can be assigned to roles when the tenant licence allows group assignment; otherwise assign users directly.

## 4. Environment variables

```
APP_BASE_URL=https://cyfun.internal.example
AUTH_TENANT_ID=<Directory (tenant) ID>
AUTH_CLIENT_ID=<Application (client) ID>
AUTH_CLIENT_SECRET=<client secret value>
AUTH_DEFAULT_ROLE=            # leave empty
SESSION_IDLE_MINUTES=480
SESSION_ABSOLUTE_HOURS=12
```

`AUTH_DEFAULT_ROLE=admin` grants Admin to any authenticated tenant user without an app role. Use it only during the first setup, then clear it.

## 5. What the application validates

* ID token signature against the tenant's JWKS (fetched from the OIDC discovery document, cached one hour, refreshed on unknown key id)
* `iss` equals `https://login.microsoftonline.com/<tenant>/v2.0`
* `aud` equals the client ID
* `exp`, `nbf`, `iat` with 60 seconds leeway
* `nonce` equals the value stored at login
* `tid` equals `AUTH_TENANT_ID`
* `state` matches a stored, unexpired login attempt (10 minutes); PKCE `code_verifier` is sent with the code

## 6. Sign-out

Sign out ends the application session and clears the cookie. The Microsoft session in the browser is not ended; users on shared devices should sign out of Microsoft as well.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `AADSTS50011` redirect URI mismatch | `APP_BASE_URL` plus `/auth/callback` differs from the registered URI (scheme, host or port). |
| `AADSTS700016` application not found | `AUTH_CLIENT_ID` or `AUTH_TENANT_ID` wrong. |
| "No role assigned" page | User has no Admin or Auditor app role assignment. |
| "ID token validation failed" | Clock skew above 60 seconds, wrong tenant ID, or the app registration is multi-tenant. |
| 503 "Single sign-on is not configured" | One of the three AUTH variables is empty. |
