"""
fabric_cd.py - CD (continuous delivery) for fabric2026. Called only by the workflow fabric-cd.yml.

Commands
  update-dev --workspace-id DEV_ID
      Job "update-dev" (after a merge into develop). Pulls the newest develop commit into
      fabric2026-DEV. The first time, it also gives the service principal its own Git
      credentials on DEV, using the shared Fabric connection github-fabric2026-sp (guide Step 7).

  promote --pipeline-id ID --to test|prod [--dev-workspace-id DEV_ID --expected-dev-commit SHA]
      Job "promote" (after a merged pull request into test or main). Runs the deployment
      pipeline DEV -> TEST or TEST -> PROD. Before DEV -> TEST it checks: DEV is synced to
      develop, DEV is on the approved commit, and nobody edited DEV directly.

Environment variables (GitHub secrets)
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   service principal
  FABRIC_GIT_CONNECTION_NAME                               optional, default github-fabric2026-sp

Every message starts with a tag so a failed log is easy to read:
  [STEP] what the script is doing   [INFO] what it found   [OK] a step that worked
  [ERROR] what failed, followed by "Why:" and "Fix:" lines.
"""
import argparse          # reads the --workspace-id style options from the command line
import os                # reads environment variables (the GitHub secrets)
import sys               # sys.exit() stops the script with an error code, so the GitHub job turns red
import time              # time.sleep() while waiting for long-running Fabric operations

import requests                                    # sends HTTP requests to the Fabric REST API
from azure.identity import ClientSecretCredential  # signs in to Microsoft Entra ID as the service principal

sys.stdout.reconfigure(line_buffering=True)  # print messages in order in the GitHub log

# Every Fabric REST API address starts with this.
API = "https://api.fabric.microsoft.com/v1"
# One shared HTTP session: after sign_in() it carries the access token on every request.
SESSION = requests.Session()


# ===========================================================================
# PART 1 - Fabric API helpers and DEV update
# ===========================================================================
# ---------------------------------------------------------------------------
# Error hints: when Fabric returns an error, we look up its errorCode (or the
# HTTP status) here and print a plain-English "Why" and "Fix" under it.
# ---------------------------------------------------------------------------
# Known Fabric error codes -> (why it happens, how to fix it)
HINTS = {
    "GitCredentialsNotConfigured": (
        "The service principal has no Git credentials on this workspace yet.",
        "Run the 'Fabric CD' workflow once by hand (guide Step 7.3); it sets them using the shared connection.",
    ),
    "DeploymentPipelineStageHasNoAssignedWorkspace": (
        "A stage of the deployment pipeline has no workspace.",
        "Fabric > Deployment pipelines > your pipeline > the stage > Assign a workspace (guide Step 8).",
    ),
    "FeatureNotAvailable": (
        "A tenant setting is off (Git / GitHub integration or service principal API access).",
        "Admin portal > Tenant settings: enable the Git, GitHub and 'Service principals can call Fabric "
        "public APIs' settings (guide Step 1.3), wait 15-30 minutes, then re-run.",
    ),
    "PrincipalTypeNotSupported": (
        "This action or item type can't be done by a service principal.",
        "Do this step once by hand in Fabric (Source control > Update all, or pipeline > Deploy).",
    ),
    "InsufficientPrivileges": (
        "The service principal doesn't have enough rights.",
        "Make sp-fabric2026-cicd Admin on the workspace and on the deployment pipeline (guide Steps 3 and 8).",
    ),
    "WorkspaceNotConnectedToGit": (
        "The workspace isn't connected to Git.",
        "Connect DEV to the develop branch by hand (guide Step 7.2).",
    ),
    "GitProviderResourceNotFound": (
        "GitHub couldn't find the repo or branch, or the token can't see it.",
        "Check the branch exists and the token in the connection github-fabric2026-sp is valid "
        "with Contents: Read and write on fabric2026.",
    ),
}
STATUS_HINTS = {
    400: ("Fabric rejected the request.", "Read the errorCode and message above."),
    401: ("Sign-in failed or the token is not accepted.",
          "Check AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET; create a new client secret if it expired."),
    403: ("The service principal is not allowed to do this.",
          "Check the tenant setting 'Service principals can call Fabric public APIs' includes sg-fabric2026-cicd, "
          "and that the service principal is Admin on the workspace / pipeline."),
    404: ("The workspace, pipeline or item was not found, or the service principal can't see it.",
          "Check DEV_WORKSPACE_ID / DEPLOYMENT_PIPELINE_ID, and that the service principal has access."),
    429: ("Too many requests to Fabric.", "Wait a few minutes, then re-run."),
}


# ---------------------------------------------------------------------------
# Small print helpers so every line in the GitHub log has a clear tag.
# ---------------------------------------------------------------------------
def info(message):
    print(f"[INFO] {message}")


def step(message):
    print(f"\n[STEP] {message}")


def ok(message):
    print(f"[OK] {message}")


def fail(message, why=None, fix=None):
    """Print a clear error (also shown as a red annotation in GitHub) and stop."""
    print(f"::error::{message}")   # '::error::' makes GitHub show the message in red on the run page
    print(f"[ERROR] {message}")
    if why:
        print(f"  Why: {why}")
    if fix:
        print(f"  Fix: {fix}")
    sys.exit(1)


def env(name, where="GitHub repo > Settings > Secrets and variables > Actions"):
    """Read a required environment variable; stop with a clear message if it's missing."""
    value = os.environ.get(name)
    if not value:
        fail(f"Missing value: {name}", f"{name} is empty or not passed to this workflow step.", f"Add it in {where}.")
    return value


def masked(value):
    """Show only the first and last 4 characters of an ID in the log (never print secrets)."""
    return f"{value[:4]}...{value[-4:]}" if value and len(value) > 8 else "(empty)"


# ---------------------------------------------------------------------------
# Sign-in and the generic API call used by everything else
# ---------------------------------------------------------------------------
def sign_in():
    """Get an access token for the Fabric API using the service principal's ID and secret."""
    step("Signing in to Fabric as the service principal")
    tenant, client = env("AZURE_TENANT_ID"), env("AZURE_CLIENT_ID")
    info(f"Tenant {masked(tenant)}, client {masked(client)}")
    try:
        credential = ClientSecretCredential(tenant_id=tenant, client_id=client, client_secret=env("AZURE_CLIENT_SECRET"))
        # ".default" = all Fabric API permissions the service principal has been given
        token = credential.get_token("https://api.fabric.microsoft.com/.default").token
    except Exception as error:  # azure-identity raises several error types
        fail(
            "Could not sign in to Microsoft Entra ID",
            f"{type(error).__name__}: {str(error).splitlines()[0]}",
            "Check the three AZURE_* secrets. 'AADSTS7000215' = wrong secret, 'AADSTS7000222' = secret expired, "
            "'AADSTS700016' = wrong client ID, 'AADSTS90002' = wrong tenant ID.",
        )
    SESSION.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    ok("Signed in")


def explain_error(method, path, status_code, payload):
    """Turn a Fabric error response into Why/Fix lines and stop the script."""
    # Fabric errors look like {"errorCode": "...", "message": "...", "requestId": "..."};
    # failed long-running operations nest them under "error".
    if isinstance(payload, dict):
        code = payload.get("errorCode") or (payload.get("error") or {}).get("errorCode") or ""
        message = payload.get("message") or (payload.get("error") or {}).get("message") or ""
        request_id = payload.get("requestId", "")
    else:
        code, message, request_id = "", str(payload)[:500], ""
    why, fix = HINTS.get(code) or STATUS_HINTS.get(status_code, ("Unexpected error.", "Read the message above."))
    print(f"  HTTP status : {status_code}")
    print(f"  Error code  : {code or '-'}")
    print(f"  Message     : {message or '-'}")
    if request_id:
        print(f"  Request ID  : {request_id} (give this to Microsoft support if needed)")
    fail(f"{method} {path} failed ({status_code} {code})".strip(), why, fix)


def call(method, path, body=None):
    """Call the Fabric API and return its JSON answer.

    Some calls (update from Git, deploy) take minutes. Fabric then answers 202 Accepted
    with an operation ID; we ask about that operation every few seconds until it has
    Succeeded or Failed, and then fetch its result.
    """
    try:
        response = SESSION.request(method, f"{API}{path}", json=body, timeout=120)
    except requests.RequestException as error:
        fail(f"{method} {path}: could not reach Fabric", str(error), "Usually temporary - re-run the job.")

    if response.status_code == 202:
        operation_id = response.headers.get("x-ms-operation-id")
        if not operation_id:
            return {}
        info(f"{method} {path} started (operation {operation_id})")
        wait = int(response.headers.get("Retry-After", 5))
        started = time.time()
        while True:
            time.sleep(wait)
            state = SESSION.get(f"{API}/operations/{operation_id}", timeout=120)
            if state.status_code >= 400:
                explain_error("GET", f"/operations/{operation_id}", state.status_code, safe_json(state))
            data = state.json()
            status = data.get("status")
            percent = data.get("percentComplete")
            info(f"  ... {status}" + (f" {percent}%" if percent is not None else "") + f" ({int(time.time() - started)}s)")
            if status == "Succeeded":
                break
            if status not in ("NotStarted", "Running"):
                print("  Operation details:", data.get("error") or data)
                explain_error(method, path, 500, data.get("error") or data)
        result = SESSION.get(f"{API}/operations/{operation_id}/result", timeout=120)
        return result.json() if result.status_code == 200 and result.content else {}

    if response.status_code >= 400:
        explain_error(method, path, response.status_code, safe_json(response))
    return response.json() if response.content else {}


def safe_json(response):
    """Return the response as JSON if it is JSON, otherwise as plain text."""
    try:
        return response.json()
    except ValueError:
        return response.text


# ---------------------------------------------------------------------------
# Git steps for the DEV workspace
# ---------------------------------------------------------------------------
def find_connection():
    """The Fabric connection that stores the GitHub token (created and shared by hand, guide Step 7.1)."""
    name = os.environ.get("FABRIC_GIT_CONNECTION_NAME") or "github-fabric2026-sp"
    step(f"Looking for the Fabric connection '{name}'")
    connections = call("GET", "/connections").get("value", [])
    for connection in connections:
        if connection.get("displayName") == name:
            ok(f"Using existing connection '{name}'")
            return connection["id"]
    visible = ", ".join(c.get("displayName", "?") for c in connections) or "none"
    fail(
        f"Connection '{name}' not found",
        f"The service principal can see these connections: {visible}.",
        f"Fabric > gear > Manage connections and gateways > '{name}' > ... > Manage users > add "
        "sp-fabric2026-cicd (or sg-fabric2026-cicd) as User (guide Step 7.1).",
    )


def ensure_my_git_credentials(workspace_id):
    """Each identity needs its own Git credentials on a workspace. DEV was connected by a person,
    so give the service principal credentials too, using the shared connection."""
    step("Checking the service principal's Git credentials on the workspace")
    current = call("GET", f"/workspaces/{workspace_id}/git/myGitCredentials")
    source = current.get("source")
    info(f"Current credentials source: {source}")
    if source == "ConfiguredConnection":
        ok("Service principal Git credentials already set")
        return
    connection_id = find_connection()
    call(
        "PATCH",
        f"/workspaces/{workspace_id}/git/myGitCredentials",
        {"source": "ConfiguredConnection", "connectionId": connection_id},
    )
    ok("Service principal Git credentials set on this workspace.")


def show_connection(workspace_id):
    """Print which repo/branch the workspace is connected to and return the connection state."""
    step(f"Reading the Git connection of workspace {workspace_id}")
    connection = call("GET", f"/workspaces/{workspace_id}/git/connection")
    state = connection.get("gitConnectionState", "NotConnected")
    details = connection.get("gitProviderDetails") or {}
    info(f"State: {state}")
    if details:
        info(f"Repo: {details.get('ownerName')}/{details.get('repositoryName')}, "
             f"branch: {details.get('branchName')}, folder: {details.get('directoryName') or '/'}")
    return state


def status(workspace_id):
    """Print and return the workspace's Git status.

    remoteCommitHash = the newest commit on the branch in GitHub
    workspaceHead    = the commit the workspace was last synced to
    changes          = items that differ (workspaceChange = edited in Fabric, remoteChange = changed in Git)
    """
    current = call("GET", f"/workspaces/{workspace_id}/git/status")
    changes = current.get("changes", [])
    info(f"Git commit (remote): {current.get('remoteCommitHash') or '-'}")
    info(f"Workspace is on    : {current.get('workspaceHead') or '-'}")
    info(f"Items with changes : {len(changes)}")
    for change in changes:
        meta = change.get("itemMetadata", {})
        info(f"  - {meta.get('displayName', '?')} ({meta.get('itemType', '?')}): "
             f"workspace={change.get('workspaceChange')}, git={change.get('remoteChange')}, "
             f"conflict={change.get('conflictType')}")
    return current


def update_from_git(workspace_id):
    """Bring the workspace up to the newest commit of its branch (Git always wins)."""
    step("Comparing the workspace with Git")
    current = status(workspace_id)
    remote = current.get("remoteCommitHash")
    head = current.get("workspaceHead")
    if remote and remote == head:
        ok("Workspace is already up to date with Git.")
        return
    body = {
        "remoteCommitHash": remote,
        # If an item was edited in the workspace AND in Git, keep the Git version.
        "conflictResolution": {"conflictResolutionType": "Workspace", "conflictResolutionPolicy": "PreferRemote"},
        # Allow Fabric to overwrite items that already exist in the workspace.
        "options": {"allowOverrideItems": True},
    }
    if head:
        body["workspaceHead"] = head
    step(f"Updating workspace from Git commit {remote}")
    call("POST", f"/workspaces/{workspace_id}/git/updateFromGit", body)
    ok("Update from Git finished.")


def update(workspace_id):
    """The full 'update' command: check the connection, set credentials if needed, then sync."""
    if show_connection(workspace_id) == "NotConnected":
        fail(
            "The workspace is not connected to Git",
            "DEV must be connected to the develop branch before it can be updated.",
            "Connect it by hand: workspace > Workspace settings > Git integration (guide Step 7.2).",
        )
    ensure_my_git_credentials(workspace_id)
    update_from_git(workspace_id)


# ===========================================================================
# PART 2 - Deployment pipeline
# ===========================================================================
# A 3-stage deployment pipeline numbers its stages: 0 = Development, 1 = Test, 2 = Production.
# "--to test" deploys stage 0 -> 1, "--to prod" deploys stage 1 -> 2.
TARGET_ORDER = {"test": 1, "prod": 2}


def check_dev_ready(dev_workspace_id, expected_commit):
    """Make sure DEV holds exactly the approved develop commit and nothing else."""
    step("Safety check 1/3: sync DEV with the develop branch")
    update(dev_workspace_id)

    step("Safety check 2/3: DEV is on the commit being promoted")
    current = status(dev_workspace_id)
    head = current.get("workspaceHead") or ""
    info(f"Expected develop commit: {expected_commit or '(not given - skipped)'}")
    if expected_commit and head != expected_commit:
        fail(
            f"DEV is synced to commit {head[:7] or 'none'}, but the commit being promoted is {expected_commit[:7]}",
            "develop moved on, or the update-dev job hasn't finished updating DEV yet.",
            "Wait for the latest 'Fabric CD' update-dev run to go green, then Re-run jobs on this run.",
        )
    ok(f"DEV is on commit {head[:7]}")

    step("Safety check 3/3: nobody edited DEV directly")
    # workspaceChange is set when an item was changed in Fabric but not committed to Git.
    edited = [
        change.get("itemMetadata", {}).get("displayName", "?")
        for change in current.get("changes", [])
        if change.get("workspaceChange") not in (None, "None")
    ]
    if edited:
        fail(
            "DEV has changes that are not in Git: " + ", ".join(edited),
            "Someone edited these items directly in fabric2026-DEV.",
            "DEV > Source control > Undo those changes (or make them through a feature branch), then Re-run jobs.",
        )
    ok("DEV is clean - no uncommitted changes")


def find_stages(pipeline_id, target_order):
    """Return (source stage, target stage) and print every stage with its workspace."""
    step(f"Reading the stages of deployment pipeline {pipeline_id}")
    stages = call("GET", f"/deploymentPipelines/{pipeline_id}/stages").get("value", [])
    for stage in sorted(stages, key=lambda s: s.get("order", 0)):
        workspace = stage.get("workspaceName") or stage.get("workspaceId") or "NO WORKSPACE ASSIGNED"
        info(f"Stage {stage.get('order')}: {stage.get('displayName')} -> {workspace}")

    by_order = {stage.get("order"): stage for stage in stages}
    source, target = by_order.get(target_order - 1), by_order.get(target_order)
    if not source or not target:
        fail(
            "Could not find the source and target stages",
            f"The pipeline returned {len(stages)} stage(s); a 3-stage pipeline is expected.",
            "Check DEPLOYMENT_PIPELINE_ID (the ID after /pipelines/ in the address bar).",
        )
    for stage in (source, target):
        if not stage.get("workspaceId"):
            fail(
                f"Stage '{stage.get('displayName')}' has no workspace",
                "A deployment can only run between stages that have a workspace.",
                "Fabric > Deployment pipelines > the stage > Assign a workspace (guide Step 8).",
            )
    return source, target


# ===========================================================================
# PART 3 - The two commands
# ===========================================================================
def run_update_dev(args):
    """update-dev: bring DEV up to the newest develop commit."""
    if not args.workspace_id:
        fail("Workspace ID is empty", "The DEV_WORKSPACE_ID variable is missing.",
             "GitHub repo > Settings > Secrets and variables > Actions > Variables > DEV_WORKSPACE_ID.")
    print(f"fabric_cd.py update-dev - workspace {args.workspace_id}")
    sign_in()
    update(args.workspace_id)
    print("\n[DONE] DEV is up to date with develop.")


def run_promote(args):
    """promote: safety checks, then run the deployment pipeline."""
    # 1. Show and check the options passed by the workflow.
    print(f"fabric_cd.py promote - to {args.to.upper()}")
    info(f"Pipeline ID      : {args.pipeline_id or '(empty)'}")
    info(f"DEV workspace ID : {args.dev_workspace_id or '(not needed)'}")
    info(f"Triggered by     : {os.environ.get('GITHUB_EVENT_NAME', 'manual')} on commit "
         f"{os.environ.get('GITHUB_SHA', 'unknown')[:7]}")
    if not args.pipeline_id:
        fail("Deployment pipeline ID is empty", "The DEPLOYMENT_PIPELINE_ID variable is missing.",
             "GitHub repo > Settings > Secrets and variables > Actions > Variables (guide Step 8).")
    if args.to == "test" and not args.dev_workspace_id:
        fail("DEV workspace ID is empty", "Promoting to TEST needs DEV_WORKSPACE_ID.",
             "GitHub repo > Settings > Secrets and variables > Actions > Variables (guide Step 5).")

    # 2. Sign in as the service principal.
    sign_in()

    # 3. DEV -> TEST only: DEV must exactly match the approved develop commit.
    if args.to == "test":
        check_dev_ready(args.dev_workspace_id, args.expected_dev_commit)

    # 4. Find the two stages and start the deployment.
    source, target = find_stages(args.pipeline_id, TARGET_ORDER[args.to])
    commit = os.environ.get("GITHUB_SHA", "manual")[:7]
    body = {
        "sourceStageId": source["id"],
        "targetStageId": target["id"],
        # The note shows in Fabric > pipeline > Deployment history, linking the deploy to a commit.
        "note": f"Deployed by GitHub Actions (commit {commit})",
    }
    step(f"Deploying '{source.get('displayName')}' -> '{target.get('displayName')}'")
    call("POST", f"/deploymentPipelines/{args.pipeline_id}/deploy", body)  # waits until it finishes
    ok("Deployment finished.")
    print(f"\n[DONE] {target.get('displayName')} now has the approved items. "
          "Data is not copied: run the notebook in that workspace if its tables are needed.")


# Entry point: python .github/scripts/fabric_cd.py update-dev|promote [options]
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_dev = sub.add_parser("update-dev", help="update DEV from develop")
    p_dev.add_argument("--workspace-id", required=True)
    p_dev.set_defaults(run=run_update_dev)

    p_promote = sub.add_parser("promote", help="deploy DEV -> TEST or TEST -> PROD")
    p_promote.add_argument("--pipeline-id", required=True)
    p_promote.add_argument("--to", required=True, choices=sorted(TARGET_ORDER))
    p_promote.add_argument("--dev-workspace-id", default="")
    p_promote.add_argument("--expected-dev-commit", default="")
    p_promote.set_defaults(run=run_promote)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()