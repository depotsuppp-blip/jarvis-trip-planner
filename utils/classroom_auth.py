"""
The Classroom sign-in's old name, kept so the command still works:

    python utils/classroom_auth.py           same as  python utils/google_auth.py
    python utils/classroom_auth.py --check   same as  python utils/google_auth.py --check

Jarvis now signs in to Google once for everything it does as the owner - Classroom
(read-only) and Calendar (read and add events) - in utils/google_auth.py, which keeps
one token.json with every scope. See that module for the setup steps.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":  # run as a script: the project folder is not on the path yet
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.google_auth import (  # noqa: E402 - after the path fix above
    SCOPES_BY_SERVICE,
    check,
    client_type,
    credentials_file,
    main,
    sign_in,
    token_file,
)

# The Classroom part of the sign-in (read-only).
SCOPES = SCOPES_BY_SERVICE["classroom"]

__all__ = ["SCOPES", "check", "client_type", "credentials_file", "main", "sign_in", "token_file"]

if __name__ == "__main__":
    sys.exit(main(sys.argv))
