# Security policy

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** flow in the repository Security
tab. Do not open a public issue for authentication bypasses, token exposure,
unsafe archive extraction, path traversal or remote-code-execution findings.

Include the affected revision, reproduction steps, impact and any suggested
mitigation. Do not include real RomM tokens, passwords, ROMs or personal save
data in the report.

## Deployment boundaries

- Treat RomM Client API Tokens as passwords and store them in mode-0600 files.
- Use a separate device token for every user/device installation.
- Keep the Bridge on a trusted network or behind an authenticated reverse proxy.
- The shared catalogue intentionally contains metadata and local asset URLs, but
  must never contain tokens, provider URLs carrying credentials, ROMs or BIOS.
- Review scripts fetched through `curl | bash` or `irm | iex` before running them.
- The optional Windows SSH helper requires a deployer-supplied public key; this
  repository does not ship an authorized key.

Only the current `main` branch is supported until versioned releases begin.
