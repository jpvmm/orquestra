#!/usr/bin/env python3
"""Policy loader, RESERVED bookkeeping paths, per-project quota and owner/ignore_dirs tests. No model, no network;
temporary roots only. Run: python3 tests/test_retro_policy.py (or pytest)."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.realpath(__file__))
FLEET = os.path.join(os.path.dirname(HERE), "fleet")
BIN = os.path.join(FLEET, "bin")
sys.path.insert(0, BIN)
import fleet_skill_lib as L  # noqa: E402


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


ENV_KEYS = (
    "FLEET_ROOT",
    "ORQ_ROOT",
    "FLEET_RETRO_POLICY",
    "FLEET_LESSONS_NAME",
    "FLEET_LESSONS",
    "ORQUESTRA_HOME",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = tempfile.mkdtemp(prefix="fleet-policy-test-")
        self.root = os.path.join(self.tmp, "fleet")
        write(
            os.path.join(self.root, "skill", "fleet-sdd", "SKILL.md"),
            "# fleet-sdd\n\n- Existing rule that is fairly long.\n",
        )
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["FLEET_ROOT"], os.environ["ORQ_ROOT"] = self.root, self.tmp
        self.retro = load(os.path.join(BIN, "fleet-retro"), "fleet_retro_policy_t")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def policy_file(self, data, name="p.json"):
        p = os.path.join(self.tmp, name)
        write(p, data if isinstance(data, str) else json.dumps(data))
        return p


class TestPolicy(Base):
    def test_defaults_without_file(self):
        pol = L.load_policy(self.root)
        self.assertEqual(pol["_warnings"], [])
        self.assertEqual({k: v for k, v in pol.items() if k != "_warnings"}, L.DEFAULTS)
        self.assertEqual(pol["quota"]["project_additions_per_project"], 3)
        self.assertEqual(pol["owner_name"], "the owner")
        pol["quota"]["generic_additions"] = 99  # a fresh copy each call
        self.assertEqual(L.load_policy(self.root)["quota"]["generic_additions"], 3)

    def test_shipped_file_equals_defaults(self):
        with open(os.path.join(FLEET, "skill", "retro-policy.json")) as f:
            self.assertEqual(json.load(f), L.DEFAULTS)

    def test_file_and_env_override_merge(self):
        write(
            os.path.join(self.root, "skill", "retro-policy.json"),
            json.dumps(
                {
                    "owner_name": "Ana",
                    "quota": {"generic_additions": 2},
                    "custom": {"x": 1},
                }
            ),
        )
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"quota": {"generic_additions": 5}, "cycle": {"due_hours": 12}}
        )
        pol = L.load_policy(self.root)
        self.assertEqual(pol["owner_name"], "Ana")
        self.assertEqual(pol["quota"]["generic_additions"], 5)  # env wins over the file
        self.assertEqual(
            pol["quota"]["project_additions_per_project"], 3
        )  # untouched sibling kept
        self.assertEqual(pol["cycle"]["due_hours"], 12)
        self.assertEqual(pol["cycle"]["late_hours"], 72)
        self.assertIs(pol["cycle"]["late_notice"], True)
        self.assertEqual(pol["custom"], {"x": 1})  # unknown keys kept
        self.assertEqual(pol["_warnings"], [])

    def test_unknown_objects_deep_merge_across_layers(self):
        write(
            os.path.join(self.root, "skill", "retro-policy.json"),
            json.dumps(
                {
                    "custom": {"x": 1, "deep": {"a": 1}},
                    "quota": {"extra": {"p": 1}},
                }
            ),
        )
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {
                "custom": {"y": 2, "deep": {"b": 2}},
                "quota": {"extra": {"q": 2}},
            }
        )
        pol = L.load_policy(self.root)
        self.assertEqual(pol["custom"], {"x": 1, "y": 2, "deep": {"a": 1, "b": 2}})
        self.assertEqual(pol["quota"]["extra"], {"p": 1, "q": 2})
        self.assertEqual(pol["_warnings"], [])

    def test_bad_types_fall_back_with_warning(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {
                "owner_name": 7,
                "quota": {"generic_additions": "many", "shrink_ops_per_scope": True},
                "ignore_dirs": "x",
                "cycle": [1],
                "judge_memory": {"near_suppress": 1, "near_jaccard": 0.7},
            }
        )
        pol = L.load_policy(self.root)
        self.assertEqual(pol["owner_name"], "the owner")
        self.assertEqual(pol["quota"]["generic_additions"], 3)
        self.assertEqual(pol["quota"]["shrink_ops_per_scope"], 3)
        self.assertEqual(pol["ignore_dirs"], L.DEFAULTS["ignore_dirs"])
        self.assertEqual(pol["cycle"], L.DEFAULTS["cycle"])
        self.assertIs(pol["judge_memory"]["near_suppress"], False)
        self.assertEqual(pol["judge_memory"]["near_jaccard"], 0.7)
        self.assertEqual(len(pol["_warnings"]), 6)

    def test_broken_file_is_ignored_with_warning(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file("{not json")
        pol = L.load_policy(self.root)
        self.assertEqual(pol["quota"], L.DEFAULTS["quota"])
        self.assertTrue(pol["_warnings"])

    def test_policy_version_changes_with_value(self):
        a = L.load_policy(self.root)
        v = L.policy_version(a)
        self.assertEqual(v, L.policy_version(L.load_policy(self.root)))
        self.assertRegex(v, r"^[0-9a-f]{8,}$")
        a["quota"]["generic_additions"] = 4
        self.assertNotEqual(v, L.policy_version(a))
        b = L.load_policy(self.root)
        b["_warnings"].append("ignored by the version")
        self.assertEqual(v, L.policy_version(b))

    def test_reserved_blocks_bookkeeping_paths(self):
        for p in (
            "skill/rules-index.json",
            "skill/judge-log.jsonl",
            "skill/evals/x.md",
            "skill/retro-policy.json",
            "skill/usage",
            "skill/usage.json",
            "skill/ledger.jsonl",
        ):
            self.assertIsNone(L.safe_rel(p), p)
        self.assertEqual(
            L.safe_rel("skill/fleet-sdd/SKILL.md"), "skill/fleet-sdd/SKILL.md"
        )

    def test_float_for_int_default_is_rejected(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {
                "cycle": {"max_batch_dirs": 2.5, "due_hours": True},
                "merge_guard": {"min_ratio": 1},
            }
        )
        pol = L.load_policy(self.root)
        self.assertEqual(pol["cycle"]["max_batch_dirs"], 10)
        self.assertEqual(pol["cycle"]["due_hours"], 48)
        self.assertEqual(
            pol["merge_guard"]["min_ratio"], 1
        )  # int ok for a float default
        self.assertEqual(len(pol["_warnings"]), 2)

    def test_invalid_override_restores_code_default_not_previous_layer(self):
        write(
            os.path.join(self.root, "skill", "retro-policy.json"),
            json.dumps(
                {"quota": {"generic_additions": 7}, "cycle": {"max_batch_dirs": 4}}
            ),
        )
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"quota": {"generic_additions": "x"}, "cycle": "bad"}
        )
        pol = L.load_policy(self.root)
        self.assertEqual(pol["quota"]["generic_additions"], 3)
        self.assertEqual(pol["cycle"], L.DEFAULTS["cycle"])
        self.assertEqual(len(pol["_warnings"]), 2)

    def test_lessons_file_must_be_plain_md_name(self):
        for bad in ("../x.md", "a/b.md", "notes.txt", ".md"[:0] + "x"):
            os.environ["FLEET_RETRO_POLICY"] = self.policy_file({"lessons_file": bad})
            pol = L.load_policy(self.root)
            self.assertEqual(pol["lessons_file"], "learnings.md", bad)
            self.assertTrue(pol["_warnings"], bad)
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"lessons_file": "notes.md"}
        )
        self.assertEqual(L.load_policy(self.root)["lessons_file"], "notes.md")

    def test_hard_ignore_floor(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file({"ignore_dirs": []})
        for n in ("fleet", "vigia", "skill"):
            self.assertFalse(L.valid_project(n), n)
        self.assertTrue(L.valid_project("tmp"))

    def test_ignore_dirs_from_policy_changes_project_detection(self):
        self.assertTrue(L.valid_project("acme"))
        self.assertFalse(L.valid_project("tmp"))
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"ignore_dirs": ["acme", "bin"]}
        )
        self.assertFalse(L.valid_project("acme"))
        self.assertTrue(L.valid_project("tmp"))
        os.makedirs(os.path.join(self.tmp, "tmp", "t1"))
        self.assertEqual(L.project_of(os.path.join(self.tmp, "tmp", "t1")), "tmp")

    def test_lessons_file_from_policy_and_env_override(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"lessons_file": "notes.md"}
        )
        self.assertEqual(L.lessons_name(), "notes.md")
        self.assertEqual(L.lessons_default(), os.path.join(self.tmp, "notes.md"))
        os.environ["FLEET_LESSONS_NAME"] = "other.md"
        self.assertEqual(L.lessons_name(), "other.md")

    def test_fleet_run_ignore_dirs_with_and_without_policy(self):
        run = load(os.path.join(BIN, "fleet-run"), "fleet_run_policy_t")
        self.assertIn("tmp", run.ignore_dirs())
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file({"ignore_dirs": ["acme"]})
        # fleet-run reads the policy of its own fleet root; the env file is also merged by the lib
        self.assertEqual(
            run.ignore_dirs(), {"acme", "fleet", "vigia", "skill"}
        )  # hard floor added


def prop(n, project="acme", scope="project", **kw):
    p = {
        "scope": scope,
        "rule": f"rule {n}",
        "why": "w",
        "skill_step": "s",
        "target": "LESSONS.md"
        if scope == "project"
        else "skill/fleet-sdd/references/topic.md",
        "edit": {
            "old": "",
            "new": f"- A perfectly valid rule number {n} that is long enough.",
        },
    }
    if scope == "project":
        p["project"] = project
    p.update(kw)
    return p


class TestQuota(Base):
    def validate(self, props, projects=("a", "b")):
        for pr in projects:
            os.makedirs(os.path.join(self.tmp, pr), exist_ok=True)
        return self.retro.validate(props, self.root, projects)

    def test_ct11_three_per_project_each_fourth_rejected(self):
        props = [prop(f"a{i}", "a") for i in range(3)] + [
            prop(f"b{i}", "b") for i in range(3)
        ]
        acc, rej, _ = self.validate(props)
        self.assertEqual((len(acc), len(rej)), (6, 0))
        acc, rej, _ = self.validate(props + [prop("a4", "a")])
        self.assertEqual(len(acc), 6)
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0][0]["rule"], "rule a4")
        self.assertIn("over the limit of 3", rej[0][1])

    def test_ct11_rejected_proposals_do_not_consume_quota(self):
        bad = [
            prop(
                f"bad{i}",
                "a",
                edit={
                    "old": "",
                    "new": f"- dated 2026-01-0{i + 1} rule that is long enough.",
                },
            )
            for i in range(5)
        ]
        good = [prop(f"ok{i}", "a") for i in range(3)]
        acc, rej, _ = self.validate(bad[:2] + good[:1] + bad[2:4] + good[1:])
        self.assertEqual(
            [p["rule"] for p, _ in acc], ["rule ok0", "rule ok1", "rule ok2"]
        )
        self.assertEqual(len(rej), 4)
        self.assertTrue(all("date" in r for _, r in rej))

    def test_generic_has_its_own_quota(self):
        props = [prop(f"a{i}", "a") for i in range(3)] + [
            prop(f"g{i}", scope="generic") for i in range(4)
        ]
        acc, rej, _ = self.validate(props)
        self.assertEqual(sum(1 for _, t in acc if t["kind"] == "generic"), 3)
        self.assertEqual(sum(1 for _, t in acc if t["kind"] == "project"), 3)
        self.assertEqual([p["rule"] for p, _ in rej], ["rule g3"])

    def test_shrink_ops_do_not_consume_addition_quota(self):
        lines = "".join(
            f"- Existing long lesson number {i} with some words.\n" for i in range(8)
        )
        write(os.path.join(self.tmp, "a", "LESSONS.md"), "# a\n" + lines)
        shr = [
            prop(
                f"s{i}",
                "a",
                edit={
                    "old": f"- Existing long lesson number {i} with some words.",
                    # a real shrink that keeps >= 0.8x of its block (op-less edits are merge-guarded)
                    "new": f"- Shorter phrasing of item {i}, kept compact.",
                },
            )
            for i in range(5)
        ]
        self.assertTrue(all(self.retro.is_shrink(p) for p in shr))
        self.assertTrue(self.retro.is_shrink(prop("m", "a", op="merge")))
        self.assertFalse(self.retro.is_shrink(prop("n", "a")))
        adds = [prop(f"n{i}", "a") for i in range(3)]
        acc, rej, _ = self.validate(shr + adds, projects=("a",))
        self.assertEqual(len(acc), 6)  # 3 shrinks (cap) + 3 additions
        self.assertEqual([p["rule"] for p, _ in rej], ["rule s3", "rule s4"])
        self.assertTrue(all("shrink ops" in r for _, r in rej))

    def test_op_merge_without_real_shrink_is_an_addition(self):
        write(
            os.path.join(self.tmp, "a", "LESSONS.md"),
            "# a\n\n- Existing long lesson with some words.\n",
        )
        ps = [prop(f"m{i}", "a", op="merge") for i in range(4)]
        acc, rej, _ = self.validate(ps, projects=("a",))
        self.assertEqual(len(acc), 3)  # addition quota applies despite op=merge
        self.assertEqual(len(rej), 1)
        self.assertIn("proposals", rej[0][1])

    def test_read_cap_reason_is_redacted_in_retro_md(self):
        write(os.path.join(self.tmp, "a", "LESSONS.md"), "# a\n")
        sec = "sk-" + "A1b2C3d4" * 4
        ps = [prop(f"c{i}", sec) for i in range(12)]
        T = os.path.join(self.tmp, "a", "t1")
        os.makedirs(T)
        ans = os.path.join(self.tmp, "ans.json")
        write(
            ans,
            "```json\n"
            + json.dumps({"verdict": "proposals", "proposals": ps})
            + "\n```",
        )
        self.retro.collect(T, "run-1", open(ans).read(), None, self.root, ["a"])
        self.assertNotIn(sec, open(os.path.join(T, "retro.md")).read())

    def test_read_cap(self):
        props = [
            prop(
                f"a{i}",
                "a",
                edit={"old": "nope-missing", "new": f"- never matches {i} anywhere."},
            )
            for i in range(12)
        ]
        acc, rej, _ = self.validate(props, projects=("a",))
        self.assertEqual(acc, [])
        self.assertEqual(
            sum("over read cap" in r for _, r in rej), 3
        )  # 12 - 3*3 looked at
        self.assertEqual(len(rej), 12)
        # a valid proposal beyond the cap is not even considered
        acc, rej, _ = self.validate(props[:9] + [prop("late", "a")], projects=("a",))
        self.assertEqual(acc, [])
        self.assertIn("over read cap", rej[-1][1])

    def test_quota_comes_from_policy(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"quota": {"project_additions_per_project": 1}}
        )
        acc, rej, _ = self.validate([prop("a1", "a"), prop("a2", "a"), prop("b1", "b")])
        self.assertEqual(len(acc), 2)
        self.assertEqual(len(rej), 1)


class TestOwnerPrompt(Base):
    def test_owner_name_and_quota_in_prompt_no_personal_name(self):
        os.environ["FLEET_RETRO_POLICY"] = self.policy_file(
            {"owner_name": "Captain Zed", "quota": {"generic_additions": 2}}
        )
        T = os.path.join(self.tmp, "acme", "t1")
        write(os.path.join(T, "progress.md"), "# p\n")
        pol = L.load_policy(self.root)
        prompt = self.retro.build_prompt(T, "run-1", self.root, "acme")
        self.assertIn("Captain Zed", prompt)
        self.assertIn("at most 3 per project and 2 generic", prompt)
        self.assertIn("At most 3 proposals PER PROJECT and at most 2 generic", prompt)
        self.assertNotIn("PER LOOP", prompt)
        self.assertNotIn("@OWNER@", prompt)
        self.assertEqual(pol["owner_name"], "Captain Zed")
        os.environ.pop("FLEET_RETRO_POLICY")
        self.assertIn(
            "the owner", self.retro.build_prompt(T, "run-1", self.root, "acme")
        )

    def test_no_literal_personal_name_in_shipped_files(self):
        banned = "".join(chr(c) for c in (82, 97, 102, 97, 101, 108)).lower()
        for p in (
            os.path.join(BIN, "fleet-retro"),
            os.path.join(BIN, "fleet-run"),
            os.path.join(BIN, "fleet_skill_lib.py"),
            os.path.join(FLEET, "skill", "retro-policy.json"),
        ):
            with open(p, encoding="utf-8") as f:
                self.assertNotIn(banned, f.read().lower(), p)


if __name__ == "__main__":
    unittest.main()
