# Security policy

## Supported versions

Security fixes target the latest `0.1.x` release and the default branch. Older snapshots are not
maintained separately.

## Report a vulnerability privately

Do not open a public issue for a vulnerability, exploitable payload, secret, private-repository
content, or credential.

Use GitHub Private Vulnerability Reporting instead:

1. Open this repository's **Security** tab.
2. Choose **Advisories** and then **Report a vulnerability**.
3. Include the affected version or commit, impact, minimal sanitized reproduction, and any suggested
   mitigation.

This creates a private Security Advisory visible to the repository maintainers. If the reporting
button is unavailable, do not move sensitive details to a public issue; contact the repository owner
through their GitHub profile and ask for the private reporting channel without including the
vulnerability details.

Maintainers will validate the report, coordinate a fix and disclosure when appropriate, and credit
reporters who want attribution. The project does not currently promise a fixed response SLA.

## Security boundary

PatchScope treats filenames, archives, source text, pull-request metadata, analyzer output, and
model output as hostile. It rejects unsafe paths and archive entries, bounds files and subprocesses,
uses fixed GitHub hosts and analyzer arguments, sanitizes errors, and never executes submitted
source.

PatchScope is not a kernel sandbox. The included local stack is not an authenticated public hosting
configuration. Read [docs/security.md](docs/security.md) for the complete threat model, storage
privacy notes, and production deployment requirements.

## Safe research

Use synthetic source and accounts you control. Do not access other users' data, degrade a shared
service, publish an unpatched exploit, or include real secrets and private code in a report.
