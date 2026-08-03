# Security policy

## Supported versions

Agentflow is a `0.x` public preview. Security fixes are provided for the latest
released version only. Older versions may receive guidance but are not
guaranteed patches.

## Report a vulnerability

Use GitHub's **Security** tab and select **Report a vulnerability** to open a
private vulnerability report for `saintdle/agentflow`. Do not open a public
issue, pull request, discussion, or Beads item for an undisclosed vulnerability.

Include, when safe:

- affected version and platform;
- prerequisite configuration and threat actor;
- reproduction steps or a minimal proof of concept;
- actual and expected behavior;
- impact and any known mitigations.

Do not include real credentials, private provider transcripts, customer data,
or destructive payloads. Use synthetic test data.

Maintainers aim to acknowledge a report within five business days, provide an
initial assessment within ten business days, and coordinate disclosure after a
fix or mitigation is available. These are targets, not a service-level
agreement. Please allow a reasonable remediation window before disclosure.

## Scope

Security-sensitive areas include controller authority and fencing, provider
handoffs, filesystem boundaries, subprocess isolation, imported assets,
credential handling, hook installation, Beads state mutation, and redaction.

Provider services, provider CLI vulnerabilities, Beads, Herdr, GitHub, and
other dependencies should normally be reported to their owners. Report an
Agentflow issue when Agentflow integrates with them unsafely.

The detailed threat model and platform limitations are in
[docs/SECURITY.md](docs/SECURITY.md).
