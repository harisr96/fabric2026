"""
fabric_pipeline.py - run a Fabric deployment pipeline stage-to-stage, as a service principal,
with safety checks so only approved, synced content is promoted.

Usage
  Promote DEV -> TEST (first syncs DEV to develop and checks it):
    python fabric_pipeline.py --pipeline-id ID --to test \
        --dev-workspace-id DEV_ID --expected-dev-commit SHA_OF_DEVELOP

  Promote TEST -> PROD:
    python fabric_pipeline.py --pipeline-id ID --to prod

Safety checks before deploying to TEST
  1. DEV is updated from Git (develop) so it holds the latest approved commit.
  2. DEV's synced commit equals --expected-dev-commit (the develop commit being promoted).
  3. DEV has no uncommitted changes (nobody edited DEV directly).

Environment variables (GitHub secrets)
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET

Uses sign_in(), call(), status() and update() from fabric_git.py in the same folder.
"""
import argparse
import os
import sys

from fabric_git import call, sign_in, status, update

# Stage order in a 3-stage pipeline: 0 = Development, 1 = Test, 2 = Production
TARGET_ORDER = {"test": 1, "prod": 2}


def check_dev_ready(dev_workspace_id, expected_commit):
    print("Syncing DEV with the develop branch before promoting ...")
    update(dev_workspace_id)

    current = status(dev_workspace_id)
    head = current.get("workspaceHead") or ""
    if expected_commit and head != expected_commit:
        sys.exit(
            f"STOP: DEV is synced to commit {head[:7] or 'none'}, but the commit being promoted is "
            f"{expected_commit[:7]}. Wait for the 'Fabric deploy DEV' run on develop to finish, then re-run this workflow."
        )

    edited = [
        change.get("itemMetadata", {}).get("displayName", "?")
        for change in current.get("changes", [])
        if change.get("workspaceChange") not in (None, "None")
    ]
    if edited:
        sys.exit(
            "STOP: DEV has changes that are not in Git (someone edited DEV directly): "
            + ", ".join(edited)
            + ". Undo them in DEV (Source control > Undo) or make them through a feature branch, then re-run."
        )
    print(f"DEV is clean and on commit {head[:7]}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--to", required=True, choices=sorted(TARGET_ORDER))
    parser.add_argument("--dev-workspace-id", default="")
    parser.add_argument("--expected-dev-commit", default="")
    args = parser.parse_args()
    if not args.pipeline_id:
        sys.exit("Deployment pipeline ID is empty - check the DEPLOYMENT_PIPELINE_ID variable.")
    if args.to == "test" and not args.dev_workspace_id:
        sys.exit("Promoting to TEST needs --dev-workspace-id (check the DEV_WORKSPACE_ID variable).")

    sign_in()

    if args.to == "test":
        check_dev_ready(args.dev_workspace_id, args.expected_dev_commit)

    stages = call("GET", f"/deploymentPipelines/{args.pipeline_id}/stages").get("value", [])
    by_order = {stage.get("order"): stage for stage in stages}
    target = by_order.get(TARGET_ORDER[args.to])
    source = by_order.get(TARGET_ORDER[args.to] - 1)
    if not source or not target:
        sys.exit(f"Could not find the stages. The pipeline returned: {stages}")

    print(f"Deploying '{source.get('displayName')}' -> '{target.get('displayName')}' ...")
    commit = os.environ.get("GITHUB_SHA", "manual")[:7]
    body = {
        "sourceStageId": source["id"],
        "targetStageId": target["id"],
        "note": f"Deployed by GitHub Actions (commit {commit})",
    }
    call("POST", f"/deploymentPipelines/{args.pipeline_id}/deploy", body)
    print("Deployment finished.")


if __name__ == "__main__":
    main()