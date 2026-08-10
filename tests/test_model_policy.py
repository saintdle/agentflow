from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import model_policy as mp


REPO_ROOT = Path(__file__).resolve().parents[1]


def _base_document() -> dict:
    return json.loads(mp.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


class LoadPolicyTests(unittest.TestCase):
    def test_loads_shipped_policy_document(self) -> None:
        policy = mp.load_policy()
        self.assertEqual(policy.schema, mp.SCHEMA)
        self.assertEqual(policy.version, 1)
        self.assertEqual(policy.id, "models-v1")
        self.assertIn("controller", policy.roles)
        self.assertTrue(policy.routes)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.load_policy(REPO_ROOT / "policies" / "does-not-exist.json")


class ParsePolicyTests(unittest.TestCase):
    def test_rejects_wrong_schema(self) -> None:
        doc = _base_document()
        doc["schema"] = "something.else"
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_unsupported_version(self) -> None:
        doc = _base_document()
        doc["version"] = 2
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_unknown_top_level_field(self) -> None:
        doc = _base_document()
        doc["extra"] = "nope"
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_unknown_route_field(self) -> None:
        doc = _base_document()
        doc["routes"][0]["extra"] = "nope"
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_route_with_undefined_role(self) -> None:
        doc = _base_document()
        doc["routes"][0]["roles"] = ["not-a-role"]
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_duplicate_roles_list(self) -> None:
        doc = _base_document()
        doc["roles"].append(doc["roles"][0])
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_duplicate_route_for_same_provider_model(self) -> None:
        doc = _base_document()
        doc["routes"].append(copy.deepcopy(doc["routes"][0]))
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_model_both_approved_and_forbidden(self) -> None:
        doc = _base_document()
        doc["forbidden_models"].append(doc["routes"][0]["model"])
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)

    def test_rejects_non_mapping_document(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(["not", "a", "mapping"])

    def test_rejects_empty_routes(self) -> None:
        doc = _base_document()
        doc["routes"] = []
        with self.assertRaises(mp.ModelPolicyError):
            mp.parse_policy(doc)


class ValidateRouteMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = mp.load_policy()

    def _assert_pass(self, **kwargs) -> None:
        result = self.policy.validate_route(**kwargs)
        self.assertTrue(result.ok, f"expected pass for {kwargs}, got: {result.reason}")

    def _assert_fail(self, **kwargs) -> None:
        result = self.policy.validate_route(**kwargs)
        self.assertFalse(result.ok, f"expected fail for {kwargs}, got ok with route {result.route}")

    # --- approved exact routes ---

    def test_codex_sol_controller_passes(self) -> None:
        self._assert_pass(provider="codex", role="controller", model="gpt-5.6-sol", effort="high")

    def test_codex_sol_judgment_passes(self) -> None:
        self._assert_pass(provider="codex", role="judgment", model="gpt-5.6-sol", effort="xhigh")

    def test_codex_luna_coding_passes(self) -> None:
        self._assert_pass(provider="codex", role="coding", model="gpt-5.6-luna", effort="medium")

    def test_claude_opus_4_8_controller_passes(self) -> None:
        self._assert_pass(provider="claude", role="controller", model="claude-opus-4-8", effort="high")

    def test_claude_opus_5_medium_judgment_passes(self) -> None:
        self._assert_pass(provider="claude", role="judgment", model="claude-opus-5", effort="medium")

    def test_claude_opus_5_explicit_low_effort_passes(self) -> None:
        self._assert_pass(provider="claude", role="judgment", model="claude-opus-5", effort="low")

    def test_claude_sonnet_coding_passes(self) -> None:
        self._assert_pass(provider="claude", role="coding", model="claude-sonnet-5", effort="medium")

    def test_copilot_opus_4_8_controller_passes(self) -> None:
        self._assert_pass(provider="copilot", role="controller", model="claude-opus-4.8", effort="high")

    def test_copilot_opus_5_medium_review_passes(self) -> None:
        self._assert_pass(provider="copilot", role="review", model="claude-opus-5", effort="medium")

    def test_copilot_sonnet_coding_passes(self) -> None:
        self._assert_pass(provider="copilot", role="coding", model="claude-sonnet-4.6", effort="high")

    def test_copilot_rejects_direct_claude_4_8_id(self) -> None:
        self._assert_fail(provider="copilot", role="controller", model="claude-opus-4-8", effort="high")

    def test_claude_rejects_copilot_4_8_id(self) -> None:
        self._assert_fail(provider="claude", role="controller", model="claude-opus-4.8", effort="high")

    def test_role_omitted_effort_still_resolves(self) -> None:
        self._assert_pass(provider="codex", role="coding", model="gpt-5.6-luna", effort=None)

    # --- forbidden / disallowed tiers ---

    def test_codex_terra_fails(self) -> None:
        self._assert_fail(provider="codex", role="coding", model="gpt-5.6-terra", effort="medium")

    def test_claude_haiku_fails(self) -> None:
        self._assert_fail(provider="claude", role="coding", model="haiku", effort="medium")

    def test_claude_haiku_exact_id_fails(self) -> None:
        self._assert_fail(provider="claude", role="coding", model="claude-haiku-4-5-20251001", effort="medium")

    def test_copilot_auto_fails(self) -> None:
        self._assert_fail(provider="copilot", role="coding", model="auto", effort="medium")

    # --- empty, generic, unresolved, lookalikes ---

    def test_empty_model_fails(self) -> None:
        self._assert_fail(provider="codex", role="coding", model="", effort="medium")

    def test_generic_gpt_5_6_fails(self) -> None:
        self._assert_fail(provider="codex", role="controller", model="gpt-5.6", effort="high")

    def test_generic_opus_alias_fails(self) -> None:
        self._assert_fail(provider="claude", role="controller", model="opus", effort="high")

    def test_generic_sonnet_alias_fails(self) -> None:
        self._assert_fail(provider="claude", role="coding", model="sonnet", effort="medium")

    def test_unresolved_placeholder_fails(self) -> None:
        self._assert_fail(provider="codex", role="controller", model="unresolved", effort="high")

    def test_template_placeholder_fails(self) -> None:
        self._assert_fail(provider="codex", role="controller", model="${MODEL}", effort="high")

    def test_lookalike_digit_for_letter_fails(self) -> None:
        # "so1" (digit one) instead of "sol" (letter l).
        self._assert_fail(provider="codex", role="controller", model="gpt-5.6-so1", effort="high")

    def test_lookalike_case_mismatch_fails(self) -> None:
        self._assert_fail(provider="claude", role="controller", model="Claude-Opus-5", effort="high")

    def test_lookalike_trailing_space_fails(self) -> None:
        self._assert_fail(provider="claude", role="controller", model="claude-opus-5 ", effort="high")

    def test_wrong_role_for_model_fails(self) -> None:
        # Sonnet is approved for coding/exploration, not controller.
        self._assert_fail(provider="claude", role="controller", model="claude-sonnet-5", effort="high")

    def test_wrong_effort_for_model_fails(self) -> None:
        self._assert_fail(provider="claude", role="controller", model="claude-opus-5", effort="minimal")

    def test_unknown_provider_fails(self) -> None:
        self._assert_fail(provider="gemini", role="coding", model="claude-sonnet-5", effort="medium")


class ProfileAuditTests(unittest.TestCase):
    """Runs the audit against the real shipped profiles in this repository."""

    def setUp(self) -> None:
        self.policy = mp.load_policy()

    def test_discovers_all_managed_shipped_profiles(self) -> None:
        profiles = mp.discover_profiles(REPO_ROOT)
        rel_paths = {mp._relative(path, REPO_ROOT) for path, _provider in profiles}
        self.assertEqual(
            rel_paths,
            {
                ".codex/agents/agentflow-controller.toml",
                ".codex/agents/agentflow-explorer.toml",
                ".codex/agents/agentflow-pr-gatekeeper.toml",
                ".codex/agents/agentflow-reviewer.toml",
                ".claude/agents/agentflow-controller.md",
                ".claude/agents/agentflow-explorer.md",
                ".claude/agents/agentflow-pr-gatekeeper.md",
                ".claude/agents/agentflow-reviewer.md",
                ".github/agents/agentflow-controller.agent.md",
                ".github/agents/agentflow-explorer.agent.md",
                ".github/agents/agentflow-pr-gatekeeper.agent.md",
                ".github/agents/agentflow-reviewer.agent.md",
            },
        )

    def test_discover_maps_github_agent_profiles_to_copilot_provider(self) -> None:
        profiles = dict(
            (mp._relative(path, REPO_ROOT), provider) for path, provider in mp.discover_profiles(REPO_ROOT)
        )
        self.assertEqual(profiles[".github/agents/agentflow-reviewer.agent.md"], "copilot")

    def test_detects_every_shipped_profile_violation(self) -> None:
        violations = mp.audit_profiles(REPO_ROOT, self.policy)
        flagged = {violation.path for violation in violations}
        # Provider profiles migrated by the integration are compliant.  The
        # two Codex files remain visible here when the managed workspace is
        # mounted read-only; audit must still report those exact files rather
        # than silently treating them as migrated.
        codex_paths = {
            mp._relative(path, REPO_ROOT)
            for path, provider in mp.discover_profiles(REPO_ROOT)
            if provider == "codex"
        }
        self.assertTrue(flagged <= codex_paths)
        by_path = {violation.path: violation for violation in violations}
        for rel, violation in by_path.items():
            self.assertTrue(violation.model)
            self.assertIn(self.policy.id, violation.reason)

    def test_compliant_profile_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex" / "agents").mkdir(parents=True)
            (root / ".codex" / "agents" / "agentflow-controller.toml").write_text(
                'name = "agentflow-controller"\nmodel = "gpt-5.6-sol"\nmodel_reasoning_effort = "high"\n',
                encoding="utf-8",
            )
            violations = mp.audit_profiles(root, self.policy)
            self.assertEqual(violations, [])

    def test_role_mismatch_is_flagged_even_for_an_otherwise_approved_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex" / "agents").mkdir(parents=True)
            # gpt-5.6-luna is an approved exact model, but not for the
            # controller role, which is reserved for gpt-5.6-sol.
            (root / ".codex" / "agents" / "agentflow-controller.toml").write_text(
                'name = "agentflow-controller"\nmodel = "gpt-5.6-luna"\n', encoding="utf-8"
            )
            violations = mp.audit_profiles(root, self.policy)
            self.assertEqual(len(violations), 1)
            self.assertIn("role", violations[0].reason)

    def test_missing_model_field_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude" / "agents").mkdir(parents=True)
            (root / ".claude" / "agents" / "agentflow-reviewer.md").write_text(
                "---\nname: agentflow-reviewer\n---\nbody\n", encoding="utf-8"
            )
            violations = mp.audit_profiles(root, self.policy)
            self.assertEqual(len(violations), 1)
            self.assertIn("declares no model", violations[0].reason)


class MigrationPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = mp.load_policy()

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        return {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def test_never_writes_any_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex" / "agents").mkdir(parents=True)
            (root / ".codex" / "agents" / "agentflow-controller.toml").write_text(
                'name = "agentflow-controller"\nmodel = "gpt-5.6"\n', encoding="utf-8"
            )
            before = self._snapshot(root)
            mp.plan_migration(root, self.policy)
            after = self._snapshot(root)
            self.assertEqual(before, after)

    def test_proposes_update_for_noncompliant_managed_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex" / "agents").mkdir(parents=True)
            path = root / ".codex" / "agents" / "agentflow-controller.toml"
            path.write_text('name = "agentflow-controller"\nmodel = "gpt-5.6"\n', encoding="utf-8")
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(len(actions), 1)
            action = actions[0]
            self.assertEqual(action.action, "propose_update")
            self.assertEqual(action.current_model, "gpt-5.6")
            self.assertIn(action.proposed_model, self.policy.approved_models("codex"))

    def test_skips_already_compliant_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex" / "agents").mkdir(parents=True)
            path = root / ".codex" / "agents" / "agentflow-reviewer.toml"
            path.write_text('name = "agentflow-reviewer"\nmodel = "gpt-5.6-sol"\n', encoding="utf-8")
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "skip_compliant")
            self.assertEqual(actions[0].proposed_model, "gpt-5.6-sol")

    def test_migration_plan_is_role_aware_for_real_shipped_profiles(self) -> None:
        actions = mp.plan_migration(REPO_ROOT, self.policy)
        by_path = {action.path: action for action in actions}

        # controller role -> the judgment/controller-tier model, not the coding tier.
        self.assertEqual(by_path[".codex/agents/agentflow-controller.toml"].proposed_model, "gpt-5.6-sol")
        self.assertEqual(by_path[".claude/agents/agentflow-controller.md"].proposed_model, "claude-opus-4-8")
        self.assertEqual(
            by_path[".github/agents/agentflow-controller.agent.md"].proposed_model, "claude-opus-4.8"
        )

        # exploration role -> the coding/execution-tier model.
        self.assertEqual(by_path[".codex/agents/agentflow-explorer.toml"].proposed_model, "gpt-5.6-luna")
        self.assertEqual(by_path[".claude/agents/agentflow-explorer.md"].proposed_model, "claude-sonnet-5")

        # review role -> the judgment/controller-tier model, same as controller.
        self.assertEqual(by_path[".claude/agents/agentflow-reviewer.md"].proposed_model, "claude-opus-4-8")

        # pr-gatekeeper is judgment, not exploration: it migrates to the
        # judgment/controller-tier model (Sol / Opus), never the coding tier.
        self.assertEqual(by_path[".codex/agents/agentflow-pr-gatekeeper.toml"].proposed_model, "gpt-5.6-sol")
        self.assertEqual(by_path[".claude/agents/agentflow-pr-gatekeeper.md"].proposed_model, "claude-opus-4-8")
        self.assertEqual(
            by_path[".github/agents/agentflow-pr-gatekeeper.agent.md"].proposed_model, "claude-opus-4.8"
        )

        for action in actions:
            self.assertIn(action.action, ("propose_update", "skip_compliant"))
            self.assertNotEqual(action.action, "skip_unmanaged")

    def test_pr_gatekeeper_role_is_judgment_not_exploration(self) -> None:
        self.assertEqual(mp.PROFILE_ROLES["agentflow-pr-gatekeeper"], "judgment")
        self.assertNotEqual(mp.PROFILE_ROLES["agentflow-pr-gatekeeper"], "exploration")

    def test_pr_gatekeeper_migrates_to_judgment_tier_for_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex" / "agents").mkdir(parents=True)
            path = root / ".codex" / "agents" / "agentflow-pr-gatekeeper.toml"
            path.write_text('name = "agentflow-pr-gatekeeper"\nmodel = "gpt-5.6-terra"\n', encoding="utf-8")
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "propose_update")
            self.assertEqual(actions[0].proposed_model, "gpt-5.6-sol")

    def test_pr_gatekeeper_migrates_to_judgment_tier_for_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude" / "agents").mkdir(parents=True)
            path = root / ".claude" / "agents" / "agentflow-pr-gatekeeper.md"
            path.write_text("---\nname: agentflow-pr-gatekeeper\nmodel: sonnet\n---\nbody\n", encoding="utf-8")
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "propose_update")
            self.assertEqual(actions[0].proposed_model, "claude-opus-4-8")

    def test_existing_claude_opus_4_8_profile_remains_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude" / "agents").mkdir(parents=True)
            path = root / ".claude" / "agents" / "agentflow-reviewer.md"
            path.write_text(
                "---\nname: agentflow-reviewer\nmodel: claude-opus-4-8\n---\nbody\n",
                encoding="utf-8",
            )
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(actions[0].action, "skip_compliant")
            self.assertEqual(actions[0].current_model, "claude-opus-4-8")
            self.assertIn("model: claude-opus-4-8", path.read_text(encoding="utf-8"))

    def test_pr_gatekeeper_migrates_to_judgment_tier_for_copilot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "agents").mkdir(parents=True)
            path = root / ".github" / "agents" / "agentflow-pr-gatekeeper.agent.md"
            path.write_text("---\nname: agentflow-pr-gatekeeper\n---\nbody\n", encoding="utf-8")
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "propose_update")
            self.assertEqual(actions[0].proposed_model, "claude-opus-4.8")

    def test_existing_copilot_opus_4_8_profile_remains_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "agents").mkdir(parents=True)
            path = root / ".github" / "agents" / "agentflow-reviewer.agent.md"
            path.write_text(
                "---\nname: agentflow-reviewer\nmodel: claude-opus-4.8\n---\nbody\n",
                encoding="utf-8",
            )
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(actions[0].action, "skip_compliant")
            self.assertEqual(actions[0].current_model, "claude-opus-4.8")
            self.assertIn("model: claude-opus-4.8", path.read_text(encoding="utf-8"))

    def test_never_proposes_update_for_unmanaged_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude" / "agents").mkdir(parents=True)
            # A user's own custom subagent: same directory glob, not a name
            # agentflow ships, and using a model this policy would reject.
            custom = root / ".claude" / "agents" / "my-custom-agent.md"
            custom.write_text("---\nname: my-custom-agent\nmodel: gpt-4\n---\nbody\n", encoding="utf-8")
            before = self._snapshot(root)
            actions = mp.plan_migration(root, self.policy, extra_paths=[custom])
            after = self._snapshot(root)
            self.assertEqual(before, after)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "skip_unmanaged")
            self.assertIsNone(actions[0].proposed_model)

    def test_unmanaged_file_not_silently_discovered_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude" / "agents").mkdir(parents=True)
            (root / ".claude" / "agents" / "my-custom-agent.md").write_text(
                "---\nname: my-custom-agent\nmodel: gpt-4\n---\nbody\n", encoding="utf-8"
            )
            # Without being named explicitly, an unmanaged file never shows up
            # in the plan at all -- the strongest form of "never touched".
            actions = mp.plan_migration(root, self.policy)
            self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
