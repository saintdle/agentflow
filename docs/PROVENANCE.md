# Authorship and third-party provenance

## AI-assisted authorship

The majority of Agentflow was generated with AI coding agents and then revised
through multi-agent review. Codex, Claude, and GitHub Copilot agents have all
contributed to the implementation. Agentflow was also used to coordinate parts
of its own implementation and review.

Those facts are disclosed so users can assess the project's maturity and risk.
Passing tests and independent agent reviews do not establish correctness or
suitability. The project remains development/testing software and requires
human review before every privileged or consequential action.

Git commits may identify the human operator who accepted a change; they should
not be interpreted as proof that every line was manually authored.

## Adapted skills

The following bundled skills adapt material from Matt Pocock's
[`mattpocock/skills`](https://github.com/mattpocock/skills) project at inspected
snapshot `2ab958093e83e0ec752e6c1c5932da465bf23e0c`, licensed under MIT:

| Agentflow skill | Upstream path | Relationship |
| --- | --- | --- |
| `to-tickets` | `skills/engineering/to-tickets/SKILL.md` | Adapted tracer-bullet tickets, blocking edges, review, and tracker publication into Beads-first Agentflow graphs. |
| `code-review` | `skills/engineering/code-review/SKILL.md` | Adapted review workflow ideas into Agentflow's repository/specification review gates. |
| `diagnosing-bugs` | `skills/engineering/diagnosing-bugs/SKILL.md` | Adapted debugging workflow ideas into Agentflow's evidence-led diagnosis flow. |
| `wayfinder` | `skills/engineering/wayfinder/SKILL.md` | Adapted decision-mapping workflow ideas for work too uncertain or large to implement directly. |

Matt Pocock is the upstream author. The upstream author and project do not
endorse Agentflow. Each adapted skill contains its own `PROVENANCE.md` and full
`THIRD_PARTY_NOTICES.md`; the distribution-level MIT notice is retained in the
repository's [third-party notices](../THIRD_PARTY_NOTICES.md).

## Other dependencies and integrations

Agentflow invokes or integrates with independently maintained tools including
Beads, Herdr, Git, GitHub CLI, Codex, Claude Code, and GitHub Copilot CLI. They
are not incorporated into Agentflow's source merely because the CLI can invoke
them. Python package dependencies are declared in `pyproject.toml`; CI actions
are pinned in `.github/workflows`.

## Maintenance rule

Any future code, skill text, template, or substantial workflow adapted from an
external author must be recorded before release with the author, canonical
source URL and path, inspected revision, licence, relationship, and local
modifications. Required licence text must ship with both the source tree and
built distribution. `scripts/validate.py` enforces the current adapted-skill
inventory.
