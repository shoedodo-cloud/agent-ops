"""Tests for secret-sweep. Hermetic: every repo is built in tmp_path, HOME is redirected,
git config is ignored, and there is no network. Fake tokens are built from pieces so this
file does not contain anything that matches a pattern."""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "bin" / "secret-sweep"

# Fake credentials. None of them is real. Each is long enough to match its pattern.
TOKENS = {
    "anthropic-key": "sk-ant-" + "a" * 40,
    "openai-key": "sk-" + "b" * 48,
    "github-token": "ghp_" + "c" * 36,
    "aws-access-key-id": "AKIA" + "E" * 16,
    "slack-token": "xoxb-" + "1" * 12 + "-" + "d" * 12,
    "google-api-key": "AIza" + "f" * 35,
    "private-key-header": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "generic-api-key": 'api_key = "' + "k9" * 12 + '"',
    "linear-api-key": "lin_api_" + "g" * 40,
    "stripe-live-key": "sk_" + "live_" + "h" * 24,
    "gitlab-token": "glpat-" + "i" * 20,
    "npm-token": "npm_" + "j" * 36,
    "huggingface-token": "hf_" + "k" * 34,
    "pypi-token": "pypi-" + "AgEIcHlwaS5vcmc" + "l" * 60,
    "url-credentials": "https://alice:" + "m" * 12 + "@db.example.com/x",
}
# What must never appear in output for each token.
SECRET_TEXT = dict(TOKENS)
SECRET_TEXT["generic-api-key"] = "k9" * 12
SECRET_TEXT["url-credentials"] = "m" * 12


def load_tool():
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("secret_sweep", str(TOOL))
    spec = importlib.util.spec_from_loader("secret_sweep", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    e = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "alice",
        "GIT_AUTHOR_EMAIL": "alice.example.invalid",
        "GIT_COMMITTER_NAME": "alice",
        "GIT_COMMITTER_EMAIL": "alice.example.invalid",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    monkeypatch.setenv("HOME", e["HOME"])
    monkeypatch.delenv("SWEEP_ROOT", raising=False)
    return e


def git(repo, env, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def make_repo(path, env):
    path.mkdir(parents=True, exist_ok=True)
    git(path, env, "init", "-q")
    return path


def commit_all(repo, env, msg="c"):
    git(repo, env, "add", "-A")
    git(repo, env, "commit", "-q", "-m", msg, "--no-gpg-sign")
    return git(repo, env, "rev-parse", "HEAD")


def sweep(env, *args, extra_env=None):
    e = dict(env)
    e.update(extra_env or {})
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)], env=e,
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def stream(data):
    """A read(n) function over bytes, like the tool uses for files and blobs."""
    return io.BytesIO(data).read


# --- pattern units ----------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(TOKENS))
def test_each_pattern_matches_its_token(name):
    mod = load_tool()
    assert name in mod.scan_stream(stream(TOKENS[name].encode()))


def test_anthropic_key_is_not_reported_as_openai():
    mod = load_tool()
    found = mod.scan_stream(stream(TOKENS["anthropic-key"].encode()))
    assert found == {"anthropic-key"}


def test_token_split_across_a_chunk_boundary_is_found():
    mod = load_tool()
    data = b"x" * (mod.CHUNK - 10) + TOKENS["github-token"].encode() + b"y" * 100
    assert "github-token" in mod.scan_stream(stream(data))


def test_ordinary_text_matches_nothing():
    mod = load_tool()
    text = b"api_key = get_api_key_from_environment()\ntask-queue-" + b"x" * 60 + b"\n"
    assert mod.scan_stream(stream(text)) == set()


# --- tracked-files mode -----------------------------------------------------------

def test_clean_repo_exits_0(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "README.md").write_text("hello\n")
    commit_all(repo, env)
    code, out, err = sweep(env, repo)
    assert code == 0, out + err
    assert "0 hit(s) in 1 repo(s)" in out


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_token_in_tracked_file_exits_1_and_prints_no_token_text(tmp_path, env, name):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "notes.txt").write_text("before\n" + TOKENS[name] + "\nafter\n")
    commit_all(repo, env)
    code, out, err = sweep(env, repo)
    assert code == 1, out + err
    assert name in out and "notes.txt" in out
    for token in SECRET_TEXT.values():
        assert token not in out + err
        assert token[:8] not in out + err        # not even the first characters


@pytest.mark.parametrize("fname", [".env", ".env.production", "server.pem", "id_rsa",
                                   "credentials.json", "my-token-list.txt", "deploy.key", ".npmrc", ".git-credentials",
                                   "id_ecdsa", "vault.kdbx", "service-account-prod.json"])
def test_credential_filename_exits_1(tmp_path, env, fname):
    repo = make_repo(tmp_path / "proj", env)
    (repo / fname).write_text("nothing secret in here\n")
    commit_all(repo, env)
    code, out, _ = sweep(env, repo)
    assert code == 1
    assert "credential-filename" in out and fname in out


def test_env_example_is_exempt_by_name_but_still_scanned_by_content(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / ".env.example").write_text("API_URL=http://localhost\n")
    commit_all(repo, env)
    assert sweep(env, repo)[0] == 0
    (repo / ".env.example").write_text("KEY=" + TOKENS["github-token"] + "\n")
    commit_all(repo, env)
    code, out, _ = sweep(env, repo)
    assert code == 1 and "github-token" in out and "credential-filename" not in out


def test_non_utf8_file_in_worktree_does_not_crash_and_token_inside_is_found(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "blob.bin").write_bytes(b"\xff\xfe\x80\x00" * 50)
    (repo / "mixed.bin").write_bytes(b"\xff\xfe" + TOKENS["aws-access-key-id"].encode() + b"\x80")
    commit_all(repo, env)
    code, out, err = sweep(env, repo)
    assert code == 1 and "Traceback" not in err
    assert "mixed.bin" in out and "blob.bin" not in out


def test_large_file_is_read_to_the_end(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    mod = load_tool()
    (repo / "big.dat").write_bytes(b"0" * (mod.CHUNK * 2 + 5) + TOKENS["slack-token"].encode())
    commit_all(repo, env)
    code, out, _ = sweep(env, repo)
    assert code == 1 and "slack-token" in out


def test_it_does_not_modify_anything(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "a.txt").write_text(TOKENS["openai-key"] + "\n")
    (repo / ".env").write_text("x\n")
    commit_all(repo, env)
    (repo / "dirty.txt").write_text("untracked\n")
    before = {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()}
    status = git(repo, env, "status", "--porcelain")
    head = git(repo, env, "rev-parse", "HEAD")
    sweep(env, repo)
    sweep(env, repo, "--history")
    assert {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()} == before
    assert git(repo, env, "status", "--porcelain") == status
    assert git(repo, env, "rev-parse", "HEAD") == head


# --- history mode -----------------------------------------------------------------

def test_deleted_secret_is_clean_in_worktree_but_found_in_history_with_commit(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "keep.txt").write_text("fine\n")
    commit_all(repo, env, "first")
    (repo / "old.cfg").write_text("token=" + TOKENS["anthropic-key"] + "\n")
    bad = commit_all(repo, env, "adds the secret")
    (repo / "old.cfg").unlink()
    commit_all(repo, env, "removes it")
    assert sweep(env, repo)[0] == 0
    code, out, err = sweep(env, repo, "--history")
    assert code == 1, out + err
    assert "old.cfg" in out and "anthropic-key" in out
    assert bad[:12] in out
    assert TOKENS["anthropic-key"] not in out + err


def test_deleted_credential_filename_is_found_in_history(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / ".env").write_text("A=1\n")
    bad = commit_all(repo, env)
    (repo / ".env").unlink()
    commit_all(repo, env)
    code, out, _ = sweep(env, repo, "--history")
    assert code == 1 and "credential-filename" in out and bad[:12] in out


def test_same_content_under_a_credential_name_is_not_hidden_by_a_twin(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "a-readme.txt").write_text("same bytes\n")
    (repo / ".env").write_text("same bytes\n")
    commit_all(repo, env)
    code, out, _ = sweep(env, repo, "--history")
    assert code == 1 and ".env" in out


def test_non_utf8_blob_in_history_does_not_crash(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "image.bin").write_bytes(bytes(range(256)) * 40)
    (repo / "latin1.txt").write_bytes("caf\xe9 \xff\xfe".encode("latin-1"))
    commit_all(repo, env)
    code, out, err = sweep(env, repo, "--history")
    assert code == 0, out + err
    assert "Traceback" not in err and err == ""


def test_token_inside_non_utf8_blob_in_history_is_found(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "dump.bin").write_bytes(b"\xff\xfe\x00" + TOKENS["github-token"].encode() + b"\x80\x81")
    commit_all(repo, env)
    code, out, _ = sweep(env, repo, "--history")
    assert code == 1 and "github-token" in out and "dump.bin" in out
    assert TOKENS["github-token"] not in out


def test_history_covers_other_branches_and_reports_each_file_once(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "base.txt").write_text("base\n")
    commit_all(repo, env)
    git(repo, env, "checkout", "-q", "-b", "side")
    for i in range(3):
        (repo / "side.txt").write_text("v%d " % i + TOKENS["aws-access-key-id"] + "\n")
        commit_all(repo, env, "v%d" % i)
    git(repo, env, "checkout", "-q", "-")
    code, out, _ = sweep(env, repo, "--history", "--json")
    data = json.loads(out)
    assert code == 1
    assert [h["path"] for h in data["hits"]] == ["side.txt"]


def test_repo_with_no_tracked_files_is_not_a_clean_pass_in_either_mode(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    for extra in ((), ("--history",)):
        code, out, err = sweep(env, repo, *extra)
        assert code == 2, out + err
        assert "0 files scanned, nothing to conclude" in out
        assert "0 files scanned, nothing to conclude" in err
        assert "0 hit(s) in 1 repo(s) scanned, 0 file(s)" in out
    code, out, _ = sweep(env, repo, "--json")
    data = json.loads(out)
    assert code == 2 and data["exit"] == 2 and data["nothing_scanned"] is True
    assert data["files_scanned"] == 0 and data["repo_files"] == {"proj": 0}


def test_an_empty_repo_next_to_a_scanned_one_is_named_but_does_not_fail_the_run(tmp_path, env):
    root = tmp_path / "code"
    full = make_repo(root / "full", env)
    make_repo(root / "empty", env)
    (full / "a.txt").write_text("fine\n")
    (full / "b.txt").write_text("also fine\n")
    commit_all(full, env)
    code, out, err = sweep(env, root)
    assert code == 0, out + err
    assert "full: 2 file(s)" in out and "empty: 0 files scanned, nothing to conclude" in out
    assert "0 hit(s) in 2 repo(s) scanned, 2 file(s)" in out
    code, out, _ = sweep(env, root, "--json")
    data = json.loads(out)
    assert code == 0 and data["files_scanned"] == 2 and data["nothing_scanned"] is False
    assert data["repo_files"] == {"full": 2, "empty": 0}


def test_history_mode_counts_each_file_version_once(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "a.txt").write_text("one\n")
    commit_all(repo, env)
    (repo / "a.txt").write_text("two\n")
    commit_all(repo, env)
    code, out, _ = sweep(env, repo, "--history", "--json")
    assert code == 0 and json.loads(out)["files_scanned"] == 2


# --- roots, errors, json ----------------------------------------------------------

def test_root_with_several_repos_names_the_right_one(tmp_path, env):
    root = tmp_path / "code"
    good = make_repo(root / "good", env)
    bad = make_repo(root / "group" / "bad", env)
    (good / "a.txt").write_text("fine\n")
    (bad / "b.txt").write_text(TOKENS["google-api-key"] + "\n")
    commit_all(good, env)
    commit_all(bad, env)
    code, out, _ = sweep(env, root)
    assert code == 1
    assert "2 repo(s)" in out
    assert os.path.join("group", "bad") in out and "good  (" not in out


def test_root_from_environment_and_argument_overrides_it(tmp_path, env):
    clean = make_repo(tmp_path / "clean", env)
    dirty = make_repo(tmp_path / "dirty", env)
    (clean / "a.txt").write_text("fine\n")
    (dirty / ".env").write_text("x\n")
    commit_all(clean, env)
    commit_all(dirty, env)
    assert sweep(env, extra_env={"SWEEP_ROOT": str(clean)})[0] == 0
    assert sweep(env, dirty, extra_env={"SWEEP_ROOT": str(clean)})[0] == 1


def test_no_root_is_an_error(env):
    code, out, err = sweep(env)
    assert code == 2 and "SWEEP_ROOT" in err


def test_missing_root_is_an_error(tmp_path, env):
    code, _, err = sweep(env, tmp_path / "nope")
    assert code == 2 and "not a directory" in err


def test_root_without_repos_is_an_error_not_a_clean_pass(tmp_path, env):
    (tmp_path / "plain").mkdir()
    code, _, err = sweep(env, tmp_path / "plain")
    assert code == 2 and "no git repositories" in err


def test_unscannable_repo_is_an_error_even_if_another_repo_is_clean(tmp_path, env):
    root = tmp_path / "code"
    good = make_repo(root / "good", env)
    (good / "a.txt").write_text("fine\n")
    commit_all(good, env)
    (root / "broken" / ".git").mkdir(parents=True)     # looks like a repo, is not one
    code, out, err = sweep(env, root)
    assert code == 2
    assert "broken" in err and "could not be scanned" in out


def test_error_wins_over_hits_but_hits_are_still_printed(tmp_path, env):
    root = tmp_path / "code"
    bad = make_repo(root / "bad", env)
    (bad / ".env").write_text("x\n")
    commit_all(bad, env)
    (root / "broken" / ".git").mkdir(parents=True)
    code, out, _ = sweep(env, root)
    assert code == 2 and ".env" in out


def test_json_is_valid_for_clean_hits_and_errors(tmp_path, env):
    clean = make_repo(tmp_path / "clean", env)
    (clean / "a.txt").write_text("fine\n")
    commit_all(clean, env)
    code, out, _ = sweep(env, clean, "--json")
    data = json.loads(out)
    assert code == 0 and data["hits"] == [] and data["errors"] == [] and data["exit"] == 0

    dirty = make_repo(tmp_path / "dirty", env)
    (dirty / "k.txt").write_text(TOKENS["private-key-header"] + "\n")
    (dirty / ".env").write_text("x\n")
    commit_all(dirty, env)
    code, out, err = sweep(env, dirty, "--json", "--history")
    data = json.loads(out)
    assert code == 1 and data["exit"] == 1 and data["mode"] == "history"
    assert {h["pattern"] for h in data["hits"]} == {"private-key-header", "credential-filename"}
    for h in data["hits"]:
        assert set(h) == {"repo", "path", "pattern", "commit"} and len(h["commit"]) == 40
    assert TOKENS["private-key-header"] not in out + err

    code, out, _ = sweep(env, tmp_path / "missing", "--json")
    data = json.loads(out)
    assert code == 2 and data["exit"] == 2 and data["errors"][0]["repo"] is None


def test_help_documents_exit_codes_and_root(env):
    code, out, _ = sweep(env, "--help")
    assert code == 0
    for word in ("SWEEP_ROOT", "--history", "--json", "exit codes"):
        assert word in out


def test_control_characters_in_file_names_are_not_printed_raw(tmp_path, env):
    repo = make_repo(tmp_path / "proj", env)
    (repo / "bad\x1b[31mname.pem").write_text("x\n")
    commit_all(repo, env)
    code, out, _ = sweep(env, repo)
    assert code == 1 and "\x1b" not in out
