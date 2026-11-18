# Browsers

Governor expects a current browser: Chrome or Edge 107, Firefox 104, Safari 16, or later. It doesn't check, warn or block; what happens without each feature is below.

| Feature | Set by | Without it |
|---|---|---|
| JavaScript modules (ES2020+) | The UI build (Vite's default target) | The UI doesn't load: the page stays blank. There is no fallback build |
| TLS 1.2 or 1.3 | Apache: `SSLProtocol -all +TLSv1.2 +TLSv1.3`; plain HTTP is redirected to HTTPS | The browser can't connect |
| HSTS | Apache, on the canonical host name only (`max-age=63072000; includeSubDomains`). The host name in use today sends none, on purpose | The first visit over plain HTTP is redirected, so someone on the network can intercept that first request (SSL stripping) |
| `SameSite=Lax` cookies | The API: the session, OAuth state and return-path cookies, all `HttpOnly` and `Secure` | Cookies go with requests other sites start, so those requests act as the signed-in user (cross-site request forgery) |
| `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'` | Apache and nginx; the CSP only nginx | Another site could frame the UI (clickjacking) |
| `X-Content-Type-Options: nosniff` | Apache and nginx | The browser may guess content types |
| `Referrer-Policy: same-origin` | Apache and nginx | An older browser sends full URLs, run ids included, to other sites as the referrer |
| `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy: same-origin` | Apache | Another site's window keeps a handle on Governor's, or embeds its responses |
| CSP `object-src 'none'`, `base-uri 'self'` | nginx | Plugins and a `<base>` tag injected into the page aren't blocked |
| `Clear-Site-Data` on logout | The API | Local storage and cached pages stay after logout; the session itself still ends, since it is deleted on the server |

Apache and nginx both set the headers they share. The CSP is set only by nginx, so it is the same wherever nginx serves the UI. It doesn't restrict scripts: see [Components and risky code](components.md).
