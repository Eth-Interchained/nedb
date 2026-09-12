#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
NEDB tri-distribution release tool.

    python3 scripts/release.py "vFROM" "vTO"
    e.g.    python3 scripts/release.py "v2.4.68" "v2.4.468"

What it does, in order:
  1. For each distribution fork (crypto-datab/cryptoDB -> crypto-database,
     nitro-db/aof-DB -> aof-db): bump every version-bearing manifest from FROM
     to TO, open a release/<vTO> PR and merge it.
  2. For the flagship (Eth-Interchained/nedb): bump the same manifests, repoint
     the distributions/* submodules to the freshly-bumped fork masters, open a
     PR and merge it.
  3. Tag vTO on nedb master -> fires CI/CD (release.yml + release-distros.yml
     + Codemagic mac wheels), publishing nedb-engine + crypto-database + aof-db
     aligned on one version across npm / PyPI / crates.io.

Idempotent by design (so a half-finished release can be re-run safely):
  * A manifest line already at TO is left untouched.
  * A repo already fully at TO produces no empty PR -- it is reported and skipped.
  * An already-existing vTO tag is left in place; CI is not re-fired.
The "rest of the script" (submodule repoint, tag) always runs even when the
version was already correct.

Both arguments REQUIRE the leading 'v'. FROM and TO must differ and look like
vMAJOR.MINOR.PATCH (patch may be any integer, e.g. v2.4.468).

Safety:
  * Never force-pushes master and never commits to master directly -- every
    change lands through a branch + PR + merge.
  * The tag goes on the sha the MERGE API returned, never on a fresh read of
    refs/heads/master -- and before the tag ref is created, the commit is
    checked to actually declare TO. Idempotency does not help here: re-running
    leaves an existing wrong tag in place, so the wrong tag has to be
    prevented rather than repaired.
  * After bumping, asserts the load-bearing version fields (npm version, PyPI
    version, the rust workspace + nedb-engine engine crate, and each wrapper's
    nedb-engine path-dep) all equal TO -- catching a stranded straggler (the
    class of bug that previously left the engine crate behind).

Requires env GITHUB_TOKEN (repo + workflow scope). Honors https_proxy if set.
Intended to be run through the `nedb-release` skill so the token is injected:
    RunWithCredentials("nedb-release", 'python3 scripts/release.py "v2.4.68" "v2.4.468"')

(c) Interchained LLC × Vex (Interchained AI fleet: GLM · Claude · Opus · Fable · GPT-6)
"""
import os, sys, re, json, time, base64, subprocess, urllib.request, urllib.error

FLAGSHIP = "Eth-Interchained/nedb"
# (fork repo, distro name) -- distro name == wrapper crate dir == submodule dir == npm/crate name
FORKS = [("crypto-datab/cryptoDB", "crypto-database"),
         ("nitro-db/aof-DB",       "aof-db")]
SUBMODULES = ["distributions/%s" % d for _, d in FORKS]
# version-bearing manifests common to every repo (relative to repo root)
COMMON = ["package.json", "pyproject.toml", "rust/Cargo.toml", "rust/nedb-v2/Cargo.toml",
          "rust/crates/nedb-py/pyproject.toml", "python/nedb/__init__.py",
          "client/node/package.json", "client/python/pyproject.toml",
          "client/python/nedb_client/__init__.py"]
KEYS = ("version", "nedb-engine", "nedb_engine", "nedb-core", "nedb_core")

TOK = os.environ.get("GITHUB_TOKEN")
PROXY = os.environ.get("https_proxy") or ""
WORK = "/tmp/nedb-release"

def scrub(s): return (s or "").replace(TOK, "***") if TOK else (s or "")
def pxy(): return (["-c", "http.proxy=%s" % PROXY] if PROXY else [])
def url_for(repo): return "https://x-access-token:%s@github.com/%s" % (TOK, repo)

def run(*a, check=True):
    r = subprocess.run(a, capture_output=True, text=True)
    print("$", " ".join((x if (not TOK or TOK not in x) else "<url>") for x in a))
    if r.stdout.strip(): print(scrub(r.stdout).strip())
    if r.stderr.strip(): print(scrub(r.stderr).strip())
    if check and r.returncode != 0: print("!! rc=%d" % r.returncode); sys.exit(1)
    return r

def git(repo_dir, *a, check=True):
    return run("git", "-C", repo_dir, *a, check=check)

def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    rq = urllib.request.Request("https://api.github.com" + path, data=data, method=method)
    rq.add_header("Authorization", "Bearer " + TOK)
    rq.add_header("Accept", "application/vnd.github+json")
    rq.add_header("User-Agent", "nedb-release")
    if data: rq.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(rq) as r:
            raw = r.read().decode(); return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(); return e.code, (json.loads(raw) if raw.strip() else {})

# ---- version helpers -------------------------------------------------------
def parse_varg(s, label):
    if not s or not s.startswith("v"):
        sys.exit("!! %s must start with 'v' (e.g. v2.4.68); got %r" % (label, s))
    num = s[1:]
    if not re.fullmatch(r"\d+\.\d+\.\d+", num):
        sys.exit("!! %s must look like vMAJOR.MINOR.PATCH; got %r" % (label, s))
    return num

def bump_tree(root, frm, to):
    """Set version-bearing lines from `frm` to `to`. Idempotent. Returns count changed."""
    rx_from = re.compile(re.escape(frm) + r'(?![\d.])')
    targets = list(COMMON)
    crates = os.path.join(root, "rust", "crates")
    if os.path.isdir(crates):
        for d in sorted(os.listdir(crates)):
            rel = os.path.join("rust", "crates", d, "Cargo.toml")
            if rel not in targets and os.path.exists(os.path.join(root, rel)):
                targets.append(rel)
    changed = 0
    for rel in targets:
        full = os.path.join(root, rel)
        if not os.path.exists(full): continue
        lines = open(full).read().split("\n"); touched = False
        for i, ln in enumerate(lines):
            if any(k in ln.lower() for k in KEYS) and rx_from.search(ln):
                lines[i] = rx_from.sub(to, ln); changed += 1; touched = True
                print("    bump %-42s %s" % (rel, lines[i].strip()[:64]))
        if touched: open(full, "w").write("\n".join(lines))
    return changed

def core_versions(root, distro=None):
    """Extract the load-bearing version fields; returns {label: value-or-None}."""
    out = {}
    pj = os.path.join(root, "package.json")
    if os.path.exists(pj):
        try: out["npm package.json"] = json.load(open(pj)).get("version")
        except Exception: out["npm package.json"] = None
    def first_ver(rel):
        f = os.path.join(root, rel)
        if not os.path.exists(f): return None
        m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', open(f).read())
        return m.group(1) if m else None
    out["pypi pyproject.toml"] = first_ver("pyproject.toml")
    out["rust workspace"] = first_ver("rust/Cargo.toml")
    out["engine nedb-v2"] = first_ver("rust/nedb-v2/Cargo.toml")
    if distro:
        wf = os.path.join(root, "rust", "crates", distro, "Cargo.toml")
        if os.path.exists(wf):
            m = re.search(r'nedb-engine\s*=\s*\{[^}]*version\s*=\s*"([^"]+)"', open(wf).read())
            out["wrapper nedb-engine dep"] = m.group(1) if m else None
    return out

def assert_all_to(root, to, distro=None):
    bad = {k: v for k, v in core_versions(root, distro).items() if v is not None and v != to}
    if bad:
        print("!! core version fields not at %s after bump:" % to)
        for k, v in bad.items(): print("     %-26s = %s" % (k, v))
        sys.exit(1)
    print("  verified: every core version field == %s" % to)

# ---- per-repo release steps ------------------------------------------------
def clone(repo):
    d = os.path.join(WORK, repo.split("/")[1])
    run("rm", "-rf", d)
    run("git", *pxy(), "clone", "--depth", "1", url_for(repo), d)
    return d

def commit_push_branch(repo_dir, repo, branch, message):
    git(repo_dir, "checkout", "-B", branch)
    git(repo_dir, "add", "-A")
    r = subprocess.run(["git", "-C", repo_dir, "-c", "user.email=vex@interchained.org",
                        "-c", "user.name=Vex", "commit", "-q", "-F", "-"],
                       input=message, capture_output=True, text=True)
    print(scrub((r.stdout or "") + (r.stderr or "")) or "(committed)")
    if r.returncode != 0: sys.exit("!! commit failed")
    git(repo_dir, *pxy(), *(["-c", "http.proxyAuthMethod=basic"] if PROXY else []),
        "push", "-q", "-f", url_for(repo), "%s:%s" % (branch, branch))

def open_and_merge_pr(repo, branch, title, body, method="squash"):
    s, pr = api("POST", "/repos/%s/pulls" % repo, {"title": title, "head": branch, "base": "master", "body": body})
    if s in (200, 201): num = pr["number"]; print("  PR #%d %s" % (num, pr["html_url"]))
    else:
        s2, prs = api("GET", "/repos/%s/pulls?head=%s:%s&state=open" % (repo, repo.split("/")[0], branch))
        if isinstance(prs, list) and prs: num = prs[0]["number"]; print("  PR exists #%d" % num)
        else: sys.exit("!! could not open PR: %s %s" % (s, scrub(json.dumps(pr))[:200]))
    for _ in range(8):
        s, info = api("GET", "/repos/%s/pulls/%d" % (repo, num))
        if s == 200 and info.get("mergeable") is True: break
        if s == 200 and info.get("mergeable_state") == "dirty": sys.exit("!! PR #%d is dirty" % num)
        time.sleep(3)
    s, mr = api("PUT", "/repos/%s/pulls/%d/merge" % (repo, num), {"merge_method": method, "commit_title": "%s (#%d)" % (title, num)})
    print("  merge #%d -> HTTP %d merged=%s sha=%s" % (num, s, mr.get("merged"), (mr.get("sha") or "")[:12]))
    if not (s == 200 and mr.get("merged")): sys.exit("!! merge failed: %s" % str(mr)[:160])
    # The merge RESPONSE carries the merge commit sha, and it comes from the
    # write path -- so it is the only answer that cannot be stale. Callers that
    # need to act on "what is master now" must use this and never re-ask.
    return mr.get("sha") or ""

def release_fork(repo, distro, frm, to, vto):
    print("\n== fork %s (%s): %s -> %s ==" % (repo, distro, frm, to))
    d = clone(repo)
    changed = bump_tree(d, frm, to)
    assert_all_to(d, to, distro=distro)
    if changed == 0 and git(d, "status", "--porcelain").stdout.strip() == "":
        print("  already at %s -- no PR needed" % to); return
    commit_push_branch(d, repo, "release/%s" % vto,
        "release: %s %s -- version align\n\nBump %s -> %s across npm/PyPI/crates manifests.\n\nCo-Authored-By: Vex (Interchained AI fleet) <vex@interchained.org>" % (distro, to, frm, to))
    open_and_merge_pr(repo, "release/%s" % vto, "release: %s %s (version align)" % (distro, to),
        "Bump to **%s** across npm/PyPI/crates manifests. Built centrally by nedb." % to)

def repoint_submodules(repo_dir):
    insteadof = "https://x-access-token:%s@github.com/" % TOK
    subprocess.run(["git", "config", "--global", "url.%s.insteadOf" % insteadof, "https://github.com/"],
                   capture_output=True, text=True)
    try:
        git(repo_dir, *pxy(), "submodule", "update", "--init", "--remote", *SUBMODULES)
    finally:
        subprocess.run(["git", "config", "--global", "--unset", "url.%s.insteadOf" % insteadof],
                       capture_output=True, text=True)
    if "x-access-token" in open(os.path.join(repo_dir, ".gitmodules")).read():
        sys.exit("!! token leaked into .gitmodules")

def release_flagship(frm, to, vto):
    print("\n== flagship %s: %s -> %s + repoint submodules ==" % (FLAGSHIP, frm, to))
    d = clone(FLAGSHIP)
    bump_tree(d, frm, to)
    assert_all_to(d, to)
    repoint_submodules(d)
    if git(d, "status", "--porcelain").stdout.strip() == "":
        print("  flagship already at %s and submodules current -- no PR needed" % to); return ""
    print(scrub(git(d, "diff", "--stat", "HEAD").stdout))
    commit_push_branch(d, FLAGSHIP, "release/%s-flagship" % vto,
        "release: NEDB %s -- flagship bump + submodule repoint\n\nFlagship to %s; submodules repointed to the %s fork masters so all three\nproducts ship aligned on %s.\n\nCo-Authored-By: Vex (Interchained AI fleet) <vex@interchained.org>" % (to, to, to, vto))
    return open_and_merge_pr(FLAGSHIP, "release/%s-flagship" % vto, "release: NEDB %s tri-distribution" % vto,
        "Flagship to **%s**; submodules repointed to the %s fork masters. Ships nedb-engine + crypto-database + aof-db aligned on **%s**." % (to, to, vto), method="merge")

def declared_version_at(sha):
    """The version `rust/crates/nedb-py/pyproject.toml` declares AT one commit.

    Read from the tree rather than the working copy, because the question being
    asked is "what will CI build when it checks this sha out" -- and CI checks
    out the tag, not this machine.
    """
    s, c = api("GET", "/repos/%s/contents/rust/crates/nedb-py/pyproject.toml?ref=%s" % (FLAGSHIP, sha))
    if s != 200: return None
    try: txt = base64.b64decode(c.get("content", "")).decode()
    except Exception: return None
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else None

def verify_tag_target(sha, to, vto):
    """Why this commit may carry this version's name. Returns None, or refuses.

    Pure and side-effect free ON PURPOSE: it is the whole safety property, so
    it has to be testable without any code path that can create a tag. An
    earlier version of the test drove `tag_flagship` directly and the "this
    commit is fine" case sailed past the check and POSTed a real tag ref, which
    fired the publish matrix on a junk version. A gate you cannot exercise
    without arming it is not a gate.
    """
    if not sha: return "!! no commit to tag"
    got = declared_version_at(sha)
    if got is None:
        return "!! cannot read rust/crates/nedb-py/pyproject.toml at %s -- refusing to tag blind" % sha[:12]
    if got != to:
        return ("!! REFUSING TO TAG: %s declares version %s, but this release is %s.\n"
                "   Tagging it would publish %s under the name %s on npm/PyPI/crates.\n"
                "   The bump commit is probably one commit ahead -- re-run once master settles."
                % (sha[:12], got, to, got, vto))
    return None

def tag_flagship(vto, to, sha=""):
    s, _ = api("GET", "/repos/%s/git/ref/tags/%s" % (FLAGSHIP, vto))
    if s == 200:
        print("\n== tag %s already exists -- leaving it (CI not re-fired) ==" % vto); return
    if sha:
        # Handed down from the merge response. NEVER re-read refs/heads/master
        # here: that read is served from a replica and is routinely one commit
        # stale a second after a merge. It is how v4.3.0 and v4.3.1 were both
        # tagged onto the PREVIOUS commit -- which still declared the OLD
        # version -- so each tag fired CI and published the wrong number while
        # reporting success. v4.1.1 and v4.2.0 are not evidence the read works;
        # they are two coin flips that landed heads.
        print("  tagging the merge commit reported by the merge API: %s" % sha[:12])
    else:
        s, ref = api("GET", "/repos/%s/git/ref/heads/master" % FLAGSHIP)
        sha = ref.get("object", {}).get("sha", "")
        print("  no merge sha (nothing to bump) -- falling back to refs/heads/master: %s" % sha[:12])
    # The gate. A tag whose commit declares a different version publishes that
    # other version under this name, on every registry, irreversibly. Refusing
    # here costs a re-run; not refusing costs a version that can never be
    # reused. Either answer to "which commit" is checked the same way.
    bad = verify_tag_target(sha, to, vto)
    if bad: sys.exit(bad)
    print("  verified %s declares %s" % (sha[:12], to))
    msg = "NEDB %s -- aligned tri-distribution: nedb-engine + crypto-database + aof-db at one version across npm/PyPI/crates + macOS wheels. (c) Interchained LLC × Vex (Interchained AI fleet: GLM · Claude · Opus · Fable · GPT-6)" % to
    s, to_obj = api("POST", "/repos/%s/git/tags" % FLAGSHIP, {"tag": vto, "message": msg, "object": sha, "type": "commit", "tagger": {"name": "Vex", "email": "vex@interchained.org"}})
    if s not in (200, 201): sys.exit("!! tag object failed HTTP %d %s" % (s, str(to_obj)[:160]))
    s, rf = api("POST", "/repos/%s/git/refs" % FLAGSHIP, {"ref": "refs/tags/%s" % vto, "sha": to_obj.get("sha", "")})
    if s in (200, 201): print("\n== TAGGED %s -> %s | Actions: https://github.com/%s/actions ==" % (vto, sha[:12], FLAGSHIP))
    else: sys.exit("!! tag ref failed HTTP %d %s" % (s, str(rf)[:160]))

def main():
    if not TOK: sys.exit("!! GITHUB_TOKEN not set (run via the nedb-release skill)")
    if len(sys.argv) != 3:
        sys.exit('usage: python3 scripts/release.py "vFROM" "vTO"   e.g.  "v2.4.68" "v2.4.468"')
    vfrm, vto = sys.argv[1], sys.argv[2]
    frm, to = parse_varg(vfrm, "FROM"), parse_varg(vto, "TO")
    if frm == to: sys.exit("!! FROM and TO must differ (%s)" % vfrm)
    print("NEDB tri-distribution release: %s -> %s" % (vfrm, vto))
    os.makedirs(WORK, exist_ok=True)
    for repo, distro in FORKS:
        release_fork(repo, distro, frm, to, vto)
    sha = release_flagship(frm, to, vto)
    tag_flagship(vto, to, sha)
    print("\nDONE %s -> %s" % (vfrm, vto))

if __name__ == "__main__":
    main()
