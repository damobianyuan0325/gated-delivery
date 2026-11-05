#!/usr/bin/env python3
"""Read-only structural and publication checks; no providers or skill execution."""
from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import subprocess
from urllib.parse import unquote, urlsplit

SKILLS = ("gated-delivery",)
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
PRIVATE_DIRS = {"backups", "evidence", "local-evidence", "installed-before", "runtime",
                "private", "source-materials", "private-receipts"}
ARTIFACT_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff",
    ".pdf", ".ppt", ".pptx", ".sqlite", ".sqlite3", ".db", ".log", ".jsonl",
    ".gz", ".zip", ".tar", ".7z", ".dump", ".bak", ".backup", ".pem", ".key", ".p12",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
TOKEN_RE = re.compile(
    r"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|\d{6,12}:[A-Za-z0-9_-]{30,})(?![\w-])"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
PERSONAL_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|(?=$|[\s\"'`]))")
CREDENTIAL_NAME_RE = re.compile(
    r"(?i)(?:password|passwd|api_?key|access_token|auth_token|client_secret|private_key|bot_token)"
)
PLACEHOLDER_RE = re.compile(
    r"(?ix)^(?:<[^>]+>|\$\{[A-Z_][A-Z0-9_]*\}|\$[A-Z_][A-Z0-9_]*"
    r"|(?:sk-|gh[pousr]_|github_pat_)?(?:YOUR|EXAMPLE|PLACEHOLDER|REPLACE)[_A-Z0-9-]*)$"
)
CONTACT_EMAIL_RE = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?\.)+[A-Z]{2,63}", re.IGNORECASE
)
ENDPOINT_URL_RE = re.compile(r"\b(?:https?|wss?|rpc|grpcs?)://[^\s<>\"'`]+", re.IGNORECASE)
MATERIAL_LOCATOR_RE = re.compile(
    r"(?<![A-Z0-9_])photo_\d+_\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2}-\d{2})?(?!\d)",
    re.IGNORECASE,
)
RFC1918_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def issue(path: str, line: int, category: str) -> dict:
    # Never include offending content or exception messages in output.
    return {"path": path, "line": line, "category": category}


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def collect(root: Path, tracked: bool) -> tuple[dict[str, bytes], list[dict], set[str]]:
    files, errors, symlinks = {}, [], set()
    if tracked:
        # Read the actual index blobs, including force-added ignored files. A clean
        # working copy cannot conceal a different secret already in the index.
        for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            header, raw_name = record.split(b"\t", 1)
            mode, oid, stage = header.decode("ascii").split()
            name = raw_name.decode("utf-8")
            if stage != "0":
                errors.append(issue(name, 0, "unmerged_index_entry"))
            elif mode == "120000":
                symlinks.add(name)
                errors.append(issue(name, 0, "symlink_forbidden"))
            elif mode not in {"100644", "100755"}:
                errors.append(issue(name, 0, "unsupported_index_mode"))
            elif not re.fullmatch(r"[0-9a-f]{40,64}", oid):
                errors.append(issue(name, 0, "invalid_index_object"))
            elif int(_git(root, "cat-file", "-s", oid)) > MAX_FILE_BYTES:
                errors.append(issue(name, 0, "file_too_large"))
            else:
                files[name] = _git(root, "cat-file", "blob", oid)
        return files, errors, symlinks
    for directory, dirs, names in os.walk(root, followlinks=False):
        here = Path(directory)
        for name in list(dirs):
            p = here / name
            if p.is_symlink():
                rel = p.relative_to(root).as_posix()
                symlinks.add(rel)
                errors.append(issue(rel, 0, "symlink_forbidden"))
                dirs.remove(name)
            elif name in SKIP_DIRS:
                dirs.remove(name)
        for name in names:
            p = here / name
            rel = p.relative_to(root).as_posix()
            mode = p.lstat().st_mode
            if stat.S_ISLNK(mode):
                symlinks.add(rel)
                errors.append(issue(rel, 0, "symlink_forbidden"))
            elif not stat.S_ISREG(mode):
                errors.append(issue(rel, 0, "nonregular_file"))
            elif p.stat().st_size > MAX_FILE_BYTES:
                errors.append(issue(rel, 0, "file_too_large"))
            else:
                files[rel] = p.read_bytes()
    return files, errors, symlinks


def _placeholder(value: str) -> bool:
    return not value.strip() or bool(PLACEHOLDER_RE.fullmatch(value.strip()))


def content_checks(path: str, text: str) -> list[dict]:
    errors = []
    for number, line in enumerate(text.splitlines(), 1):
        if PRIVATE_KEY_RE.search(line):
            errors.append(issue(path, number, "private_key_material"))
        if any(not _placeholder(m.group()) for m in TOKEN_RE.finditer(line)):
            errors.append(issue(path, number, "secret_token_shape"))
        if PERSONAL_PATH_RE.search(line):
            errors.append(issue(path, number, "personal_absolute_path"))
        if re.search(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", line):
            errors.append(issue(path, number, "credentialed_url"))
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return errors + [issue(path, 0, "python_syntax_invalid")]
        for node in ast.walk(tree):
            pairs = []
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                pairs = [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.Dict):
                pairs = [(k.value, v) for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            for key, value in pairs:
                if (CREDENTIAL_NAME_RE.fullmatch(key) and isinstance(value, ast.Constant)
                        and isinstance(value.value, str) and not _placeholder(value.value)):
                    errors.append(issue(path, value.lineno, "hardcoded_credential_literal"))
    elif path.endswith(".json"):
        try:
            document = json.loads(text)
        except ValueError:
            return errors + [issue(path, 0, "json_syntax_invalid")]

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if (CREDENTIAL_NAME_RE.fullmatch(key) and isinstance(item, str)
                            and not _placeholder(item)):
                        match = re.search(re.escape(json.dumps(key)) + r"\s*:", text)
                        line = text[:match.start()].count("\n") + 1 if match else 0
                        errors.append(issue(path, line, "hardcoded_credential_literal"))
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(document)
    return errors


def public_content_checks(path: str, text: str) -> list[dict]:
    """Bounded publication hints, not a history, rights or exhaustive secret audit."""
    errors = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in CONTACT_EMAIL_RE.finditer(line):
            domain = match.group().rsplit("@", 1)[1].lower()
            documented = (domain.endswith((".example", ".invalid")) or any(
                domain == name or domain.endswith("." + name)
                for name in ("example.com", "example.org", "example.net")))
            if not documented:
                errors.append(issue(path, number, "public_contact_email"))
        for match in ENDPOINT_URL_RE.finditer(line):
            endpoint = match.group().rstrip(".,;!?)")
            if "[" not in endpoint:
                endpoint = endpoint.rstrip("]}")
            try:
                address = ipaddress.ip_address(urlsplit(endpoint).hostname or "")
            except ValueError:
                continue
            if isinstance(address, ipaddress.IPv6Address):
                address = address.ipv4_mapped
            if (address is not None and not address.is_loopback
                    and any(address in network for network in RFC1918_NETWORKS)):
                errors.append(issue(path, number, "public_private_endpoint"))
        if MATERIAL_LOCATOR_RE.search(line):
            errors.append(issue(path, number, "public_source_material_locator"))
    return errors


def frontmatter(path: str, text: str, expected_name: str) -> list[dict]:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return [issue(path, 1, "skill_frontmatter_missing")]
    end = lines.index("---", 1)
    values, metadata, errors = {}, False, []
    for n, line in enumerate(lines[1:end], 2):
        match = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if match:
            key, value = match.groups()
            metadata = key == "metadata" and not value.strip()
            if key in {"name", "description", "version"}:
                if key in values:
                    errors.append(issue(path, n, "skill_frontmatter_duplicate"))
                values[key] = (value.strip().strip("\"'"), n)
        elif metadata:
            match = re.match(r"^\s{2}version:\s*(.*?)\s*$", line)
            if match:
                if "version" in values:
                    errors.append(issue(path, n, "skill_frontmatter_duplicate"))
                values["version"] = (match.group(1).strip("\"'"), n)
    if values.get("name", (None, 1))[0] != expected_name:
        errors.append(issue(path, values.get("name", (None, 1))[1], "skill_name_invalid"))
    description, line = values.get("description", ("", 1))
    if not description or (description in {"|", ">", "|-", ">-"}
                           and not any(x.startswith("  ") and x.strip() for x in lines[line:end])):
        errors.append(issue(path, line, "skill_description_missing"))
    version, line = values.get("version", ("", 1))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        errors.append(issue(path, line, "skill_version_invalid"))
    return errors


def markdown_destinations(text: str):
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        line = re.sub(r"(`+).*?\1", "", line)
        definition = re.match(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)", line)
        if definition:
            yield number, definition.group(1).strip("<>")
            continue
        for match in re.finditer(r"\[[^\]\n]*\]\(\s*", line):
            tail = line[match.end():]
            if tail.startswith("<"):
                if ">" in tail:
                    yield number, tail[1:tail.index(">")]
                continue
            chars, depth, escaped = [], 0, False
            for char in tail:
                if escaped:
                    chars.append(char); escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "(" :
                    depth += 1; chars.append(char)
                elif char == ")" and depth:
                    depth -= 1; chars.append(char)
                elif char == ")" or (char.isspace() and not depth):
                    break
                else:
                    chars.append(char)
            yield number, "".join(chars)


def check(root: Path, tracked: bool = False, public: bool = False) -> dict:
    files, errors, symlinks = collect(root, tracked)
    texts = {}
    for path, raw in files.items():
        parts = path.split("/")
        name = parts[-1]
        if parts[0] == "skills" and (len(parts) < 3 or parts[1] not in SKILLS):
            errors.append(issue(path, 0, "skill_outside_repository_scope"))
        if (Path(path).suffix.lower() in ARTIFACT_SUFFIXES or PRIVATE_DIRS.intersection(parts[:-1])
                or (name.startswith(".env") and name != ".env.example")
                or name in {"credentials.json", "auth.json", "id_rsa", "id_ed25519"}):
            errors.append(issue(path, 0, "private_or_generated_artifact"))
        try:
            text = raw.decode("utf-8")
            if "\0" in text:
                raise UnicodeError
        except UnicodeError:
            errors.append(issue(path, 0, "nontext_artifact"))
            continue
        texts[path] = text
        errors.extend(content_checks(path, text))
        if public:
            errors.extend(public_content_checks(path, text))
    for skill in SKILLS:
        path = f"skills/{skill}/SKILL.md"
        if path not in texts:
            errors.append(issue(path, 0, "skill_entrypoint_missing"))
        else:
            errors.extend(frontmatter(path, texts[path], skill))
    for path, text in texts.items():
        if not path.lower().endswith(".md"):
            continue
        for number, destination in markdown_destinations(text):
            if not destination or destination.startswith("#"):
                continue
            try:
                parsed = urlsplit(destination)
            except ValueError:
                errors.append(issue(path, number, "invalid_link_destination"))
                continue
            if parsed.scheme or parsed.netloc:
                if parsed.scheme not in {"http", "https", "mailto", "tel"}:
                    errors.append(issue(path, number, "unsafe_link_scheme"))
                continue
            target = unquote(parsed.path)
            normal = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if target.startswith(("/", "\\")) or normal == ".." or normal.startswith("../"):
                errors.append(issue(path, number, "local_link_outside_repo"))
            elif any(normal == x or normal.startswith(x + "/") for x in symlinks):
                errors.append(issue(path, number, "local_link_symlink"))
            elif normal not in files:
                errors.append(issue(path, number, "local_link_missing"))
    unique = {tuple(x.values()): x for x in errors}
    errors = sorted(unique.values(), key=lambda x: (x["path"], x["line"], x["category"]))
    return {"ok": not errors, "mode": "index_blobs" if tracked else "working_tree",
            "files_checked": len(files), "issues": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true", help="Check actual Git index blobs, read-only")
    parser.add_argument("--public", action="store_true", help="Also flag contact emails, private endpoint URLs and source-material locators; no history or rights audit")
    args = parser.parse_args()
    try:
        result = check(args.root.resolve(strict=True), args.tracked, args.public)
    except (OSError, ValueError, subprocess.SubprocessError):
        result = {"ok": False, "issues": [issue(".", 0, "repository_read_failed")]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
