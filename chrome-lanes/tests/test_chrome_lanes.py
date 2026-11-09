"""Tests for bin/chrome-lanes.

Hermetic: every test gets its own HOME, CHROME_LANES_HOME and CHROME_LANES_PROFILE_ROOT under
tmp_path. Nothing reads the real Chrome profile dir, and any ssh call fails the test unless the
test replaced remote_call on purpose. The LevelDB, snappy and WAL parsers run on hand-built bytes.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import stat
import struct
from datetime import datetime, timedelta, timezone

import pytest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "bin" / "chrome-lanes"
_loader = importlib.machinery.SourceFileLoader("chrome_lanes", str(MODULE_PATH))
_spec = importlib.util.spec_from_loader("chrome_lanes", _loader)
cl = importlib.util.module_from_spec(_spec)
_loader.exec_module(cl)
REAL_REMOTE_CALL = cl.remote_call


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CHROME_LANES_HOME", str(tmp_path / "lanes"))
    monkeypatch.setenv("CHROME_LANES_PROFILE_ROOT", str(tmp_path / "chrome"))
    monkeypatch.setenv("CHROME_LANES_MACHINE", "host-a")
    for var in ("CHROME_LANES_HOSTS", "CHROME_LANES_STALE_MINUTES", "CHROME_LANES_EXTENSION_ID",
                "CHROME_LANES_CHROME_BIN", "CLAUDE_PID", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    cl._noted.clear()

    def no_ssh(name, args):
        raise AssertionError(f"unexpected remote call to {name}: {args}")

    monkeypatch.setattr(cl, "remote_call", no_ssh)
    return tmp_path


def leases(tmp_path):
    return tmp_path / "lanes" / "leases"


def now_iso(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def dead_pid_or_skip():
    pid = 2 ** 22 - 1
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    except PermissionError:
        pytest.skip("cannot determine liveness of a made-up pid here")
    pytest.skip(f"pid {pid} unexpectedly exists here")


# ---------- helpers to build LevelDB fixtures ----------

def encode_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def build_wal_batch(seq, ops):
    """ops: list of (type, key_bytes, value_bytes_or_None). Returns one write-batch payload."""
    body = bytearray(struct.pack("<Q", seq) + struct.pack("<I", len(ops)))
    for t, key, val in ops:
        body.append(t)
        body += encode_varint(len(key)) + key
        if t == 1:
            body += encode_varint(len(val)) + val
    return bytes(body)


def wrap_full_record(payload):
    """One 7-byte LevelDB log header (crc + length + type=1 full) followed by payload."""
    return b"\x00\x00\x00\x00" + struct.pack("<H", len(payload)) + bytes([1]) + payload


# ---------- snappy, block, log, latest_values ----------

def test_snappy_decompress_literal_copy1_copy2():
    literal = bytes([((4 - 1) << 2) | 0]) + b"abcd"
    copy1 = bytes([1]) + bytes([4])                      # length 4, offset 4
    copy2 = bytes([(4 << 2) | 2]) + struct.pack("<H", 8)  # length 5, offset 8
    expected = "abcdabcdabcda"
    stream = encode_varint(len(expected)) + literal + copy1 + copy2
    assert cl.snappy_decompress(stream) == expected.encode()


def test_snappy_decompress_bad_offset_raises():
    with pytest.raises(ValueError):
        cl.snappy_decompress(bytes([1, 0b00000001, 5]))


def test_block_entries_prefix_compressed():
    entry1 = encode_varint(0) + encode_varint(4) + encode_varint(6) + b"key1" + b"value1"
    entry2 = encode_varint(3) + encode_varint(1) + encode_varint(6) + b"2" + b"value2"
    data = entry1 + entry2 + struct.pack("<I", 0) + struct.pack("<I", 1)
    assert list(cl.block_entries(data)) == [(b"key1", b"value1"), (b"key2", b"value2")]


def test_log_entries_full_record_put_and_delete():
    raw = wrap_full_record(build_wal_batch(100, [(1, b"k1", b"v1"), (0, b"k1", None)]))
    assert list(cl.log_entries(raw)) == [(b"k1", 100, 1, b"v1"), (b"k1", 101, 0, b"")]


def test_latest_values_json_decode_and_delete_wins(tmp_path):
    d = tmp_path / "ext-storage"
    d.mkdir()
    raw = (wrap_full_record(build_wal_batch(1, [(1, b"bridgeDisplayName", json.dumps("MyLabel").encode())]))
           + wrap_full_record(build_wal_batch(10, [(1, b"bridgeDeviceId", json.dumps("abc-123").encode())]))
           + wrap_full_record(build_wal_batch(11, [(0, b"bridgeDeviceId", None)])))
    (d / "000003.log").write_bytes(raw)
    out = cl.latest_values(str(d), {b"bridgeDeviceId", b"bridgeDisplayName"})
    assert out[b"bridgeDisplayName"] == "MyLabel"
    assert out[b"bridgeDeviceId"] is None              # the later delete beat the earlier put


def test_latest_values_skips_unreadable_files(tmp_path):
    d = tmp_path / "ext-storage"
    d.mkdir()
    (d / "garbage.ldb").write_bytes(b"not a real ldb file")
    assert cl.latest_values(str(d), {b"bridgeDeviceId"})[b"bridgeDeviceId"] is None


# ---------- lease claim / release / status ----------

FAKE_ROW = {"deviceId": "11111111-1111-1111-1111-111111111111", "profileDir": "Profile 1",
            "profileName": "Alice Personal", "account": "alice@example.com", "label": "alice-desk"}
ROW_B = {"deviceId": "22222222-2222-2222-2222-222222222222", "profileDir": "Profile 2",
         "profileName": "Bob Work", "account": "bob@example.com", "label": None}
REMOTE_ROW = {"deviceId": "33333333-3333-3333-3333-333333333333", "profileDir": "Default",
              "profileName": "Host B Chrome", "account": "alice@example.com", "label": "host-b main",
              "machine": "host-b"}


def _setup_lease_env(tmp_path, monkeypatch, rows=(FAKE_ROW,)):
    monkeypatch.setattr(cl, "load_rows", lambda cached: [dict(r) for r in rows])
    monkeypatch.setattr(cl, "scan", lambda: [dict(r) for r in rows])
    d = leases(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_claim_fresh_writes_lease(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    assert cl.cmd_claim(["Alice Personal", "--pid", "4242", "--project", "testproj"]) == 0
    assert "claimed: Alice Personal" in capsys.readouterr().out
    lease = json.loads((d / f"{FAKE_ROW['deviceId']}.json").read_text())
    assert lease["pid"] == 4242 and lease["project"] == "testproj"
    assert lease["profileName"] == "Alice Personal" and lease["machine"] == "host-a"


def test_claim_held_by_live_other_pid(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()                                   # alive: it is this test process
    f = d / f"{FAKE_ROW['deviceId']}.json"
    _write(f, {"deviceId": FAKE_ROW["deviceId"], "profileName": "Alice Personal", "pid": other,
               "project": "otherproj", "since": "2026-01-01T00:00:00+00:00", "last_used": now_iso()})
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1)]) == 4
    err = capsys.readouterr().err
    assert "HELD:" in err and f"pid {other}" in err and "Queue for it" in err
    assert json.loads(f.read_text())["project"] == "otherproj"


def test_claim_dead_pid_replaced_silently(tmp_path, monkeypatch, capsys):
    dead = dead_pid_or_skip()
    d = _setup_lease_env(tmp_path, monkeypatch)
    f = d / f"{FAKE_ROW['deviceId']}.json"
    _write(f, {"deviceId": FAKE_ROW["deviceId"], "profileName": "Alice Personal", "pid": dead,
               "project": "stale", "since": "2026-01-01T00:00:00+00:00"})
    assert cl.cmd_claim(["Alice Personal", "--pid", "555"]) == 0
    lease = json.loads(f.read_text())
    assert lease["pid"] == 555 and lease["project"] != "stale"


def test_release_all_by_pid(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    _write(d / "device-a.json", {"deviceId": "device-a", "profileName": "Mine", "pid": 777,
                                 "project": "p", "since": "2026-01-01T00:00:00+00:00"})
    _write(d / "device-b.json", {"deviceId": "device-b", "profileName": "NotMine", "pid": 999,
                                 "project": "p", "since": "2026-01-01T00:00:00+00:00"})
    assert cl.cmd_release(["--all", "--pid", "777"]) == 0
    assert "released: Mine" in capsys.readouterr().out
    assert not (d / "device-a.json").exists() and (d / "device-b.json").exists()


def test_status_clears_dead_lease(tmp_path, monkeypatch, capsys):
    dead = dead_pid_or_skip()
    d = _setup_lease_env(tmp_path, monkeypatch)
    _write(d / "device-c.json", {"deviceId": "device-c", "profileName": "Stale", "pid": dead,
                                 "project": "p", "since": "2026-01-01T00:00:00+00:00"})
    assert cl.cmd_status([]) == 0
    assert "cleared dead lease: Stale" in capsys.readouterr().out
    assert not (d / "device-c.json").exists()


def test_status_lists_live_lease(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    me = os.getpid()
    _write(d / "device-d.json", {"deviceId": "device-d", "profileName": "Live", "pid": me,
                                 "project": "p", "since": "2026-01-01T00:00:00+00:00"})
    assert cl.cmd_status([]) == 0
    assert f"Live device-d pid {me} alive" in capsys.readouterr().out
    assert (d / "device-d.json").exists()


# ---------- load_rows, snapshot ----------

def test_load_rows_cached_missing_raises_no_snapshot():
    with pytest.raises(cl.NoSnapshot):
        cl.load_rows(cached=True)


def test_load_rows_cached_reads_snapshot(tmp_path):
    _write(tmp_path / "lanes" / "snapshot.json", {"machine": "host-a", "written": "now", "browsers": [FAKE_ROW]})
    assert cl.load_rows(cached=True) == [dict(FAKE_ROW, machine="host-a")]


def test_write_snapshot_atomic(tmp_path):
    data = cl.write_snapshot([FAKE_ROW])
    snap = tmp_path / "lanes" / "snapshot.json"
    on_disk = json.loads(snap.read_text())
    assert on_disk["browsers"] == [dict(FAKE_ROW, machine="host-a")]
    assert on_disk["machine"] == data["machine"] == "host-a"
    assert [p.name for p in snap.parent.iterdir()] == ["snapshot.json"]     # no temp files left


def test_write_snapshot_stays_under_home_and_is_private(tmp_path):
    before = {p for p in tmp_path.rglob("*")}
    cl.write_snapshot([FAKE_ROW])
    new = {p for p in tmp_path.rglob("*")} - before
    assert new and all(str(p).startswith(str(tmp_path / "lanes")) for p in new)
    assert stat.S_IMODE((tmp_path / "lanes" / "snapshot.json").stat().st_mode) == 0o600


def test_load_rows_cached_merges_peer_snapshots(tmp_path, monkeypatch):
    """With hosts configured, the local snapshot outranks any peer copy of the same id, a peer file
    named after this machine is ignored, and an unreadable peer file is skipped with a note."""
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-b=ssh-b.example.com")
    lanes = tmp_path / "lanes"
    _write(lanes / "snapshot.json", {"machine": "host-a", "written": "t", "browsers": [
        {"deviceId": "aaaa", "profileDir": "Profile 2", "profileName": "Alice", "account": "a@example.com", "label": "a"}]})
    _write(lanes / "peers" / "host-a.json", {"machine": "host-a", "written": "old", "browsers": [
        {"deviceId": "aaaa", "profileDir": "Profile 2", "profileName": "OLD NAME", "account": "", "label": "old"}]})
    _write(lanes / "peers" / "host-b.json", {"machine": "host-b", "written": "t", "browsers": [
        {"deviceId": "bbbb", "profileDir": "Default", "profileName": "Bob", "account": "b@example.com", "label": "host-b"},
        {"deviceId": "aaaa", "profileDir": "X", "profileName": "duplicate", "account": "", "label": ""}]})
    (lanes / "peers" / "broken.json").write_text("{not json")
    rows = cl.load_rows(cached=True)
    by_id = {r["deviceId"]: r for r in rows}
    assert by_id["aaaa"]["profileName"] == "Alice" and by_id["aaaa"]["machine"] == "host-a"
    assert by_id["bbbb"]["machine"] == "host-b" and by_id["bbbb"]["label"] == "host-b"
    assert len(rows) == 2


def test_peer_snapshots_ignored_when_hosts_unset(tmp_path):
    lanes = tmp_path / "lanes"
    _write(lanes / "snapshot.json", {"machine": "host-a", "written": "t", "browsers": [FAKE_ROW]})
    _write(lanes / "peers" / "host-b.json", {"machine": "host-b", "written": "t", "browsers": [REMOTE_ROW]})
    assert [r["deviceId"] for r in cl.load_rows(cached=True)] == [FAKE_ROW["deviceId"]]


# ---------- several machines (optional block) ----------

def _fake_remote(monkeypatch, result=(0, "ok")):
    calls = []

    def fake(name, args):
        calls.append((name, args))
        return result

    monkeypatch.setattr(cl, "remote_call", fake)
    return calls


def test_claim_on_remote_row_places_remote_lease(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-b=ssh-b.example.com")
    d = _setup_lease_env(tmp_path, monkeypatch, rows=(REMOTE_ROW,))
    calls = _fake_remote(monkeypatch)
    assert cl.cmd_claim(["Host B Chrome", "--pid", "4242", "--project", "testproj"]) == 0
    assert "remote lease placed on host-b" in capsys.readouterr().out
    assert len(calls) == 1
    name, args = calls[0]
    assert name == "host-b" and args[0] == "lease-put"
    placed = json.loads(args[1])
    assert placed["deviceId"] == REMOTE_ROW["deviceId"]
    assert placed["machine"] == "host-a"           # the claiming session's machine, not the profile's
    assert json.loads((d / f"{REMOTE_ROW['deviceId']}.json").read_text())["remote_machine"] == "host-b"


def test_claim_on_remote_row_ssh_failure_still_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-b=ssh-b.example.com")
    d = _setup_lease_env(tmp_path, monkeypatch, rows=(REMOTE_ROW,))
    _fake_remote(monkeypatch, result=(255, "ssh: timeout"))
    assert cl.cmd_claim(["Host B Chrome", "--pid", "4242"]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "ssh: timeout" in err
    assert "remote_machine" not in json.loads((d / f"{REMOTE_ROW['deviceId']}.json").read_text())


def test_claim_on_remote_row_without_hosts_never_uses_ssh(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch, rows=(REMOTE_ROW,))
    assert cl.cmd_claim(["Host B Chrome", "--pid", "4242"]) == 0      # the autouse no_ssh would raise
    assert "no entry in CHROME_LANES_HOSTS" in capsys.readouterr().err
    assert (d / f"{REMOTE_ROW['deviceId']}.json").exists()


def test_release_all_deletes_remote_copy(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    f = d / f"{REMOTE_ROW['deviceId']}.json"
    _write(f, {"deviceId": REMOTE_ROW["deviceId"], "profileName": "Host B Chrome", "pid": 777,
               "project": "p", "since": "2026-01-01T00:00:00+00:00", "machine": "host-a",
               "expires": "2099-01-01T00:00:00+00:00", "remote_machine": "host-b"})
    calls = _fake_remote(monkeypatch)
    assert cl.cmd_release(["--all", "--pid", "777"]) == 0
    assert not f.exists()
    assert calls == [("host-b", ["lease-del", REMOTE_ROW["deviceId"]])]


def test_lease_alive_foreign_machine_expired_and_future():
    assert cl.lease_alive({"machine": "host-b", "expires": "2000-01-01T00:00:00+00:00", "pid": 1}) is False
    assert cl.lease_alive({"machine": "host-b", "expires": "2099-01-01T00:00:00+00:00", "pid": 1}) is True


def test_lease_put_and_lease_del_round_trip(tmp_path, capsys):
    lease = {"deviceId": "device-x", "profileName": "Remote Profile", "pid": 1, "project": "p",
             "since": "2026-01-01T00:00:00+00:00", "machine": "host-b", "expires": "2099-01-01T00:00:00+00:00"}
    assert cl.main(["lease-put", json.dumps(lease)]) == 0
    assert "lease-put: Remote Profile" in capsys.readouterr().out
    f = leases(tmp_path) / "device-x.json"
    assert json.loads(f.read_text()) == lease
    assert cl.main(["lease-del", "device-x"]) == 0
    assert "lease-del: device-x" in capsys.readouterr().out and not f.exists()
    assert cl.main(["lease-del", "device-x"]) == 0                # no file: still 0, and says so
    assert "lease-del: none" in capsys.readouterr().out


def test_lease_put_rejects_unsafe_device_id(tmp_path, capsys):
    assert cl.main(["lease-put", json.dumps({"deviceId": "../escape", "pid": 1})]) == 2
    assert "unsafe device id" in capsys.readouterr().err
    assert not (tmp_path / "lanes" / "escape.json").exists()


def test_lease_alive_old_format_uses_pid_liveness():
    dead = dead_pid_or_skip()
    assert cl.lease_alive({"pid": os.getpid()}) is True       # no "machine" key at all
    assert cl.lease_alive({"pid": dead}) is False


def test_remote_call_shell_quotes_each_arg_and_names_the_machine(monkeypatch):
    """ssh gives the remote shell one string. Every argument must arrive shell-quoted, and the
    remote end must run under the configured machine name."""
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-b=ssh-b.example.com")
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, "lease-put: x", ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return Proc()

    monkeypatch.setattr(cl.subprocess, "run", fake_run)
    payload = json.dumps({"deviceId": "d", "profileName": "P Q"})
    assert REAL_REMOTE_CALL("host-b", ["lease-put", payload]) == (0, "lease-put: x")
    assert "ssh-b.example.com" in seen["cmd"] and "BatchMode=yes" in seen["cmd"]
    remote = seen["cmd"][-1]
    assert remote.startswith("CHROME_LANES_MACHINE=host-b python3 - lease-put ")
    assert remote.endswith("'" + payload + "'")       # single-quoted, braces and spaces intact
    assert "def remote_call" in seen["input"]         # the script's own source goes on stdin


def test_remote_call_unknown_machine_is_an_error_without_ssh(monkeypatch):
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: pytest.fail("ssh must not run"))
    rc, out = REAL_REMOTE_CALL("host-c", ["lease-del", "x"])
    assert rc == 1 and "no ssh host" in out


def test_hosts_map_parses_and_ignores_bad_entries(monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_HOSTS", " host-a = a.example.com ,host-b=bob@b.example.com,broken,evil=-oProxyCommand=x,")
    assert cl.hosts_map() == {"host-a": "a.example.com", "host-b": "bob@b.example.com"}
    err = capsys.readouterr().err
    assert "'broken'" in err and "evil" in err


def test_snapshot_peers_fetches_and_merges(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-a=a.example.com,host-b=b.example.com")
    monkeypatch.setattr(cl, "scan", lambda: [dict(FAKE_ROW)])
    peer = [{"deviceId": "bbbb", "profileDir": "Default", "profileName": "Bob", "account": "", "label": "b"}]
    calls = _fake_remote(monkeypatch, result=(0, json.dumps(peer)))
    assert cl.main(["snapshot", "--peers"]) == 0
    assert calls == [("host-b", ["list", "--json"])]           # never asks itself
    stored = json.loads((tmp_path / "lanes" / "peers" / "host-b.json").read_text())
    assert stored["browsers"][0]["machine"] == "host-b"
    assert {r["machine"] for r in cl.load_rows(cached=True)} == {"host-a", "host-b"}


def test_snapshot_peers_bad_answer_reports_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-b=b.example.com")
    monkeypatch.setattr(cl, "scan", lambda: [])
    _fake_remote(monkeypatch, result=(255, "ssh: connect timed out"))
    assert cl.main(["snapshot", "--peers"]) == 1
    assert "no profile list from host-b" in capsys.readouterr().err


def test_snapshot_peers_without_hosts_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(cl, "scan", lambda: [])
    assert cl.main(["snapshot", "--peers"]) == 2
    assert "CHROME_LANES_HOSTS is not set" in capsys.readouterr().err


# ---------- session pid ----------

def test_session_pid_prefers_claude_pid_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "424242")
    assert cl.session_pid() == 424242


def test_session_pid_walks_past_shells(monkeypatch):
    monkeypatch.setattr(cl.os, "getppid", lambda: 10)
    tree = {10: (20, "zsh"), 20: (30, "bash"), 30: (40, "claude")}

    class Proc:
        def __init__(self, pid):
            ppid, comm = tree[pid]
            self.stdout = f"{ppid} {comm}\n"

    monkeypatch.setattr(cl.subprocess, "run", lambda cmd, **kw: Proc(int(cmd[-1])))
    assert cl.session_pid() == 30                        # first ancestor that is not a shell


# ---------- guard ----------

def _ext_dir(tmp_path, pdir):
    d = tmp_path / "chrome" / pdir / "Local Extension Settings" / cl.DEFAULT_EXT_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guard_env(tmp_path, monkeypatch, pid=777):
    monkeypatch.setattr(cl, "session_pid", lambda: pid)
    d = leases(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_guard(monkeypatch, capsys, tool="mcp__claude-in-chrome__navigate"):
    monkeypatch.setattr(cl.sys, "stdin", io.StringIO(json.dumps({"tool_name": tool})))
    rc = cl.cmd_guard([])
    return rc, capsys.readouterr().err


def _own_lease(d, **extra):
    lease = {"deviceId": "id-a", "profileName": "Alice", "pid": 777, "project": "t", "since": "x",
             "machine": "host-a"}
    lease.update(extra)
    _write(d / "id-a.json", lease)


def test_guard_allows_without_lease(tmp_path, monkeypatch, capsys):
    _guard_env(tmp_path, monkeypatch)
    assert _run_guard(monkeypatch, capsys) == (0, "")


def test_guard_exits_0_with_a_lease_and_no_takeover_note(tmp_path, monkeypatch, capsys):
    d = _guard_env(tmp_path, monkeypatch)
    _own_lease(d)
    assert _run_guard(monkeypatch, capsys) == (0, "")


def test_guard_passes_recovery_tools_and_keeps_the_takeover_note(tmp_path, monkeypatch, capsys):
    d = _guard_env(tmp_path, monkeypatch, pid=4242)
    note = d / "taken" / "4242.json"
    _write(note, {"pid": 4242, "profileName": "Alice", "by_pid": 9, "by_project": "proj-b"})
    rc, err = _run_guard(monkeypatch, capsys, tool="mcp__claude-in-chrome__select_browser")
    assert (rc, err) == (0, "") and note.exists()             # recovery calls pass, the note waits
    assert _run_guard(monkeypatch, capsys)[0] == 2            # the next ordinary call gets it


def test_guard_stamps_last_used(tmp_path, monkeypatch, capsys):
    d = _guard_env(tmp_path, monkeypatch)
    _own_lease(d)
    _run_guard(monkeypatch, capsys)
    assert "last_used" in json.loads((d / "id-a.json").read_text())


def test_guard_tells_the_old_holder_it_was_taken_over(tmp_path, monkeypatch, capsys):
    d = _guard_env(tmp_path, monkeypatch, pid=4242)
    _write(d / "taken" / "4242.json", {"pid": 4242, "profileName": "Alice", "by_pid": 9, "by_project": "proj-b"})
    rc, err = _run_guard(monkeypatch, capsys)
    assert rc == 2 and "CHROME LEASE TAKEN OVER" in err and "proj-b" in err and "ask the user" in err
    assert _run_guard(monkeypatch, capsys) == (0, "")       # said once


def test_guard_refreshes_remote_copy_every_five_minutes(tmp_path, monkeypatch, capsys):
    d = _guard_env(tmp_path, monkeypatch)
    calls = _fake_remote(monkeypatch)
    _own_lease(d, since="2020-01-01T00:00:00+00:00", remote_machine="host-b")
    _run_guard(monkeypatch, capsys)
    assert len(calls) == 1 and calls[0] == ("host-b", calls[0][1]) and calls[0][1][0] == "lease-put"
    assert "remote_refreshed" in json.loads((d / "id-a.json").read_text())
    _run_guard(monkeypatch, capsys)
    assert len(calls) == 1                                  # refreshed a moment ago: no second ssh


# ---------- HELD idle time, --force and idle takeover ----------

def _lease_idle(d, row, minutes, pid, project, machine="host-a"):
    t = now_iso(minutes)
    _write(d / f"{row['deviceId']}.json", {"deviceId": row["deviceId"], "profileName": row["profileName"],
                                           "pid": pid, "project": project, "since": t, "last_used": t,
                                           "machine": machine})


def test_held_message_shows_idle_time_and_force_takes_over(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _lease_idle(d, FAKE_ROW, 5, other, "sleepy")
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--project", "eager"]) == 4
    err = capsys.readouterr().err
    assert "idle 5m" in err and "--force" in err
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--project", "eager", "--force"]) == 0
    assert "TAKEN OVER" in capsys.readouterr().err
    assert json.loads((d / f"{FAKE_ROW['deviceId']}.json").read_text())["project"] == "eager"
    marker = json.loads((d / "taken" / f"{other}.json").read_text())
    assert marker["by_project"] == "eager" and marker["profileName"] == "Alice Personal"


def test_stuck_holder_is_taken_over_without_force(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _lease_idle(d, FAKE_ROW, 5, other, "busy")
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--project", "next"]) == 4
    assert "HELD" in capsys.readouterr().err                 # 5 min idle: still theirs
    _lease_idle(d, FAKE_ROW, 16, other, "busy")
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--project", "next"]) == 0
    err = capsys.readouterr().err
    assert "TAKEN OVER" in err and "stuck threshold" in err
    assert json.loads((d / f"{FAKE_ROW['deviceId']}.json").read_text())["project"] == "next"


def test_stale_minutes_comes_from_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_STALE_MINUTES", "3")
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _lease_idle(d, FAKE_ROW, 4, other, "busy")
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--project", "next"]) == 0
    assert "over the 3 min stuck threshold" in capsys.readouterr().err


def test_bad_stale_minutes_falls_back_to_15_and_says_so(monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_STALE_MINUTES", "soon")
    assert cl.stale_minutes() == 15
    assert "CHROME_LANES_STALE_MINUTES" in capsys.readouterr().err
    monkeypatch.setenv("CHROME_LANES_STALE_MINUTES", "0")
    assert cl.stale_minutes() == 15


# ---------- machine-wide turn ----------

def _turn_env(tmp_path, monkeypatch):
    return _setup_lease_env(tmp_path, monkeypatch, rows=(FAKE_ROW, ROW_B))


def test_turn_is_machine_wide_any_profile(tmp_path, monkeypatch, capsys):
    d = _turn_env(tmp_path, monkeypatch)
    me = os.getpid()
    assert cl.cmd_claim(["Alice Personal", "--pid", str(me), "--project", "first"]) == 0
    capsys.readouterr()
    assert cl.cmd_claim(["Bob Work", "--pid", str(me + 1), "--project", "second"]) == 4   # other profile
    err = capsys.readouterr().err
    assert "Chrome turn" in err and "first" in err and "--wait" in err
    assert not (d / f"{ROW_B['deviceId']}.json").exists()
    assert cl.cmd_claim(["Bob Work", "--pid", str(me), "--project", "first"]) == 0     # same session: ok


def test_stuck_turn_holder_is_taken_over_across_profiles(tmp_path, monkeypatch, capsys):
    d = _turn_env(tmp_path, monkeypatch)
    me = os.getpid()
    _lease_idle(d, FAKE_ROW, 20, me, "asleep")
    assert cl.cmd_claim(["Bob Work", "--pid", str(me + 1), "--project", "awake"]) == 0
    assert "TAKEN OVER the Chrome turn" in capsys.readouterr().err
    assert not (d / f"{FAKE_ROW['deviceId']}.json").exists()
    assert json.loads((d / "taken" / f"{me}.json").read_text())["by_project"] == "awake"


def test_claim_wait_polls_until_free(tmp_path, monkeypatch, capsys):
    _turn_env(tmp_path, monkeypatch)
    answers = iter([4, 4, 0])
    monkeypatch.setattr(cl, "_claim_once", lambda argv: next(answers))
    slept = []
    monkeypatch.setattr(cl.time, "sleep", lambda s: slept.append(s))
    assert cl.cmd_claim(["Bob Work", "--wait", "600"]) == 0
    assert slept == [10, 10]
    assert "waiting for the Chrome turn" in capsys.readouterr().err


def test_release_name_refuses_another_sessions_live_lease(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _write(d / f"{FAKE_ROW['deviceId']}.json", {"deviceId": FAKE_ROW["deviceId"], "profileName": "Alice Personal",
                                                "pid": other, "project": "theirs", "since": "x",
                                                "last_used": now_iso(), "machine": "host-a"})
    assert cl.cmd_release(["Alice Personal", "--pid", str(other + 1)]) == 4
    assert "NOT YOURS" in capsys.readouterr().err
    assert (d / f"{FAKE_ROW['deviceId']}.json").exists()
    assert cl.cmd_release(["Alice Personal", "--pid", str(other + 1), "--force"]) == 0
    assert not (d / f"{FAKE_ROW['deviceId']}.json").exists()


def test_release_all_matches_machine_not_only_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_MACHINE", "host-b")
    d = _setup_lease_env(tmp_path, monkeypatch)
    _write(d / "remote-copy.json", {"deviceId": "rc", "profileName": "Default", "pid": 4242, "project": "a-work",
                                    "since": "x", "machine": "host-a", "expires": "2999-01-01T00:00:00+00:00"})
    _write(d / "own.json", {"deviceId": "own", "profileName": "Bob", "pid": 4242, "project": "b-work",
                            "since": "x", "machine": "host-b"})
    assert cl.cmd_release(["--all", "--pid", "4242"]) == 0
    assert (d / "remote-copy.json").exists()               # same pid number, other machine: untouched
    assert not (d / "own.json").exists()


def test_takeover_of_foreign_lease_tells_holder_over_ssh(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHROME_LANES_HOSTS", "host-b=b.example.com")
    d = _turn_env(tmp_path, monkeypatch)
    calls = _fake_remote(monkeypatch)
    _write(d / "remote-copy.json", {"deviceId": "rc", "profileName": "Default", "pid": 4242, "project": "b-work",
                                    "since": "2020-01-01T00:00:00+00:00", "machine": "host-b",
                                    "expires": "2999-01-01T00:00:00+00:00"})
    assert cl.cmd_claim(["Bob Work", "--pid", "9", "--project", "eager"]) == 0
    assert "TAKEN OVER the Chrome turn" in capsys.readouterr().err
    taken = [c for c in calls if c[1][0] == "lease-taken"]
    assert taken and taken[0][0] == "host-b"
    assert json.loads(taken[0][1][1])["pid"] == 4242


def test_claim_option_without_value_is_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    _turn_env(tmp_path, monkeypatch)
    assert cl.cmd_claim(["Bob Work", "--wait"]) == 2
    assert "needs a value" in capsys.readouterr().err
    assert cl.cmd_claim(["Bob Work", "--ttl", "soon"]) == 2
    assert "expected a number" in capsys.readouterr().err


def test_unreadable_timestamp_counts_as_stuck():
    assert cl.idle_minutes({"since": None}) >= cl.stale_minutes()
    assert cl.idle_text({"since": None}) == "idle unknown"


# ---------- find_by_name ----------

_ROWS = [
    {"deviceId": "aaaaaaaa-0001", "machine": "host-a", "profileName": "work.example.com",
     "account": "alice@work.example.com", "label": "work"},
    {"deviceId": "bbbbbbbb-0002", "machine": "host-b", "profileName": "work.example.com",
     "account": "alice@work.example.com", "label": "work-host-b"},
    {"deviceId": "cccccccc-0003", "machine": "host-a", "profileName": "personal@example.com",
     "account": "personal@example.com", "label": ""},
]


def test_find_by_name_same_name_on_two_machines_is_ambiguous():
    assert len(cl.find_by_name(_ROWS, "work.example.com")) == 2


def test_find_by_name_machine_prefix_pins_one_row():
    assert [h["deviceId"] for h in cl.find_by_name(_ROWS, "host-b:work")] == ["bbbbbbbb-0002"]
    assert [h["deviceId"] for h in cl.find_by_name(_ROWS, "HOST-A:work.example.com")] == ["aaaaaaaa-0001"]


def test_find_by_name_device_id_matches_exactly():
    assert [h["machine"] for h in cl.find_by_name(_ROWS, "aaaaaaaa-0001")] == ["host-a"]


def test_find_by_name_colon_in_a_plain_name_still_works():
    assert cl.find_by_name(_ROWS, "desk:work") == []          # unknown machine: a plain name
    assert len(cl.find_by_name(_ROWS, "personal@example.com")) == 1


# ---------- env-driven paths ----------

def test_paths_follow_chrome_lanes_home(tmp_path):
    assert cl.snapshot_path() == str(tmp_path / "lanes" / "snapshot.json")
    assert cl.lease_dir() == str(tmp_path / "lanes" / "leases")
    assert cl.peers_dir() == str(tmp_path / "lanes" / "peers")


def test_default_home_is_under_dot_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROME_LANES_HOME")
    assert cl.lanes_home() == str(tmp_path / "home" / ".config" / "chrome-lanes")


def test_machine_name_env_then_short_hostname(monkeypatch):
    assert cl.machine_name() == "host-a"
    monkeypatch.delenv("CHROME_LANES_MACHINE")
    monkeypatch.setattr(cl.socket, "gethostname", lambda: "host-b.example.com")
    assert cl.machine_name() == "host-b"


def test_profile_root_env_and_platform_defaults(tmp_path, monkeypatch):
    assert cl.profile_root() == str(tmp_path / "chrome")
    monkeypatch.delenv("CHROME_LANES_PROFILE_ROOT")
    home = tmp_path / "home"
    monkeypatch.setattr(cl.sys, "platform", "darwin")
    assert cl.profile_root() == str(home / "Library" / "Application Support" / "Google" / "Chrome")
    monkeypatch.setattr(cl.sys, "platform", "linux")
    assert cl.profile_root() == str(home / ".config" / "google-chrome")
    monkeypatch.setattr(cl.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert cl.profile_root() == os.path.join(str(tmp_path / "local"), "Google", "Chrome", "User Data")


def _fake_chrome(tmp_path):
    root = tmp_path / "chrome"
    _write(root / "Local State", {"profile": {"info_cache": {
        "Profile 1": {"name": "Alice Personal", "user_name": "alice@example.com"},
        "Profile 2": {"name": "No Extension", "user_name": "bob@example.com"}}}})
    ext = _ext_dir(tmp_path, "Profile 1")
    (ext / "000003.log").write_bytes(wrap_full_record(build_wal_batch(5, [
        (1, b"bridgeDeviceId", json.dumps(FAKE_ROW["deviceId"]).encode()),
        (1, b"bridgeDisplayName", json.dumps("alice-desk").encode())])))
    return root


def test_snapshot_and_lookup_read_the_profile_root(tmp_path, capsys):
    _fake_chrome(tmp_path)
    assert cl.main(["snapshot"]) == 0
    rows = json.loads((tmp_path / "lanes" / "snapshot.json").read_text())["browsers"]
    assert rows == [dict(FAKE_ROW, machine="host-a")]            # the profile without the extension is skipped
    capsys.readouterr()
    assert cl.main(["lookup", "alice-desk", "--cached"]) == 0
    assert FAKE_ROW["deviceId"] in capsys.readouterr().out
    assert cl.main(["lookup", "nobody"]) == 2
    assert "NO PROFILE matches" in capsys.readouterr().err


def test_unreadable_storage_is_reported_not_called_never_connected(tmp_path, capsys):
    root = tmp_path / "chrome"
    _write(root / "Local State", {"profile": {"info_cache": {"Profile 1": {"name": "Alice", "user_name": ""}}}})
    (_ext_dir(tmp_path, "Profile 1") / "000005.ldb").write_bytes(b"truncated, not an sstable")
    rows = cl.scan()
    assert rows[0]["deviceId"] is None and rows[0]["unreadable"] == 1
    assert cl.main(["list"]) == 0
    assert "(unreadable storage, 1 files)" in capsys.readouterr().out
    assert cl.main(["claim", "Alice", "--pid", "5"]) == 2
    assert "unreadable storage" in capsys.readouterr().err


def test_extension_id_comes_from_env(tmp_path, monkeypatch):
    _fake_chrome(tmp_path)
    monkeypatch.setenv("CHROME_LANES_EXTENSION_ID", "otherextensionid")
    assert cl.scan() == []


def test_snapshot_without_chrome_exits_1_and_writes_nothing(tmp_path, capsys):
    assert cl.main(["snapshot"]) == 1
    assert "cannot read Chrome's profile list" in capsys.readouterr().err
    assert not (tmp_path / "lanes" / "snapshot.json").exists()


def test_list_cached_without_snapshot_exits_3(capsys):
    assert cl.main(["list", "--cached"]) == 3
    assert "no snapshot" in capsys.readouterr().err


def test_claim_without_chrome_or_snapshot_exits_1(capsys):
    assert cl.main(["claim", "alice", "--pid", "5"]) == 1
    assert "cannot read Chrome's profile list" in capsys.readouterr().err


# ---------- open, snippet, help ----------

def test_open_command_per_platform(monkeypatch):
    monkeypatch.setattr(cl.sys, "platform", "darwin")
    assert cl.chrome_open_command("Profile 3") == ["open", "-na", "Google Chrome", "--args",
                                                    "--profile-directory=Profile 3"]
    monkeypatch.setattr(cl.sys, "platform", "linux")
    assert cl.chrome_open_command("Profile 3") == ["google-chrome", "--profile-directory=Profile 3"]
    monkeypatch.setenv("CHROME_LANES_CHROME_BIN", "chromium")
    assert cl.chrome_open_command("Profile 3")[0] == "chromium"
    monkeypatch.setattr(cl.sys, "platform", "win32")
    assert cl.chrome_open_command("Profile 3")[:4] == ["cmd", "/c", "start", ""]


def test_open_launches_local_profile_and_refuses_remote(tmp_path, monkeypatch, capsys):
    _setup_lease_env(tmp_path, monkeypatch, rows=(FAKE_ROW, REMOTE_ROW))
    monkeypatch.setattr(cl.sys, "platform", "darwin")
    launched = []
    monkeypatch.setattr(cl.subprocess, "call", lambda cmd: launched.append(cmd) or 0)
    assert cl.cmd_open(["Alice Personal"]) == 0
    assert launched and launched[0][-1] == "--profile-directory=Profile 1"
    assert cl.cmd_open(["Host B Chrome"]) == 2
    assert "not on this machine" in capsys.readouterr().err and len(launched) == 1


def test_snippet_is_valid_json_with_three_entries(capsys):
    assert cl.main(["snippet"]) == 0
    out, err = capsys.readouterr()
    frag = json.loads(out)
    assert frag["permissions"]["deny"] == ["mcp__claude-in-chrome__switch_browser"]
    start = frag["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    guard = frag["hooks"]["PreToolUse"][0]
    assert start.endswith("snapshot || true") and "chrome-lanes" in start
    assert guard["matcher"] == "mcp__claude-in-chrome__.*"
    assert guard["hooks"][0]["command"].endswith(" guard") and guard["hooks"][0]["timeout"] == 15
    for key in ("permissions.deny", "hooks.SessionStart", "hooks.PreToolUse"):
        assert key in err                                    # each entry is explained
    assert "Nothing was written" in err


def test_help_exits_zero_and_names_every_command(capsys):
    assert cl.main(["--help"]) == 0
    out = capsys.readouterr().out
    for word in ("list", "lookup", "snapshot", "claim", "release", "status", "guard", "open", "snippet",
                 "CHROME_LANES_HOME", "CHROME_LANES_STALE_MINUTES", "CHROME_LANES_HOSTS"):
        assert word in out
    assert cl.main(["claim", "--help"]) == 0
    assert cl.main(["bogus"]) == 2


# ---------- --no-takeover (background jobs) and queueing ----------

def test_no_takeover_leaves_an_idle_holder_alone(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _lease_idle(d, FAKE_ROW, 60, other, "person")
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--project", "cron",
                         "--no-takeover"]) == 4
    err = capsys.readouterr().err
    assert "HELD" in err and "TAKEN OVER" not in err
    assert json.loads((d / f"{FAKE_ROW['deviceId']}.json").read_text())["project"] == "person"
    assert not (d / "taken" / f"{other}.json").exists()


def test_no_takeover_still_claims_a_free_turn(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    assert cl.cmd_claim(["Alice Personal", "--pid", "77", "--project", "cron", "--no-takeover"]) == 0
    assert json.loads((d / f"{FAKE_ROW['deviceId']}.json").read_text())["project"] == "cron"


def test_no_takeover_with_force_is_a_usage_error(tmp_path, monkeypatch, capsys):
    _setup_lease_env(tmp_path, monkeypatch)
    assert cl.cmd_claim(["Alice Personal", "--force", "--no-takeover"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_held_hint_tells_the_agent_to_queue(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _lease_idle(d, FAKE_ROW, 1, other, "busy")
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1)]) == 4
    err = capsys.readouterr().err
    assert "Queue for it" in err and "--wait 590" in err


def test_wait_with_no_takeover_keeps_queueing(tmp_path, monkeypatch, capsys):
    d = _setup_lease_env(tmp_path, monkeypatch)
    other = os.getpid()
    _lease_idle(d, FAKE_ROW, 60, other, "person")
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(cl.time, "time", lambda: next(clock))
    monkeypatch.setattr(cl.time, "sleep", lambda s: None)
    assert cl.cmd_claim(["Alice Personal", "--pid", str(other + 1), "--no-takeover", "--wait", "30"]) == 4
    assert json.loads((d / f"{FAKE_ROW['deviceId']}.json").read_text())["project"] == "person"
