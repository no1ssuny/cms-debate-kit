#!/usr/bin/env python3
"""Validate a Codex skill using only the Python standard library."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
}
NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
FRONTMATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
TOP_LEVEL_FIELD_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
STANDALONE_TODO_PATTERN = re.compile(r"^[ ]{0,3}\[TODO:[^\n]*\][ \t]*$")


class ValidationError(ValueError):
    """Raised when a skill does not satisfy the supported format."""


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(f"Missing required file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ValidationError(f"File is not valid UTF-8: {path}") from exc


def parse_scalar(value: str | None, line_number: int) -> str | None:
    if value is None or not value.strip():
        return None

    value = value.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Invalid double-quoted value on frontmatter line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(parsed, str):
            raise ValidationError(
                f"Frontmatter line {line_number} must contain a string value."
            )
        return parsed

    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValidationError(
                f"Unclosed single-quoted value on frontmatter line {line_number}."
            )
        return value[1:-1].replace("''", "'")

    if value in {"|", ">", "|-", ">-", "|+", ">+"}:
        raise ValidationError(
            "Multiline frontmatter scalars are not supported by this dependency-free validator."
        )
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str | None], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = FRONTMATTER_PATTERN.match(normalized)
    if not match:
        raise ValidationError("SKILL.md must begin with YAML frontmatter delimited by ---. ")

    fields: dict[str, str | None] = {}
    current_key: str | None = None
    for line_number, line in enumerate(match.group(1).splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            if current_key is None:
                raise ValidationError(
                    f"Unexpected indentation on frontmatter line {line_number}."
                )
            continue

        field_match = TOP_LEVEL_FIELD_PATTERN.fullmatch(line)
        if not field_match:
            raise ValidationError(f"Invalid frontmatter syntax on line {line_number}.")
        key, raw_value = field_match.groups()
        if key in fields:
            raise ValidationError(f"Duplicate frontmatter field: {key}")
        fields[key] = parse_scalar(raw_value, line_number)
        current_key = key

    return fields, normalized[match.end() :]


def body_has_unfinished_todo(body: str) -> bool:
    fence_character: str | None = None
    fence_length = 0
    for line in body.splitlines():
        fence_match = re.match(
            r"^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$", line
        )
        if fence_match:
            marker = fence_match.group(1)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not fence_match.group(2).strip()
            ):
                fence_character = None
                fence_length = 0
            continue
        if fence_character is None and STANDALONE_TODO_PATTERN.fullmatch(line):
            return True
    return False


def validate_openai_yaml(skill_dir: Path, skill_name: str) -> list[str]:
    path = skill_dir / "agents" / "openai.yaml"
    if not path.exists():
        return []

    try:
        content = read_utf8(path)
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    prompt_match = re.search(
        r'^\s*default_prompt:\s*(["\'])(.*?)\1\s*$', content, re.MULTILINE
    )
    if prompt_match and f"${skill_name}" not in prompt_match.group(2):
        errors.append(
            f"agents/openai.yaml default_prompt must mention ${skill_name}."
        )
    return errors


def validate_skill(skill_dir: Path) -> list[str]:
    try:
        fields, body = parse_frontmatter(read_utf8(skill_dir / "SKILL.md"))
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    unexpected = sorted(set(fields) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected:
        errors.append(f"Unexpected frontmatter fields: {', '.join(unexpected)}")

    name = fields.get("name")
    description = fields.get("description")
    if not isinstance(name, str) or not name.strip():
        errors.append("Frontmatter requires a non-empty string field: name")
    else:
        name = name.strip()
        if not NAME_PATTERN.fullmatch(name):
            errors.append("name must contain only lowercase letters, digits, and hyphens.")
        if name.startswith("-") or name.endswith("-") or "--" in name:
            errors.append("name cannot start/end with a hyphen or contain consecutive hyphens.")
        if len(name) > 64:
            errors.append("name must be 64 characters or fewer.")

    if not isinstance(description, str) or not description.strip():
        errors.append("Frontmatter requires a non-empty string field: description")
    else:
        description = description.strip()
        if description.startswith("[TODO:"):
            errors.append("description contains an unfinished TODO placeholder.")
        if "<" in description or ">" in description:
            errors.append("description cannot contain angle brackets.")
        if len(description) > 1024:
            errors.append("description must be 1024 characters or fewer.")

    if body_has_unfinished_todo(body):
        errors.append("Skill instructions contain an unfinished TODO placeholder.")

    if isinstance(name, str) and name.strip():
        errors.extend(validate_openai_yaml(skill_dir, name.strip()))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_directory", type=Path)
    args = parser.parse_args()

    errors = validate_skill(args.skill_directory.resolve())
    if errors:
        print("Skill validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Skill is valid!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

