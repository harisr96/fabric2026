"""
fabric_ci.py - CI (continuous integration) checks for fabric2026. Called only by the workflow fabric-ci.yml,
on every pull request into develop, test or main.

Checks
  1. Branch flow: test only accepts develop; main only accepts test; develop does not accept test or main.
  2. Notebook code (not the Fabric '# META' metadata lines) has no hard-coded GUIDs,
     so it keeps working in every workspace. Use lakehouse.schema.table names instead.
  3. Notebook code is valid Python (cell magics like %%sql are skipped).
  4. No obvious secrets (client secrets, GitHub tokens) are committed in Fabric items.

Usage: python .github/scripts/fabric_ci.py --base BASE_BRANCH --head HEAD_BRANCH [--root .]
Needs only Python - no packages, no sign-in. Each check prints [PASS] or [FAIL]; every
problem is listed with file:line and how to fix it.
"""
import argparse   # reads the --base / --head options from the command line
import ast        # parses notebook code to find Python syntax errors without running it
import pathlib    # walks the repo folders
import re         # patterns for IDs and secrets
import sys        # sys.exit(1) makes the GitHub check fail

sys.stdout.reconfigure(line_buffering=True)  # print messages in order in the GitHub log


# ===========================================================================
# The checks
# ===========================================================================
# A GUID looks like 1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d (workspace, lakehouse and item IDs).
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Patterns that look like real secrets: GitHub tokens and a client secret written in code.
SECRETS = [
    ("GitHub classic token", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("client secret", re.compile(r"(?i)client_?secret\s*[=:]\s*['\"][^'\"]{8,}['\"]")),
]
# Which branch may open a pull request into which: test <- develop, main <- test.
ALLOWED_SOURCES = {"test": {"develop"}, "main": {"test"}}
SKIP_DIRS = {".git", ".github"}   # automation files, not Fabric items


def item_files(root, pattern):
    """All files matching pattern under the repo root, except .git and .github."""
    for path in sorted(root.rglob(pattern)):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def report(name, problems, checked=None):
    """Print one check's result line."""
    detail = f" ({checked})" if checked else ""
    if problems:
        print(f"[FAIL] {name}{detail}: {len(problems)} problem(s)")
    else:
        print(f"[PASS] {name}{detail}")
    return problems


def check_branch_flow(base, head):
    """Check 1: the pull request goes between the right branches."""
    problems = []
    allowed = ALLOWED_SOURCES.get(base)
    if allowed is not None and head not in allowed:
        problems.append(
            f"Pull requests into '{base}' must come from {sorted(allowed)}, not '{head}'. "
            f"Fix: close this pull request; merge into develop first, then promote develop -> test -> main."
        )
    if base == "develop" and head in ("test", "main"):
        problems.append(f"Don't merge '{head}' back into develop. Fix: close this pull request.")
    return report(f"Branch flow ({head} -> {base})", problems)


def is_marker(line):
    """Fabric's own structure lines: '# META ...', '# METADATA ****', '# CELL ****', '# MARKDOWN ****'."""
    return line.startswith(("# META", "# CELL", "# MARKDOWN"))


def check_notebooks(root):
    """Checks 2 and 3: no GUIDs in notebook code, and the code is valid Python.

    Fabric saves a notebook as notebook-content.py: Python cells separated by
    '# CELL' lines, plus '# META' lines holding settings (those may contain IDs - that's fine).
    """
    guid_problems, syntax_problems = [], []
    notebooks = list(item_files(root, "notebook-content.py"))
    for path in notebooks:
        lines = path.read_text(encoding="utf-8").splitlines()
        python_lines = []          # same length as the file, so error line numbers stay exact
        in_magic_cell = False      # a cell that starts with %%sql, %%pyspark etc. is not Python
        cell_started = False
        for number, line in enumerate(lines, start=1):
            # A new cell (or markdown/metadata block) starts: reset the magic-cell flag.
            if line.startswith("# CELL") or line.startswith("# MARKDOWN") or line.startswith("# METADATA"):
                in_magic_cell, cell_started = False, line.startswith("# CELL")
                python_lines.append("")
                continue
            if is_marker(line):
                python_lines.append("")
                continue
            if GUID.search(line):
                guid_problems.append(
                    f"{path}:{number}: hard-coded ID '{GUID.search(line).group()}' in notebook code. "
                    f"Fix: use names like bronze.dbo.orders instead of workspace or lakehouse IDs."
                )
            stripped = line.strip()
            # The first non-empty line of a cell decides whether the whole cell is a magic cell.
            if cell_started and stripped:
                in_magic_cell = stripped.startswith("%%")
                cell_started = False
            # Blank out magic cells and single magic lines (%pip, !ls) so Python can parse the rest.
            if in_magic_cell or stripped.startswith(("%", "!")):
                python_lines.append("")
            else:
                python_lines.append(line)
        try:
            ast.parse("\n".join(python_lines), filename=str(path))
        except SyntaxError as error:
            syntax_problems.append(
                f"{path}:{error.lineno}: Python syntax error: {error.msg}. "
                f"Fix: open the notebook in your workspace, fix that line, run it, commit again."
            )
    names = ", ".join(str(p.parent.relative_to(root)) for p in notebooks) or "no notebooks found"
    problems = report("No hard-coded IDs in notebook code", guid_problems, names)
    problems += report("Notebook code is valid Python", syntax_problems, names)
    return problems


def check_secrets(root):
    """Check 4: no tokens or client secrets committed in Fabric item files."""
    problems = []
    files = list(item_files(root, "*"))
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue   # binary file - nothing to scan
        for label, pattern in SECRETS:
            if pattern.search(text):
                problems.append(
                    f"{path}: looks like a {label} is committed. "
                    f"Fix: remove it, commit again, and create a new token/secret (the old one is exposed)."
                )
                break
    return report("No secrets in Fabric items", problems, f"{len(files)} files scanned")


# Entry point: python .github/scripts/fabric_ci.py --base BRANCH --head BRANCH
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    print(f"Fabric CI - pull request {args.head} -> {args.base}\n")
    root = pathlib.Path(args.root)
    problems = check_branch_flow(args.base, args.head)
    if root.exists():
        problems += check_notebooks(root)
        problems += check_secrets(root)
    else:
        print(f"[INFO] Folder '{root}' not found - skipping item checks.")

    if problems:
        print("\nFabric CI found problems:")
        for problem in problems:
            print(f"::error::{problem}")   # also shown in red on the pull request's run page
            print(f"  - {problem}")
        sys.exit(1)
    print("\n[DONE] Fabric CI: all checks passed.")


if __name__ == "__main__":
    main()