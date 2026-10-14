# Security Policy

## Supported versions

Only the latest `main` and the most recent tagged release receive fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them
privately through GitHub's
[private vulnerability reporting](https://github.com/vchatela-org/tributary/security/advisories/new),
with the affected version or commit, how to reproduce it, and the impact
you believe it has. Expect an acknowledgement within 7 days and an
assessment within 30 days.

## Scope and design

- **No login, by design.** Tributary has no user accounts: run it on a
  machine only you reach, or behind an authenticating reverse proxy
  ([docs/deployment.md](docs/deployment.md)). Its port is bound to
  `127.0.0.1` in the provided `docker-compose.yml`. State-changing requests
  coming from another site (by `Sec-Fetch-Site`) are refused, so a web page
  can't start a run in your browser.
- **Secrets** (API keys, tokens) are read from the environment only. They
  are never written to its database, logged, or returned by its API or
  pages. A connector sends them only to its own source; prefer `https://`
  or a private network for that link.
- **What reaches InfluxDB** is numbers and low-cardinality tags, plus book
  or show titles: never emails, IP addresses, tokens or free text
  ([docs/schema.md](docs/schema.md)). InfluxDB 1.x often runs without
  authentication: keep it off public networks.
- Vulnerabilities in a source application (Audiobookshelf...), InfluxDB or
  Grafana belong to those projects.

## Automated checks

- Tests on every push and pull request, including checks that no secret,
  email or IP address is stored, logged or written.
- A privacy deny-list scan of the tree and the whole history, and gitleaks.
- CodeQL (`python`, `actions`, `security-extended`) on every push, pull
  request and weekly, and dependency review on pull requests.
- Secret scanning with push protection, and Dependabot security updates.
- `main` takes changes through pull requests only, with these checks
  required; release tags (`v*`) can't be moved or deleted.
- Dependabot updates for pip, Docker, Docker Compose and GitHub Actions.
