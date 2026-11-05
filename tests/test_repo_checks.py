"""Isolated fixtures: never run collectors, migrations, providers or a real DB."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "repo_guard", Path(__file__).resolve().parents[1] / "scripts/check_repo.py"
)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class RepoChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in GUARD.SKILLS:
            self.write(f"skills/{name}/SKILL.md", self.skill(name))

    @staticmethod
    def skill(name):
        return f'---\nname: {name}\ndescription: A useful bounded skill.\nmetadata:\n  version: "1.2.3"\n---\n'

    def write(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value if isinstance(value, bytes) else value.encode())
        return p

    def categories(self, result=None):
        return {x["category"] for x in (result or GUARD.check(self.root))["issues"]}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    @staticmethod
    def publication_samples():
        # Synthetic values are composed at runtime, never private contact data.
        contact = "fixture-contact" + "@" + "publication-fixture" + ".test"
        endpoint = "http" + "://" + ".".join(map(str, (192, 168, 14, 9))) + ":8545/rpc"
        locator = "photo_" + "17_" + "2026-" + "10-" + "29_" + "12-34-56.jpg"
        return contact, endpoint, locator

    def test_valid_env_placeholders_and_real_links(self):
        self.write("README.md", '[Guide](docs/guide%20one.md#topic)\n[web](https://example.com)\n'
                   '[same](#topic)\n```md\n[demonstration](not-a-real-file.md)\n```\n'
                   '`[inline example](also-not-real.md)`\n[ref]: docs/guide%20one.md\n')
        self.write("docs/guide one.md", "# Topic\n")
        self.write("config.py", 'import os\napi_key = os.environ.get("API_KEY")\n'
                   'password = "${APP_PASSWORD}"\nbot_token = "<BOT_TOKEN>"\n')
        self.assertTrue(GUARD.check(self.root)["ok"])

    def test_name_description_and_version_are_required(self):
        path = "skills/gated-delivery/SKILL.md"
        self.write(path, '---\nname: wrong\ndescription: \nmetadata:\n  version: yesterday\n---\n')
        self.assertEqual(self.categories(), {
            "skill_name_invalid", "skill_description_missing", "skill_version_invalid"
        })
        self.write(path, "# No frontmatter\n")
        self.assertIn("skill_frontmatter_missing", self.categories())

    def test_missing_and_escaping_links_are_rejected(self):
        self.write("README.md", '[missing](missing.md)\n[escape](../outside.md)\n'
                   '[encoded escape](%2e%2e/outside.md)\n[unsafe](file:///tmp/example)\n')
        cats = self.categories()
        self.assertTrue({"local_link_missing", "local_link_outside_repo", "unsafe_link_scheme"} <= cats)

    def test_symlink_targets_are_not_followed(self):
        outside = Path(self.temp.name).parent / (self.root.name + "-outside")
        outside.write_text("not candidate content")
        self.addCleanup(outside.unlink)
        (self.root / "borrowed.md").symlink_to(outside)
        self.write("README.md", "[borrowed](borrowed.md)\n")
        cats = self.categories()
        self.assertTrue({"symlink_forbidden", "local_link_symlink"} <= cats)

    def test_actual_token_shape_and_personal_path_are_redacted(self):
        token = "sk-" + "A" * 40
        personal = "/Users/" + "fixture-private-person/work/data"
        self.write("notes.md", token + "\n" + personal + "\n")
        result = GUARD.check(self.root)
        self.assertTrue({"secret_token_shape", "personal_absolute_path"} <= self.categories(result))
        self.assertNotIn(token, str(result))
        self.assertNotIn(personal, str(result))
        self.assertTrue(all(set(x) == {"path", "line", "category"} for x in result["issues"]))

    def test_placeholder_token_and_regex_source_are_not_secrets(self):
        self.write("examples.py", 'api_key = "EXAMPLE_API_KEY"\n'
                   'password = "<PASSWORD>"\npattern = r"sk-[A-Za-z0-9_-]{20,}"\n')
        self.write("README.md", "Set API_KEY, never commit its value.\n"
                   + "sk-" + "REPLACE_WITH_YOUR_API_KEY\n")
        self.assertTrue(GUARD.check(self.root)["ok"])

    def test_hardcoded_password_and_key_material_are_rejected(self):
        self.write("bad.py", 'password = "fixture-only-not-a-real-password"\n')
        self.write("notes.txt", "-----BEGIN " + "PRIVATE KEY-----\nfixture\n")
        self.assertTrue({"hardcoded_credential_literal", "private_key_material"} <= self.categories())

    def test_json_credentials_are_checked_as_data_not_env_names(self):
        self.write("example.json", '{"api_key":"${API_KEY}","password":"<PASSWORD>"}')
        self.assertTrue(GUARD.check(self.root)["ok"])
        self.write("example.json", '{"auth":{"password":"fixture-password-not-real"}}')
        self.assertIn("hardcoded_credential_literal", self.categories())

    def test_malformed_link_is_reported_in_its_file(self):
        self.write("README.md", "[invalid](https://[invalid)\n")
        errors = GUARD.check(self.root)["issues"]
        self.assertTrue(any(x["path"] == "README.md" and x["line"] == 1
                            and x["category"] == "invalid_link_destination" for x in errors))

    def test_course_image_or_runtime_file_is_not_publishable(self):
        self.write("course/photo.jpg", b"fixture image bytes")
        self.write("local-evidence/result.json", "{}\n")
        self.write("backup.sql.gz", b"fixture archive")
        self.assertEqual(sum(x["category"] == "private_or_generated_artifact"
                             for x in GUARD.check(self.root)["issues"]), 3)

    def test_tracked_checks_index_content_and_link_membership(self):
        self.git("init", "--quiet")
        self.write("README.md", "[target](target.md)\n")
        self.write("target.md", "target\n")
        self.git("add", ".")
        self.assertTrue(GUARD.check(self.root, tracked=True)["ok"])
        token = "ghp_" + "B" * 40
        self.write("private.txt", token)
        self.git("add", "private.txt")
        self.write("private.txt", "redacted working copy")
        self.assertIn("secret_token_shape", self.categories(GUARD.check(self.root, tracked=True)))
        self.git("rm", "--cached", "target.md")
        self.assertIn("local_link_missing", self.categories(GUARD.check(self.root, tracked=True)))

    def test_force_staged_ignored_image_is_rejected(self):
        self.git("init", "--quiet")
        self.write(".gitignore", "*.jpg\n")
        self.write("course.jpg", b"fixture original")
        self.git("add", ".")
        self.git("add", "--force", "course.jpg")
        self.assertIn("private_or_generated_artifact", self.categories(GUARD.check(self.root, tracked=True)))

    def test_project_domain_skills_cannot_reenter_working_tree_or_index(self):
        self.git("init", "--quiet")
        for name in ("project-analysis", "project-retrospective"):
            with self.subTest(name=name):
                path = f"skills/{name}/SKILL.md"
                file = self.write(path, self.skill(name))
                self.assertIn("skill_outside_repository_scope", self.categories())
                self.git("add", "--", path)
                file.unlink()  # A clean working tree cannot hide an indexed domain payload.
                result = GUARD.check(self.root, tracked=True)
                self.assertTrue(any(x["path"] == path and x["category"] == "skill_outside_repository_scope"
                                    for x in result["issues"]))
        self.assertEqual(GUARD.SKILLS, ("gated-delivery",))

    def test_public_checks_are_opt_in_and_redact_working_tree_values(self):
        samples = self.publication_samples()
        self.write("notes.md", "\n".join(samples) + "\n")
        self.assertTrue(GUARD.check(self.root)["ok"])
        result = GUARD.check(self.root, public=True)
        self.assertEqual(self.categories(result), {
            "public_contact_email", "public_private_endpoint", "public_source_material_locator"
        })
        self.assertEqual({x["line"] for x in result["issues"]}, {1, 2, 3})
        for sample in samples:
            self.assertNotIn(sample, str(result))
        self.assertTrue(all(set(x) == {"path", "line", "category"} for x in result["issues"]))
        cli = subprocess.run([sys.executable, str(Path(GUARD.__file__)), "--root", str(self.root),
                              "--public"], capture_output=True, text=True, check=False)
        self.assertEqual(cli.returncode, 1)
        self.assertEqual(self.categories(json.loads(cli.stdout)), self.categories(result))
        self.assertEqual(cli.stderr, "")

    def test_public_documentation_contacts_and_loopback_are_allowed(self):
        contacts = ["fixture" + "@" + domain for domain in (
            "example.com", "example.org", "example.net", "docs.example.com",
            "project.example", "project.invalid")]
        endpoints = [scheme + "://" + authority for scheme, authority in (
            ("http", "127.0.0.1:8545"), ("http", "localhost:8000"),
            ("http", "[::1]:8000"), ("https", "192.0.2.1/example"))]
        self.write("guide.md", "\n".join(contacts + endpoints) + "\n")
        self.assertTrue(GUARD.check(self.root, public=True)["ok"])
        token = "sk-" + "C" * 40
        self.write("secret.txt", token)
        for public in (False, True):
            result = GUARD.check(self.root, public=public)
            self.assertIn("secret_token_shape", self.categories(result))
            self.assertNotIn(token, str(result))

    def test_public_checks_read_actual_index_after_working_copy_is_redacted(self):
        self.git("init", "--quiet")
        samples = self.publication_samples()
        self.write("notes.md", "\n".join(samples) + "\n")
        self.git("add", ".")
        self.write("notes.md", "redacted working copy\n")
        self.assertTrue(GUARD.check(self.root, public=True)["ok"])
        self.assertTrue(GUARD.check(self.root, tracked=True)["ok"])
        result = GUARD.check(self.root, tracked=True, public=True)
        self.assertEqual(self.categories(result), {
            "public_contact_email", "public_private_endpoint", "public_source_material_locator"
        })
        for sample in samples:
            self.assertNotIn(sample, str(result))

    def test_source_material_and_private_receipt_directories_stay_out(self):
        for directory in ("private", "source-materials", "private-receipts"):
            self.write(directory + "/notes.txt", "local review input\n")
        for public in (False, True):
            result = GUARD.check(self.root, public=public)
            self.assertEqual(sum(x["category"] == "private_or_generated_artifact"
                                 for x in result["issues"]), 3)


if __name__ == "__main__":
    unittest.main()
