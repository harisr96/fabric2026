"""
ci_checks.py - checks that run on every pull request (GitHub Actions: Fabric CI).

1. Branch flow: test only accepts develop; main only accepts test;
   develop does not accept test or main.
2. Notebook code (not the Fabric '# META' metadata lines) has no hard-coded GUIDs,
   so it keeps working in every workspace. Use lakehouse.schema.table names instead.
3. Notebook code is valid Python (cell magics like %%sql are skipped).
4. No obvious secrets (client secrets, GitHub tokens) are committed under fabric/.

Usage: python ci_checks.py --base BASE_BRANCH --head HEAD_BRANCH [--root fabric]
"""
import argparse
import ast
import pathlib
import re
import sys

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SECRETS = [
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"(?i)client_?secret\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
]
ALLOWED_SOURCES = {"test": {"develop"}, "main": {"test"}}


def check_branch_flow(base, head):
    problems = []
    allowed = ALLOWED_SOURCES.get(base)
    if allowed is not None and head not in allowed:
        problems.append(f"Pull requests into '{base}' must come from {sorted(allowed)}, not '{head}'.")
    if base == "develop" and head in ("test", "main"):
        problems.append(f"Don't merge '{head}' back into develop.")
    return problems


def is_marker(line):
    """Fabric's own structure lines: '# META ...', '# METADATA ****', '# CELL ****', '# MARKDOWN ****'."""
    return line.startswith(("# META", "# CELL", "# MARKDOWN"))


def check_notebooks(root):
    problems = []
    for path in sorted(root.rglob("notebook-content.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        python_lines = []          # same length as the file, so error line numbers stay exact
        in_magic_cell = False      # a cell that starts with %%sql, %%pyspark etc. is not Python
        cell_started = False
        for number, line in enumerate(lines, start=1):
            if line.startswith("# CELL") or line.startswith("# MARKDOWN") or line.startswith("# METADATA"):
                in_magic_cell, cell_started = False, line.startswith("# CELL")
                python_lines.append("")
                continue
            if is_marker(line):
                python_lines.append("")
                continue
            if GUID.search(line):
                problems.append(f"{path}:{number}: hard-coded GUID in notebook code -> use names like bronze.dbo.orders")
            stripped = line.strip()
            if cell_started and stripped:
                in_magic_cell = stripped.startswith("%%")
                cell_started = False
            if in_magic_cell or stripped.startswith(("%", "!")):
                python_lines.append("")
            else:
                python_lines.append(line)
        try:
            ast.parse("\n".join(python_lines), filename=str(path))
        except SyntaxError as error:
            problems.append(f"{path}:{error.lineno}: Python syntax error: {error.msg}")
    return problems


def check_secrets(root):
    problems = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRETS:
            if pattern.search(text):
                problems.append(f"{path}: looks like a secret or token is committed - remove it and rotate it")
                break
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default="fabric")
    args = parser.parse_args()

    root = pathlib.Path(args.root)
    problems = check_branch_flow(args.base, args.head)
    if root.exists():
        problems += check_notebooks(root)
        problems += check_secrets(root)
    else:
        print(f"Note: folder '{root}' not found - skipping item checks.")

    if problems:
        print("Fabric CI found problems:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("Fabric CI: all checks passed.")


if __name__ == "__main__":
    main()