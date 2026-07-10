# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in weatherbot, please report it responsibly.

**Please do not open a public GitHub issue for security vulnerabilities.**

Preferred: use GitHub's private vulnerability reporting — the **"Report a vulnerability"** button under this repository's **Security** tab. It opens a private channel visible only to the maintainer.

Alternatively, email **andrew.brook@fooblah.org**.

Include as much detail as you can:
- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested mitigations

We will acknowledge your report within 48 hours and aim to release a fix within 14 days for critical issues.

## Scope

This policy covers the weatherbot data pipeline, its `ingest` package, and the private Cloud Run MCP Toolbox service in this repository. Secrets (Ambient Weather Network keys, the Cloud SQL password) live in Google Secret Manager and are never committed; a report of any path that would expose them is in scope.
