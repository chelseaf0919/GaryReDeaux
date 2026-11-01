"""
google_auth_setup.py - One-time Google login so Gary can use your calendar and email.

Run this ONCE on your own computer (not on Railway):
    python google_auth_setup.py

It needs the OAuth client file you downloaded from Google Cloud sitting in this
folder (Google names it client_secret_....json -- no need to rename it).
A browser window opens, you approve access, and this script:
  1. saves GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN to .env
  2. prints the same three values so you can paste them into Railway

Re-running it is safe; it just replaces the old values.
"""

import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit("Missing a package. Run this first, then try again:\n    pip install google-auth-oauthlib")

HERE = Path(__file__).parent

# Everything Gary will need, requested in one go so this only has to be done once.
# gmail.compose lets him create drafts; Gary's code has no send-email tool.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def find_client_file() -> Path:
    candidates = list(HERE.glob("client_secret*.json")) + list(HERE.glob("google_credentials.json"))
    if not candidates:
        sys.exit(
            "Couldn't find your Google client file.\n"
            "Download it from Google Cloud (Clients -> your client -> Download JSON)\n"
            f"and put it in this folder:\n    {HERE}"
        )
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if len(candidates) > 1:
        print(f"Found {len(candidates)} client files, using the newest: {newest.name}")
    return newest


def save_to_env(values: dict):
    env_path = HERE / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    client_file = find_client_file()
    print(f"Using {client_file.name}")
    print("Opening your browser -- sign in with the Google account Gary should manage...\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    # prompt=consent guarantees Google hands back a refresh token even if
    # you've approved this app before.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        success_message="All done! Gary has access. You can close this tab and go back to VS Code.",
    )
    if not creds.refresh_token:
        sys.exit("Google didn't return a refresh token. Run the script again.")

    values = {
        "GOOGLE_CLIENT_ID": creds.client_id,
        "GOOGLE_CLIENT_SECRET": creds.client_secret,
        "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
    }
    save_to_env(values)

    print("Success! Saved to .env.\n")
    print("Now add these three variables to Railway (GaryReDeaux service -> Variables):\n")
    for k, v in values.items():
        print(f"  {k}")
        print(f"  {v}\n")


if __name__ == "__main__":
    main()
