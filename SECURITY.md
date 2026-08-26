# Security Policy

SMS and one-time passwords are sensitive data. Do not include real API keys,
Feishu credentials, phone numbers, message content, database files, or server
configuration in public issues.

If you discover a vulnerability, please use GitHub's private vulnerability
reporting or open a private security advisory for this repository. Include the
affected version, reproduction steps, impact, and a minimal sanitized example.

Before deploying, use independent random values for `SMS_RELAY_API_KEY` and
`SMS_RELAY_READ_API_KEY`; keep `.env` outside
version control, expose the application only through an HTTPS reverse proxy,
and restrict access to the SQLite data directory.

Browser entry authorization is owned exclusively by the configured central
OAuth issuer. Do not add a second local Open ID or Union ID allowlist. The
browser must only receive an opaque host-only `Secure`, `HttpOnly`,
`SameSite=Lax` session handle; central access and refresh tokens remain in the
server-side SQLite session store. Treat that database as credential material.

Protected requests fail closed when introspection is inactive, malformed, or
unavailable. Refresh is serialized per service process and rotating refresh
handles are replaced atomically. Application logout revokes the central refresh
grant before deleting the local session. Optional Feishu bot credentials are
only for group notifications and must never be reused for browser login.
