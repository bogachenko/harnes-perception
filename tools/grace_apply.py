#!/usr/bin/env python3
"""Append one approved XML fragment and commit it as one GRACE step."""

from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.etree import ElementTree as ET


class GraceApplyError(RuntimeError):
    """Expected validation or Git error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Append one XML fragment to a unique parent element, validate the result, "
            "show the diff, and create one Git commit containing only the target file."
        )
    )
    parser.add_argument("--file", required=True, help="XML file relative to the Git repository root")
    parser.add_argument("--parent", required=True, help="Unique XML parent tag receiving the fragment")
    parser.add_argument("--message", required=True, help="Commit message for this approved GRACE step")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show the diff, then restore the file without committing",
    )
    return parser.parse_args()


def run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
        raise GraceApplyError(detail)
    return result


def repository_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise GraceApplyError("Current directory is not inside a Git repository.")
    return Path(result.stdout.strip()).resolve()


def parse_xml(xml_text: str, label: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise GraceApplyError(f"{label} is not valid XML: {exc}") from exc


def collect_ids(root: ET.Element, label: str) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for element in root.iter():
        value = element.attrib.get("id")
        if not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        joined = ", ".join(sorted(duplicates))
        raise GraceApplyError(f"Duplicate id values in {label}: {joined}")
    return seen


def indent_fragment(fragment: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else "" for line in fragment.splitlines())


def build_updated_xml(original: str, parent_tag: str, fragment: str) -> str:
    document_root = parse_xml(original, "Target document")
    fragment_root = parse_xml(fragment, "Input fragment")

    existing_ids = collect_ids(document_root, "target document")
    fragment_ids = collect_ids(fragment_root, "input fragment")
    collisions = sorted(existing_ids & fragment_ids)
    if collisions:
        raise GraceApplyError(f"The fragment reuses existing id values: {', '.join(collisions)}")

    matching_parents = [element for element in document_root.iter() if element.tag == parent_tag]
    if len(matching_parents) != 1:
        raise GraceApplyError(
            f"Expected exactly one <{parent_tag}> element, found {len(matching_parents)}."
        )

    closing_tag = f"</{parent_tag}>"
    closing_positions: list[int] = []
    start = 0
    while True:
        position = original.find(closing_tag, start)
        if position < 0:
            break
        closing_positions.append(position)
        start = position + len(closing_tag)
    if len(closing_positions) != 1:
        raise GraceApplyError(
            f"Expected exactly one textual closing tag {closing_tag}, found {len(closing_positions)}."
        )

    closing_position = closing_positions[0]
    line_start = original.rfind("\n", 0, closing_position) + 1
    parent_indent = original[line_start:closing_position]
    if parent_indent.strip():
        raise GraceApplyError(f"Closing tag {closing_tag} must start on its own line.")

    child_indent = parent_indent + "    "
    normalized_fragment = textwrap.dedent(fragment).strip()
    inserted_fragment = indent_fragment(normalized_fragment, child_indent)

    before = original[:line_start].rstrip()
    after = original[closing_position + len(closing_tag):].lstrip("\r\n")
    updated = f"{before}\n\n{inserted_fragment}\n{parent_indent}{closing_tag}"
    if after:
        updated += "\n" + after
    if not updated.endswith("\n"):
        updated += "\n"

    updated_root = parse_xml(updated, "Updated document")
    collect_ids(updated_root, "updated document")
    return updated


def ensure_target_is_clean(repo_root: Path, relative_path: str) -> None:
    status = run_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative_path,
    ).stdout.strip()
    if status:
        raise GraceApplyError(
            f"Target file already has uncommitted changes:\n{status}\n"
            "Commit, stash, or revert them before applying another GRACE step."
        )


def verify_commit(repo_root: Path, relative_path: str, expected_message: str) -> str:
    commit_sha = run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
    actual_message = run_git(repo_root, "show", "-s", "--format=%s", "HEAD").stdout.strip()
    if actual_message != expected_message:
        raise GraceApplyError(
            f"Commit verification failed: expected message {expected_message!r}, got {actual_message!r}."
        )

    changed_files = [
        line.strip()
        for line in run_git(
            repo_root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).stdout.splitlines()
        if line.strip()
    ]
    if changed_files != [relative_path]:
        raise GraceApplyError(
            "Commit verification failed: expected exactly one changed file "
            f"({relative_path}), got {changed_files}."
        )
    return commit_sha


def main() -> int:
    args = parse_args()
    original: str | None = None
    target: Path | None = None
    wrote_file = False

    try:
        repo_root = repository_root()
        target = (repo_root / args.file).resolve()
        try:
            relative_path = target.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise GraceApplyError("--file must resolve inside the current Git repository.") from exc

        if not target.is_file():
            raise GraceApplyError(f"Target XML file does not exist: {relative_path}")
        if target.suffix.lower() != ".xml":
            raise GraceApplyError("Target file must have the .xml extension.")

        ensure_target_is_clean(repo_root, relative_path)
        fragment = sys.stdin.read()
        if not fragment.strip():
            raise GraceApplyError("No XML fragment was provided on standard input.")

        original = target.read_text(encoding="utf-8")
        updated = build_updated_xml(original, args.parent, fragment)
        if updated == original:
            raise GraceApplyError("The operation produced no file changes.")

        target.write_text(updated, encoding="utf-8")
        wrote_file = True

        diff_check = run_git(repo_root, "diff", "--check", "--", relative_path, check=False)
        if diff_check.returncode != 0:
            raise GraceApplyError(diff_check.stdout.strip() or diff_check.stderr.strip())

        diff = run_git(repo_root, "diff", "--", relative_path).stdout
        print(diff, end="" if diff.endswith("\n") else "\n")

        if args.dry_run:
            target.write_text(original, encoding="utf-8")
            wrote_file = False
            print("DRY_RUN_PASS")
            print(f"file: {relative_path}")
            return 0

        run_git(repo_root, "commit", "--only", "-m", args.message, "--", relative_path)
        wrote_file = False

        committed_xml = target.read_text(encoding="utf-8")
        committed_root = parse_xml(committed_xml, "Committed document")
        collect_ids(committed_root, "committed document")
        commit_sha = verify_commit(repo_root, relative_path, args.message)

        print("PASS")
        print(f"commit: {commit_sha}")
        print(f"file: {relative_path}")
        print(f"message: {args.message}")
        return 0
    except GraceApplyError as exc:
        if wrote_file and target is not None and original is not None:
            target.write_text(original, encoding="utf-8")
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        if wrote_file and target is not None and original is not None:
            try:
                target.write_text(original, encoding="utf-8")
            except OSError:
                pass
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
