---
name: diagnosing-bugs
description: Diagnose reproducible defects, failing tests, regressions, flaky behavior, performance problems, and unexpected runtime results through an evidence-led feedback loop. Use when the user asks to diagnose, debug, investigate, find a root cause, or fix a reported defect. Diagnose without editing unless the request also authorizes a fix.
---

# Diagnose Bugs

## Establish authority and context

1. Determine whether the request authorizes diagnosis only or diagnosis plus a fix. Do not edit product code for a diagnosis-only request.
2. Read repository instructions, relevant domain skills, nearby tests, `CONTEXT.md`, and applicable ADRs before changing anything.
3. Check active Beads state. When a bug bead exists, keep its symptom, reproduction, evidence, and disposition current. Store distilled facts and artifact paths, not raw logs or transcripts.

## Build the feedback loop

Before forming a cause theory, create one command that exercises the reported path and can distinguish the exact failure from success.

Prefer, in order:

1. An existing focused unit, integration, or end-to-end test.
2. A minimal CLI, API, browser, or fixture-driven reproduction.
3. A deterministic replay, differential comparison, or benchmark.
4. A bounded stress loop for intermittent behavior.
5. A temporary harness around the narrowest real interface.

The loop must be:

- **Red-capable:** it detects the user's symptom, not merely a nearby error.
- **Repeatable:** repeated runs produce a trustworthy verdict; for flaky defects, record the reproduction rate.
- **Focused:** unrelated setup and noise are removed.
- **Agent-runnable:** it can run unattended within the authorized environment.

Run it and capture the command plus concise observed evidence. If no trustworthy loop can be built, stop with the missing environment, artifact, access, or user action needed. Do not replace reproduction with speculation.

## Minimize and explain

1. Reduce the input, configuration, callers, and environment while preserving the failure.
2. Write three to five ranked, falsifiable hypotheses. For each, state the observation that would support or refute it.
3. Test one variable at a time. Prefer debuggers, traces, profilers, and narrow boundary logging over broad log collection.
4. Mark temporary instrumentation with a unique searchable tag.
5. Identify the root cause only when evidence rules in one explanation and rules out credible alternatives.

For diagnosis-only work, stop here and return the cause, evidence, impact, and smallest recommended correction. Clearly label anything still unverified.

## Fix when authorized

1. Choose a regression-test seam that observes public behavior and represents the real failure. If no sound seam exists, document that architectural limitation instead of adding a misleading test.
2. Convert the minimized reproduction into a failing test and run it red.
3. Make the smallest correction that addresses the established cause. Do not bundle opportunistic refactors.
4. Run the regression test green, then rerun the original unminimized loop.
5. Run affected repository checks.

## Clean up and return

- Remove all tagged instrumentation and throwaway harnesses unless the user approved keeping them.
- Record the correct hypothesis and evidence in the bead, commit, or PR.
- Report: outcome; diagnosis or fix; reproduction command; root-cause evidence; changed files and commit when applicable; checks; residual risks; and untested lanes.
- If blocked, return one decision or dependency, evidence tried, available options, recommendation, current branch/check state, and remaining risk.
