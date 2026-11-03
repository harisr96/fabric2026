"""
fabric_git.py - connect and sync Fabric workspaces with GitHub, as a service principal.

Commands
  setup   --workspace-id ID --branch BRANCH --strategy PreferWorkspace|PreferRemote
          Connects the workspace to the branch (once) and does the first sync.
          PreferWorkspace = workspace items win and are committed to Git (DEV).
          PreferRemote    = Git wins and the workspace is updated from Git (TEST, PROD).
  update  --workspace-id ID
          Pulls the latest commit of the connected branch into the workspace.

Environment variables (set as GitHub secrets / variables)
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   service principal
  FABRIC_GIT_PAT                                           GitHub token (setup only)
  GH_OWNER, GH_REPO                                        e.g. hari-sr / fabric2026
  FABRIC_GIT_CONNECTION_NAME                               optional, default github-fabric2026-sp
  FABRIC_GIT_FOLDER                                        optional, default fabric
"""
import argparse
import os
import sys
import time

import requests
from azure.identity import ClientSecretCredential

API = "https://api.fabric.microsoft.com/v1"
SESSION = requests.Session()


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"Missing environment variable: {name}")
    return value


def sign_in():
    credential = ClientSecretCredential(
        tenant_id=env("AZURE_TENANT_ID"),
        client_id=env("AZURE_CLIENT_ID"),
        client_secret=env("AZURE_CLIENT_SECRET"),
    )
    token = credential.get_token("https://api.fabric.microsoft.com/.default").token
    SESSION.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})


def call(method, path, body=None):
    """Call the Fabric API. Waits for long-running operations and returns their result."""
    response = SESSION.request(method, f"{API}{path}", json=body)
    if response.status_code == 202:
        operation_id = response.headers.get("x-ms-operation-id")
        if not operation_id:
            return {}
        wait = int(response.headers.get("Retry-After", 5))
        while True:
            time.sleep(wait)
            state = SESSION.get(f"{API}/operations/{operation_id}")
            state.raise_for_status()
            status = state.json().get("status")
            print(f"  ... {method} {path}: {status}")
            if status == "Succeeded":
                break
            if status not in ("NotStarted", "Running"):
                sys.exit(f"Operation failed: {state.text}")
        result = SESSION.get(f"{API}/operations/{operation_id}/result")
        if result.status_code == 200 and result.content:
            return result.json()
        return {}
    if response.status_code >= 400:
        sys.exit(f"{method} {path} failed ({response.status_code}): {response.text}")
    return response.json() if response.content else {}


def get_or_create_connection():
    """A Fabric connection that stores the GitHub token. The service principal owns it."""
    name = os.environ.get("FABRIC_GIT_CONNECTION_NAME") or "github-fabric2026-sp"
    for connection in call("GET", "/connections").get("value", []):
        if connection.get("displayName") == name:
            print(f"Using existing connection '{name}'")
            return connection["id"]

    owner, repo = env("GH_OWNER"), env("GH_REPO")
    body = {
        "displayName": name,
        "connectivityType": "ShareableCloud",
        "connectionDetails": {
            "type": "GitHubSourceControl",
            "creationMethod": "GitHubSourceControl.Contents",
            "parameters": [
                {"dataType": "Text", "name": "url", "value": f"https://github.com/{owner}/{repo}"}
            ],
        },
        "credentialDetails": {
            "singleSignOnType": "None",
            "connectionEncryption": "NotEncrypted",
            "skipTestConnection": False,
            "credentials": {"credentialType": "Key", "key": env("FABRIC_GIT_PAT")},
        },
    }
    created = call("POST", "/connections", body)
    print(f"Created connection '{name}'")
    return created["id"]


def git_state(workspace_id):
    return call("GET", f"/workspaces/{workspace_id}/git/connection").get("gitConnectionState", "NotConnected")


def status(workspace_id):
    return call("GET", f"/workspaces/{workspace_id}/git/status")


def update_from_git(workspace_id, remote_commit_hash=None, workspace_head=None):
    if remote_commit_hash is None:
        current = status(workspace_id)
        remote_commit_hash = current.get("remoteCommitHash")
        workspace_head = current.get("workspaceHead")
        if remote_commit_hash and remote_commit_hash == workspace_head:
            print("Workspace is already up to date with Git.")
            return
    body = {
        "remoteCommitHash": remote_commit_hash,
        "conflictResolution": {
            "conflictResolutionType": "Workspace",
            "conflictResolutionPolicy": "PreferRemote",
        },
        "options": {"allowOverrideItems": True},
    }
    if workspace_head:
        body["workspaceHead"] = workspace_head
    print(f"Updating workspace from Git commit {remote_commit_hash} ...")
    call("POST", f"/workspaces/{workspace_id}/git/updateFromGit", body)
    print("Update from Git finished.")


def commit_to_git(workspace_id, comment):
    current = status(workspace_id)
    body = {"mode": "All", "comment": comment}
    if current.get("workspaceHead"):
        body["workspaceHead"] = current["workspaceHead"]
    print("Committing all workspace items to Git ...")
    call("POST", f"/workspaces/{workspace_id}/git/commitToGit", body)
    print("Commit finished.")


def setup(workspace_id, branch, strategy):
    state = git_state(workspace_id)
    print(f"Workspace Git state: {state}")

    if state == "NotConnected":
        connection_id = get_or_create_connection()
        body = {
            "gitProviderDetails": {
                "gitProviderType": "GitHub",
                "ownerName": env("GH_OWNER"),
                "repositoryName": env("GH_REPO"),
                "branchName": branch,
                "directoryName": os.environ.get("FABRIC_GIT_FOLDER") or "fabric",
            },
            "myGitCredentials": {"source": "ConfiguredConnection", "connectionId": connection_id},
        }
        call("POST", f"/workspaces/{workspace_id}/git/connect", body)
        print(f"Connected workspace to branch '{branch}'.")
        state = "Connected"

    if state == "Connected":
        result = call(
            "POST",
            f"/workspaces/{workspace_id}/git/initializeConnection",
            {"initializationStrategy": strategy},
        )
        action = result.get("requiredAction", "None")
        print(f"Initialized. Required action: {action}")
        if action == "UpdateFromGit":
            update_from_git(workspace_id, result.get("remoteCommitHash"), result.get("workspaceHead"))
        elif action == "CommitToGit":
            commit_to_git(workspace_id, "Initial commit from DEV workspace")
        return

    print("Workspace is already connected and initialized. Nothing to set up.")


def update(workspace_id):
    state = git_state(workspace_id)
    if state == "NotConnected":
        print("Workspace is not connected to Git yet - skipping. Run the setup workflow first.")
        return
    update_from_git(workspace_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--workspace-id", required=True)
    p_setup.add_argument("--branch", required=True)
    p_setup.add_argument("--strategy", required=True, choices=["PreferWorkspace", "PreferRemote"])

    p_update = sub.add_parser("update")
    p_update.add_argument("--workspace-id", required=True)

    args = parser.parse_args()
    if not args.workspace_id:
        sys.exit("Workspace ID is empty - check the repository variables.")

    sign_in()
    if args.command == "setup":
        setup(args.workspace_id, args.branch, args.strategy)
    else:
        update(args.workspace_id)


if __name__ == "__main__":
    main()