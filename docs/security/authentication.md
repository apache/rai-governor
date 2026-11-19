# Authentication and sessions

## Sign-in pathways

Providers are configured in `auth.yaml` (`AUTH_CONFIG_PATH`). Only the enabled ones are offered. The site's effective strength is that of the weakest enabled pathway: enabling an `open_*` mode on any provider admits everyone that provider admits.

| Provider | Who gets in | What is checked |
|---|---|---|
| `asf` | Today: only uids in `site_admins` (the committer path is commented out in `AsfProvider.evaluate_login`, "Admin-only for testing") | oauth.apache.org sign-in; LDAP committer and PMC groups; `cn=asf-banned` denies, even a site admin |
| `github` | `allowlist` (default): active members of `allowed_orgs`. `open_github`: any GitHub user | OAuth with `read:user`, `user:email`, `read:org`; org membership state must be `active` |
| `google` | `allowlist`: members of `allowed_groups`. `open_domain`: anyone in `hosted_domain` | OAuth; the hosted domain is checked again on the server, not just in the authorize URL |
| `microsoft` | `allowlist`: members of `allowed_groups`. `open_tenant`: anyone in the tenant | OAuth against the configured tenant; transitive group membership |
| `dev_stub` | Any configured user, picked from a list | Nothing. Registered only when `APP_ENV` is `local`, `dev` or `test` |
| Legacy Firebase | Firebase users | The API accepts a Firebase token only when `APP_ENV=firebase`. `legacy_firebase_enabled` in `auth.yaml` only decides whether the login page offers it |

Multi-factor authentication is the identity provider's: Governor does not add a factor of its own, and the providers don't tell it whether one was used.

A login that every provider refuses never gets a session. Every login, refused login and logout is recorded as an observability event (`auth_success`, `auth_failure`, `auth_logout`, and `auth_sessions_ended` when all of a user's sessions are ended) with the user, the provider and, for a refusal, why. The OAuth code, the state and the return path are never logged.

## The flow

1. `GET /auth/login/{provider}` sets two cookies for 10 minutes: `gofannon_auth_state` (a random state) and, if given, `gofannon_return_to`. `return_to` is kept only when it is a path on this site: one leading `/`, no backslash, no control characters, at most 2048 characters.
2. The provider sends the browser to `/auth/callback/{provider}`. The state cookie must be present. When the provider echoes `state`, it must match (ASF doesn't echo it).
3. The code is exchanged, memberships are read, and the provider's policy allows or refuses the login.
4. On allow, a session is created and the browser goes to `FRONTEND_URL` plus the return path, or `/`.

The `redirect_uri` sent to the provider is `AUTH_CALLBACK_BASE_URL` + `/auth/callback/{provider}` when that is set, and is otherwise built from the request's Host header. Production should set it.

## Sessions

| | |
|---|---|
| Cookie | `gofannon_sid`: 32 random bytes, `HttpOnly`, `SameSite=Lax`, `Secure` when the request came over HTTPS (uvicorn reads that from `X-Forwarded-Proto`) |
| Stored as | A doc in `user_sessions` whose id is the SHA-256 of the cookie value. The value itself is not stored, so reading the database doesn't give anyone a session |
| Lifetime | `session_ttl_hours` in `auth.yaml` (default 24), counted from login |
| Idle timeout | `session_idle_hours` in `auth.yaml` (default 8; 0 turns it off). A session unused that long is refused and deleted. To avoid a write on every request, last use is recorded only when the session is written anyway, at the membership refresh (every `workspace_refresh_minutes`, whether or not anything changed), so it is accurate to about 15 minutes: a session can end that much before it has been idle for the full time. The limit is never shorter than two refresh intervals |
| Concurrent sessions | Unlimited. Signing in elsewhere doesn't end other sessions |
| Ending all of a user's sessions | "Sign out everywhere" in the profile menu (`POST /auth/logout-everywhere`) deletes every session of the signed-in user, this one included, and clears the cookie as logout does. A site admin can end every session of any uid with `POST /auth/admin/users/{uid}/end-sessions` (API only; there is no page for it); anyone else gets 403, including a site admin viewing as a regular user. Both are recorded as `auth_sessions_ended` with who, whose sessions and how many. Sessions are found by an index on `userUid` |
| Memberships | Read at login, then read again on the first request after `workspace_refresh_minutes` (default 15). This applies only to providers that can look them up without the user present: ASF (through LDAP) and `dev_stub`. GitHub, Google and Microsoft keep no token to do it with, so their sessions keep the memberships they logged in with. When LDAP fails, the session keeps what it had. A user banned since login has their session deleted |
| Site-admin flag | Recomputed from `site_admins` at the same refresh |
| Logout | Deletes this session and clears the cookie, with `Clear-Site-Data: "cookies", "storage", "cache"` |
| Expired sessions | Refused and deleted when next presented. Every API worker also sweeps hourly, starting at a random time and with a random spread of a few minutes, deleting sessions past their lifetime or idle limit (`session_sweep` event with the count). The sweep reads only what the indexes on `expiresAt` and `lastUsedAt` find and checks each session again before deleting it, so workers sweeping at once is harmless. It runs only when a sign-in provider is configured, and needs CouchDB: the other database backends answer only equality queries, so there it finds nothing |

### With the identity provider

Governor's session and the provider's session are independent. Signing out of Governor doesn't sign the user out of the provider, and signing out of the provider (or being removed there) doesn't end the Governor session. For ASF, removal from a group or a ban is picked up at the next membership refresh. For the other providers it applies only at the next sign-in, at most `session_ttl_hours` later. A site admin who needs it sooner can end that user's sessions.

### Not decided yet

- A way for a user or an admin to list sessions and end one of them (ending all of a user's sessions at once is done).
- Step-up authentication for site-admin actions.

## Site admins and viewing as a user

Site admins are listed in `site_admins` in `auth.yaml`. A site admin can switch to viewing as a regular user (`/auth/view-as`), which only drops privileges.

`services/audit_service.py` stores site-admin actions in `site_admin_audit`, append-only by convention: there is no update or delete API, but nothing in storage prevents either. Nothing writes to it yet: wiring it into the routes where a site admin acts outside their own workspaces is still to do.
