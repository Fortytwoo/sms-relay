# Security Policy

SMS and one-time passwords are sensitive data. Do not include real API keys,
Feishu credentials, phone numbers, message content, database files, or server
configuration in public issues.

If you discover a vulnerability, please use GitHub's private vulnerability
reporting or open a private security advisory for this repository. Include the
affected version, reproduction steps, impact, and a minimal sanitized example.

Before deploying, use independent random values for `SMS_RELAY_API_KEY` and
`SMS_RELAY_SESSION_SECRET`, keep `.env` outside version control, expose the
application only through an HTTPS reverse proxy, and restrict access to the
SQLite data directory.
