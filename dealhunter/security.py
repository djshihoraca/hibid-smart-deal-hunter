"""Password verification without storing plaintext credentials."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque


class LoginLimiter:
    """Shared across Streamlit sessions in one process, without credential values."""
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = deque()

    def allow(self, username):
        now = time.monotonic()
        identity = hashlib.sha256(username.encode()).digest()
        with self.lock:
            while self.attempts and self.attempts[0][0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 100 or sum(u == identity for _, u in self.attempts) >= 5:
                return False
            self.attempts.append((now, identity))
            return True


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Use a password of at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return "pbkdf2_sha256$600000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 600_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt, validate=True), int(rounds))
        return hmac.compare_digest(actual, base64.b64decode(expected, validate=True))
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    import getpass
    print(hash_password(getpass.getpass("New app password (not your HiBid password): ")))
