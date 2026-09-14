#!/usr/bin/env python3
"""Derive a stable Ed25519 SSH deploy key from the DigitalOcean API token.

The GitHub Actions deploy workflow needs SSH access to the server on every run (first install and
later updates) without storing a private key anywhere. Deriving the key from the DO_TOKEN secret
gives the same key on every run, and grants nothing the token holder does not already have: whoever
holds DO_TOKEN can already destroy or rebuild the server. Rotating the token rotates the key; the
workflow then registers the new public key and the next run re-adds it to the server.

Usage: derive_ssh_key.py <out_dir>   (reads DO_TOKEN from the environment; writes id_ed25519 + .pub)
Prints two lines: the OpenSSH public key, then its MD5 fingerprint as DigitalOcean reports it.
"""

import base64
import hashlib
import hmac
import os
import stat
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    token = os.environ.get("DO_TOKEN", "")
    if len(token) < 32:
        print("DO_TOKEN is missing or too short", file=sys.stderr)
        return 1
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    seed = hmac.new(token.encode(), b"ssa-corpus deploy ssh key v1", hashlib.sha256).digest()
    key = Ed25519PrivateKey.from_private_bytes(seed)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    priv_path = os.path.join(out, "id_ed25519")
    with open(priv_path, "wb") as fh:
        fh.write(priv)
    os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)
    with open(priv_path + ".pub", "wb") as fh:
        fh.write(pub + b" ssa-corpus-deploy\n")
    blob = base64.b64decode(pub.split()[1])
    fingerprint = ":".join(f"{b:02x}" for b in hashlib.md5(blob).digest())
    print(pub.decode())
    print(fingerprint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
