# Conditional prose-quality lane

Agentflow can add one bounded plain-language editing pass when Claude writes a
reader-facing Markdown artifact. It is designed for blog posts, public
documentation, and learner-facing Instruqt assignments. It is not a display
filter and does not alter ordinary chat, code, research notes, reviews, or
internal handoffs.

The lane is conditional:

1. The controller classifies the deliverable as `reader-facing` or `internal`
   before dispatch and records the writer provider.
2. Claude writes the artifact with the repository's domain skill.
3. `agentflow prose prepare` runs deterministic checks without calling a model.
4. A passing artifact continues unchanged. A failing artifact produces an
   ignored, preflightable handoff for one exact, policy-approved editor session.
5. The editor writes a sibling file. `agentflow prose verify` checks prose and
   rejects changed code fences, inline code, link destinations, or numbers.
6. Domain checks still run. The controller reviews the diff and decides whether
   to replace the original.

There is no Ollama dependency. Agentflow never silently overwrites the source
and never launches a second editing pass. Codex Luna at `medium` is the bundled
default, not a requirement.

## Controller use

When `orchestrate-agents` is active, the controller selects this lane
automatically for Claude-authored reader-facing Markdown unless the user opts
out. The task still needs a matching profile, at least one required domain
skill, and at least one authoritative domain check.

For Claude selected through Copilot, pass both `--writer-provider copilot` and
the exact `--writer-model claude-...` identifier so the classification is
explicit and durable.

For a technical blog draft:

```sh
agentflow prose prepare posts/agentflow.md \
  --profile technical-blog \
  --writer-provider claude \
  --require-skill isovalent-ai-tme-skill \
  --check "python3 scripts/validate_post.py posts/agentflow.edited.md"
```

For an Instruqt assignment:

```sh
agentflow prose prepare tracks/example/01-start/assignment.md \
  --profile instruqt \
  --writer-provider claude \
  --require-skill instruqt-track-author \
  --check "python3 scripts/validate.py tracks/example"
```

If the check passes, the command prints `NO_EDIT` and creates nothing. If it
fails, it prints exact preflight and launch commands. By default the generated
launch is pinned by `models-v1` to:

```text
provider=codex model=gpt-5.6-luna role=editing effort=medium
```

### Use without OpenAI access

Set an exact editor route in `.agentflow/config.json` (shared) or the ignored
`.agentflow/config.local.json` (machine-local). For example, with GitHub
Copilot access:

```json
{
  "schema": "agentflow.project-local@1",
  "version": 1,
  "prose": {
    "editor": {
      "provider": "copilot",
      "model": "claude-sonnet-4.6",
      "effort": "medium",
      "max_ai_credits": 30
    }
  },
  "skills": []
}
```

Claude Code can use `provider=claude`, `model=claude-sonnet-5`, and
`effort=medium`. Both alternatives are present in the bundled model policy.
Custom exact routes work when the project policy explicitly approves the
model for role `editing`.

Override configuration for one invocation by supplying the complete tuple:

```sh
agentflow prose prepare draft.md \
  --profile technical-blog --writer-provider claude \
  --editor-provider copilot --editor-model claude-sonnet-4.6 \
  --editor-effort medium --editor-max-ai-credits 30 \
  --require-skill my-writing-skill --check "./scripts/check-content draft.edited.md"
```

Copilot routes require a numeric cap of at least 30 AI credits. Partial or
policy-unapproved routes fail closed. To use deterministic checks
without any editing model, configure `"prose": {"editor": null}`. A failing
artifact then returns status `edit-required-no-route` and creates no handoff.

The handoff permits only the edited sibling, prohibits research and new claims,
requires the domain skill, and carries the deterministic findings. Run its
preflight, then its printed launch command. Finally verify the returned file:

```sh
agentflow prose verify posts/agentflow.md posts/agentflow.edited.md \
  --profile technical-blog
```

The controller must also run the domain check supplied to `prepare` and inspect
the source-to-edited diff. Only then may it deliberately replace the original.

## Direct check

Use the deterministic checker without preparing a handoff:

```sh
agentflow prose check draft.md --profile technical-blog
agentflow prose check assignment.md --profile instruqt --json
```

Exit status is `0` when the artifact passes, `1` when it needs editing, and `2`
for invalid or unsafe input. Inputs must be regular UTF-8 `.md` or `.markdown`
files no larger than 2 MiB; final-path symlinks are rejected.

The built-in profiles flag oversized sentences and paragraphs, repeated
sentences, and a small stable set of filler phrases. They ignore YAML
frontmatter and fenced code. These checks are routing signals, not a substitute
for editorial or domain judgment.
