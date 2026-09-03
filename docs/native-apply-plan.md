# Connected accounts and native-apply plan

## Decisions

- Keep Dice for now. It is an enabled direct discovery source in
  `src/selfapply/config/sites.yaml`, and the application router already treats
  Dice as a job-board aggregator. Removing its connection card would hide a
  source the product can actually use.
- Remove Monster from the default Connection Center until a Monster discovery
  adapter exists. A login card without discoverable jobs is misleading.
- Keep LinkedIn, Indeed, ZipRecruiter, Glassdoor, and one representative
  Workday connection. Generate connection cards from enabled discovery sources
  where possible so the UI cannot drift from the backend again.
- Build native apply only after the generic worker, resume archive, and queue
  safeguards remain green.

## Phase 1 - connection-source alignment

1. Replace the hard-coded portal list with capabilities: discover, connect,
   native apply, and generic browser apply.
2. Hide a portal when it has neither an enabled discovery source nor queued
   jobs from that host.
3. Remove Monster from defaults. Keep an adapter slot so it can return when a
   tested discovery source is added.
4. Keep Dice and verify that Smart Extract produces stable job and application
   URLs before displaying it as Connected.
5. Add tests proving every default connection has a real discovery or queued-job
   path and that only one Workday card is shown.

## Phase 2 - ZipRecruiter 1-Click pilot

1. Put the feature behind a `native_ziprecruiter_apply` setting, off by default
   until the pilot passes.
2. Detect a true ZipRecruiter 1-Click form. Redirects to employer ATS pages stay
   on the existing generic worker path.
3. Require the exact queued job URL, a live connected session, eligibility,
   duplicate protection, and a valid job-specific tailored PDF before opening
   the form.
4. Fill deterministic profile fields in code. Use the browser agent only for
   unknown screening questions; never invent identity, authorization, salary,
   or demographic answers.
5. Upload the job-specific PDF and archive the exact bytes used.
6. Mark Applied only after a visible receipt or independent browser
   confirmation. Otherwise return the job to Ready with a precise reason.
7. Add fixture-driven tests for success, expired jobs, already-applied jobs,
   session expiry, validation errors, redirects, and security challenges.
8. Pilot on one job at a time and compare completion rate, actions, duration,
   and failures with the generic worker before enabling it broadly.

## Phase 3 - Dice application support

1. Measure Dice discovery URL quality and deduplication first.
2. Classify Dice-hosted quick apply versus employer redirects.
3. Reuse the ZipRecruiter safety contract for any Dice-native flow.
4. Keep external ATS applications on the existing generic worker.

## Phase 4 - Monster, only with a real source

1. Add and test a Monster discovery adapter or supported upstream provider.
2. Verify stable job IDs, descriptions, locations, and application URLs.
3. Restore the connection card only after jobs can reach the queue.
4. Add native apply only if Monster exposes a stable, testable form; otherwise
   use the generic browser worker.

## Release gates

- No applied job can be selected twice.
- No fallback-to-master document can enter the automatic queue.
- Every applied record opens the archived resume used for submission.
- CAPTCHA and security challenges remain human-attention outcomes.
- Portal-session failures stop or defer cleanly instead of consuming the queue.
- All unit tests and PDF integrity checks pass.
