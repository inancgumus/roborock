"""A keyring backend that keeps secrets in a file, so the server and the commands share them in tests."""
import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend


class FileKeyring(KeyringBackend):
    priority = 1

    @property
    def file(self):
        return Path(os.environ["TEST_KEYRING_FILE"])

    def load(self):
        return json.loads(self.file.read_text()) if self.file.exists() else {}

    def get_password(self, service, username):
        return self.load().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        secrets = self.load()
        secrets[f"{service}/{username}"] = password
        self.file.write_text(json.dumps(secrets))

    def delete_password(self, service, username):
        secrets = self.load()
        secrets.pop(f"{service}/{username}", None)
        self.file.write_text(json.dumps(secrets))
