#!/usr/bin/env python3
"""Print a salted hash; never accept a plaintext password as a command argument."""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from services.password_auth import hash_password


if __name__ == "__main__":
    password = getpass.getpass("New password (at least 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        sys.exit("Passwords do not match")
    try:
        print("LOGIN_PASSWORD_HASH=" + hash_password(password))
    except ValueError as error:
        sys.exit(str(error))
