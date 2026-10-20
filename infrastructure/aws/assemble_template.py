#!/usr/bin/env python3
"""Assemble the SAM source manifest and resource fragments into one template.

The generated template is intentionally kept beside the source manifest so
relative CodeUri paths retain their existing meaning.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


AWS_DIR = Path(__file__).resolve().parent
SOURCE_TEMPLATE = AWS_DIR / "template.yml"
DEFAULT_OUTPUT = AWS_DIR / ".template-assembled.yml"
MODULES_PATTERN = re.compile(r"(?ms)^Modules:\s*\n((?:[ \t]+-\s+[^\n]+\n)+)")
RESOURCE_ID_PATTERN = re.compile(r"(?m)^  ([A-Za-z][A-Za-z0-9]*):\s*$")


def assemble_template(source: Path = SOURCE_TEMPLATE) -> str:
    """Return a normal SAM template with module resource fragments merged."""
    source = source.resolve()
    text = source.read_text(encoding="utf-8")
    modules_match = MODULES_PATTERN.search(text)
    if not modules_match:
        raise ValueError(f"No Modules list found in {source}")

    outputs_match = re.search(r"(?m)^Outputs:\s*$", text[modules_match.end() :])
    if not outputs_match:
        raise ValueError(f"No top-level Outputs section found in {source}")
    outputs_start = modules_match.end() + outputs_match.start()

    module_paths = [
        line.strip()[2:].strip()
        for line in modules_match.group(1).splitlines()
        if line.strip()
    ]
    fragments: list[str] = []
    seen_ids: set[str] = set()
    for relative_path in module_paths:
        module_path = (source.parent / relative_path).resolve()
        if not module_path.is_relative_to(AWS_DIR / "stacks"):
            raise ValueError(f"Module must be inside infrastructure/aws/stacks: {relative_path}")
        if not module_path.is_file():
            raise FileNotFoundError(f"Template module does not exist: {module_path}")
        fragment = module_path.read_text(encoding="utf-8").strip()
        if not fragment:
            raise ValueError(f"Template module is empty: {module_path}")
        resource_ids = RESOURCE_ID_PATTERN.findall(fragment)
        if not resource_ids:
            raise ValueError(f"No resource definitions found in {module_path}")
        duplicates = seen_ids.intersection(resource_ids)
        if duplicates:
            raise ValueError(f"Duplicate logical IDs across modules: {sorted(duplicates)}")
        seen_ids.update(resource_ids)
        fragments.append(f"  # {module_path.stem}\n{fragment}")

    prefix = text[: modules_match.start()]
    suffix = text[outputs_start:]
    return (
        prefix
        + "Resources:\n"
        + "\n\n".join(fragments)
        + "\n\n"
        + suffix
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Generated SAM template path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.write_text(assemble_template() + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
