"""
google_auth_setup.py - One-time Google login so Gary can use your calendar and email.

Run this ONCE on your own computer (not on Railway):
    python google_auth_setup.py           # calendar + email, one Google account
    python google_auth_setup.py --gmail   # email only, from a DIFFERENT account

It needs the OAuth client ID/secret: either already in .env from a previous run,
or the client file you downloaded from Google Cloud sitting in this folder
(Google names it client_secret_....json -- no need to rename it).
A browser window opens, you approve access, and this script:
  1. saves the values to .env
  2. prints them so you can paste them into Railway

With --gmail, only GOOGLE_GMAIL_REFRESH_TOKEN is saved. Gary then reads and
drafts email in that account and keeps using the first account's calendar.

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
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
SCOPES = ["https://www.googleapis.com/auth/calendar"] + GMAIL_SCOPES


def read_env() -> dict:
    env_path = HERE / ".env"
    if not env_path.exists():
        return {}
    pairs = (l.split("=", 1) for l in env_path.read_text(encoding="utf-8").splitlines() if "=" in l)
    return {k.strip(): v.strip().strip('"').strip("'") for k, v in pairs}


def find_client_file() -> Path | None:
    candidates = list(HERE.glob("client_secret*.json")) + list(HERE.glob("google_credentials.json"))
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if len(candidates) > 1:
        print(f"Found {len(candidates)} client files, using the newest: {newest.name}")
    return newest


def make_flow(scopes: list) -> InstalledAppFlow:
    client_file = find_client_file()
    if client_file:
        print(f"Using {client_file.name}")
        return InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
    env = read_env()
    if env.get("GOOGLE_CLIENT_ID") and env.get("GOOGLE_CLIENT_SECRET"):
        print("Using the Google client ID/secret already saved in .env")
        return InstalledAppFlow.from_client_config({"installed": {
            "client_id": env["GOOGLE_CLIENT_ID"],
            "client_secret": env["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}, scopes)
    sys.exit(
        "Couldn't find your Google client file.\n"
        "Download it from Google Cloud (Clients -> your client -> Download JSON)\n"
        f"and put it in this folder:\n    {HERE}"
    )


def save_to_env(values: dict):
    env_path = HERE / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    gmail_only = "--gmail" in sys.argv[1:]
    flow = make_flow(GMAIL_SCOPES if gmail_only else SCOPES)
    if gmail_only:
        print("Opening your browser -- sign in with the Google account whose EMAIL Gary should read...\n")
    else:
        print("Opening your browser -- sign in with the Google account Gary should manage...\n")

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

    if gmail_only:
        values = {"GOOGLE_GMAIL_REFRESH_TOKEN": creds.refresh_token}
    else:
        values = {
            "GOOGLE_CLIENT_ID": creds.client_id,
            "GOOGLE_CLIENT_SECRET": creds.client_secret,
            "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
        }
    save_to_env(values)

    print("Success! Saved to .env.\n")
    print(f"Now add {'this variable' if len(values) == 1 else 'these variables'} to Railway "
          "(GaryReDeaux service -> Variables):\n")
    for k, v in values.items():
        print(f"  {k}")
        print(f"  {v}\n")


if __name__ == "__main__":
    main()
