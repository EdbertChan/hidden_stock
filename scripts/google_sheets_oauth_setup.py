#!/usr/bin/env python3
"""One-time Google OAuth so exports can create *new* Sheets in your Drive.

Service accounts have 0 Drive quota and cannot create files. After this setup,
each export creates a timestamped spreadsheet owned by you.

Steps:
  1. GCP Console → APIs & Services → Credentials → Create OAuth client ID
     (Desktop app). Download JSON.
  2. Save it e.g. ~/.config/hidden_stock/google_oauth_client.json
  3. Run this script (browser login once).
  4. Put paths in .env (printed at end).

  python scripts/google_sheets_oauth_setup.py \\
    --client-secrets ~/.config/hidden_stock/google_oauth_client.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_TOKEN = Path.home() / ".config" / "hidden_stock" / "google_sheets_token.json"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--client-secrets",
        required=True,
        help="OAuth Desktop client secrets JSON from Google Cloud Console",
    )
    p.add_argument(
        "--token",
        default=str(DEFAULT_TOKEN),
        help=f"Where to write authorized_user token (default: {DEFAULT_TOKEN})",
    )
    args = p.parse_args()

    secrets = Path(args.client_secrets).expanduser()
    token = Path(args.token).expanduser()
    if not secrets.is_file():
        print(f"missing client secrets: {secrets}", file=sys.stderr)
        return 1

    token.parent.mkdir(parents=True, exist_ok=True)
    if token.exists():
        token.unlink()

    import gspread

    # Opens browser; writes authorized_user_filename on success.
    gc = gspread.oauth(
        credentials_filename=str(secrets),
        authorized_user_filename=str(token),
    )
    # Smoke: create + trash a tiny sheet to prove Drive write works.
    sh = gc.create("hidden_stock oauth probe (delete me)")
    url = f"https://docs.google.com/spreadsheets/d/{sh.id}"
    print(f"ok — created probe sheet: {url}")
    try:
        gc.del_spreadsheet(sh.id)
        print("probe sheet deleted")
    except Exception as e:
        print(f"leave probe sheet; delete manually ({e})", file=sys.stderr)

    print(
        "\nAdd to .env:\n"
        f"GOOGLE_SHEETS_OAUTH_CLIENT_SECRETS={secrets}\n"
        f"GOOGLE_SHEETS_OAUTH_TOKEN={token}\n"
        "GOOGLE_SHEETS_CREATE_NEW=1\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
