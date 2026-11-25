# Security policy

## Supported versions

Security fixes are applied to the latest published version tag. Older tags
remain available as immutable images for rollback, but do not receive fixes.

## Reporting a vulnerability

Report vulnerabilities privately through the repository's GitHub Security
Advisories page. Do not open a public issue for an unpatched vulnerability.
Include the affected image tag or commit, reproduction steps, and impact.

## Automated scanning

CI scans:

* the locked Python dependency graph (`uv.lock`) with Trivy, and
* the built container image with Trivy.

Scans report `HIGH` and `CRITICAL` findings with fixes (`ignore-unfixed: true`
is set) and upload the results to the workflow log. Findings are advisory in CI
so that a new upstream advisory does not block unrelated pull requests, but the
release manager must review the scans for the tagged commit before publishing.
A release tag must not be pushed while an un-triaged `CRITICAL` finding is
present.

## Triage and exceptions

For each reported finding, either:

1. upgrade the dependency or base image and let Dependabot/CI revalidate it, or
2. record a temporary exception in `.trivyignore` with the CVE, a short
   justification, an owner, and an expiry date in a comment.

Expired exceptions must be removed or renewed with a fresh justification.
Exceptions that affect the container image are reviewed again at every release.
Trivy ignores unfixed findings automatically; that is not an exception and
does not need an entry.

## Release integrity

Publishing is limited to pushed `vMAJOR.MINOR.PATCH` tags. The workflow
validates the exact tagged commit before pushing, and every image includes
provenance and an SBOM attestation identifying the source commit. Version tags
are never overwritten. Roll back by deploying a previous version tag or its
`sha256` digest; see the README for commands.
