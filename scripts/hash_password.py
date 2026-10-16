"""Print a password hash for NOESIS_ADMIN_PASSWORD_HASH. The password is read without echo."""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.api.auth import hash_password  # noqa: E402

pw = getpass.getpass("Dashboard password (12+ characters): ")
if len(pw) < 12:
    sys.exit("Use at least 12 characters.")
if pw != getpass.getpass("Repeat: "):
    sys.exit("Passwords did not match.")
print(hash_password(pw))
