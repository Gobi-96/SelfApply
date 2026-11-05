# Security policy

## Supported version

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** flow for this repository so the
report and any proof of concept remain private. Do not include API keys,
passwords, candidate profiles, resumes, browser-session data, or other personal
information in a public issue.

## Local data and secrets

SelfApply stores runtime data below `~/.selfapply` by default. Candidate
profiles, resumes, application history, generated documents, logs, browser
profiles, and secrets are local runtime artifacts and must not be committed.
The repository's `.gitignore` and public-release tests guard the common paths,
but contributors should still review every staged file before pushing.

Use fictional data for tests, documentation, screenshots, and bug reports.
Rotate any credential immediately if it is accidentally exposed.

## Browser automation

Run dry mode first and review the result before enabling submission. SelfApply
does not bypass forced login, site blocks, or manual verification requirements.
Users are responsible for the accuracy of submitted information and for
following applicable site terms, rate limits, and law.
