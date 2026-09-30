"""Build step: put the Google sign-in details (from GitHub secrets) into the app."""
import os
import sys

cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
if not cid.endswith(".apps.googleusercontent.com") or not secret:
    sys.exit("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET repository secrets are missing or wrong.")
with open("_client.py", "w") as f:
    f.write(f"CLIENT_ID = {cid!r}\n")
    f.write(f"CLIENT_SECRET = {secret!r}\n")
print("Google sign-in details added.")
