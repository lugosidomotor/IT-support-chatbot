# Security policy

This project is a **local proof of concept**. It is designed to be strictly read-only
(see [Read-only security model](README.md#read-only-security-model)) and binds to
`127.0.0.1` by default. It has no user authentication: do not expose it to untrusted networks.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/lugosidomotor/IT-support-chatbot/security/advisories/new)
instead, and include steps to reproduce and the impact.

Particularly interesting findings:

- any way to make the agent perform a write, delete or transition in GitHub, Jira or Confluence;
- any way to reach a repository outside `GITHUB_REPOS`, or a host other than the trusted endpoints;
- credentials (OpenRouter, GitHub, Atlassian) leaking into logs, API responses or model requests;
- prompt injection that bypasses the tool policy or exfiltrates data through the UI.

## Handling secrets

Credentials are read only from environment variables / `.env` (git-ignored). Never commit a `.env`
file; `.env.example` must stay free of real values.
