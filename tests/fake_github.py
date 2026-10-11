"""An in-memory stand-in for the parts of GitHub the engine uses (git data API,
commit history by path, and the code sign-in), for tests."""
import base64
import datetime
import hashlib
import itertools
import json
import re


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeGitHub:
    TOKEN = "gho_test_token"

    def __init__(self, repo="nige/companion-data"):
        self.repo = repo
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.head = None
        self.polls = 0
        self.ids = itertools.count(1)
        self.race_next_update = False        # simulate someone else committing first

    def __call__(self, method, url, body=None, headers=None, form=False):
        if url.startswith("https://github.com/login/device/code"):
            return 200, {"device_code": "dev1", "user_code": "ABCD-1234",
                         "verification_uri": "https://github.com/login/device", "interval": 0, "expires_in": 30}
        if url.startswith("https://github.com/login/oauth/access_token"):
            self.polls += 1
            if self.polls % 2 == 1:
                return 200, {"error": "authorization_pending"}
            return 200, {"access_token": self.TOKEN, "token_type": "bearer", "scope": "repo"}
        if (headers or {}).get("Authorization") != f"Bearer {self.TOKEN}":
            return 401, {"message": "Bad credentials"}
        pre = f"https://api.github.com/repos/{self.repo}"
        if not url.startswith(pre):
            return 404, {"message": "Not Found"}
        path = url[len(pre):]
        return self.route(method, path, body)

    # -- helpers --
    def tree_files(self, tree_sha):
        return dict(self.trees[tree_sha]) if tree_sha else {}

    def new_tree(self, files):
        sha = hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()
        self.trees[sha] = files
        return sha

    def route(self, method, path, body):
        if method == "GET" and path == "":
            return 200, {"full_name": self.repo, "private": True}
        if method == "GET" and path.startswith("/git/ref/heads/"):
            if not self.head:
                return 409, {"message": "Git Repository is empty."}
            return 200, {"object": {"sha": self.head}}
        m = re.match(r"^/git/commits/(\w+)$", path)
        if method == "GET" and m:
            c = self.commits[m.group(1)]
            return 200, {"sha": m.group(1), "tree": {"sha": c["tree"]}}
        m = re.match(r"^/git/trees/(\w+)", path)
        if method == "GET" and m:
            files = self.tree_files(m.group(1))
            return 200, {"tree": [{"path": p, "sha": s, "type": "blob"} for p, s in files.items()]}
        m = re.match(r"^/git/blobs/(\w+)$", path)
        if method == "GET" and m:
            return 200, {"content": base64.b64encode(self.blobs[m.group(1)]).decode(), "encoding": "base64"}
        if method == "POST" and path == "/git/blobs":
            data = base64.b64decode(body["content"])
            sha = blob_sha(data)
            self.blobs[sha] = data
            return 201, {"sha": sha}
        if method == "POST" and path == "/git/trees":
            files = self.tree_files(body.get("base_tree"))
            for e in body["tree"]:
                if e["sha"] is None:
                    files.pop(e["path"], None)
                else:
                    files[e["path"]] = e["sha"]
            return 201, {"sha": self.new_tree(files)}
        if method == "POST" and path == "/git/commits":
            sha = hashlib.sha1(f"commit{next(self.ids)}".encode()).hexdigest()
            parent = body["parents"][0] if body["parents"] else None
            before = self.tree_files(self.commits[parent]["tree"]) if parent else {}
            after = self.tree_files(body["tree"])
            changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
            self.commits[sha] = {"tree": body["tree"], "parent": parent, "changed": changed,
                                 "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "message": body["message"]}
            return 201, {"sha": sha}
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            if self.race_next_update:                         # someone else got there first
                self.race_next_update = False
                c = self.commits[self.head]
                other = hashlib.sha1(f"other{next(self.ids)}".encode()).hexdigest()
                self.commits[other] = dict(c, parent=self.head, changed=set(), message="someone else")
                self.head = other
                return 422, {"message": "Update is not a fast forward"}
            if self.commits[body["sha"]]["parent"] != self.head:
                return 422, {"message": "Update is not a fast forward"}
            self.head = body["sha"]
            return 200, {"object": {"sha": self.head}}
        if method == "POST" and path == "/git/refs":
            if self.head:
                return 422, {"message": "Reference already exists"}
            self.head = body["sha"]
            return 201, {"object": {"sha": self.head}}
        m = re.match(r"^/commits\?path=([^&]+)", path)
        if method == "GET" and m:
            want, sha = m.group(1), self.head
            while sha:
                c = self.commits[sha]
                if want in c["changed"]:
                    return 200, [{"sha": sha, "commit": {"committer": {"date": c["date"]}}}]
                sha = c["parent"]
            return 200, []
        return 404, {"message": f"fake: no route for {method} {path}"}
