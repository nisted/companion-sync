"""Build step: put the Google sign-in details (from GitHub secrets) into the app."""
import os
with open("_client.py", "w") as f:
    f.write(f"CLIENT_ID = {os.environ['GOOGLE_CLIENT_ID']!r}\n")
    f.write(f"CLIENT_SECRET = {os.environ['GOOGLE_CLIENT_SECRET']!r}\n")
