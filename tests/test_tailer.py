import os

from copilot.tailer import Checkpoint, Tailer


def append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(text)


def test_reads_only_complete_lines(tmp_path):
    log = tmp_path / "app.log"
    append(log, "one\ntwo\nthr")
    t = Tailer(log)
    assert t.read_new() == ["one", "two"]
    append(log, "ee\n")
    assert t.read_new() == ["three"]
    assert t.read_new() == []


def test_resumes_from_checkpoint(tmp_path):
    log = tmp_path / "app.log"
    append(log, "one\ntwo\n")
    t = Tailer(log)
    t.read_new()
    append(log, "three\n")
    resumed = Tailer(log, Checkpoint(t.cp.offset, t.cp.inode))
    assert resumed.read_new() == ["three"]


def test_truncation_restarts_from_beginning(tmp_path):
    log = tmp_path / "app.log"
    append(log, "a long first line\n")
    t = Tailer(log)
    t.read_new()
    log.write_text("new\n")
    assert t.read_new() == ["new"]


def test_rotation_drains_old_file_first(tmp_path):
    log = tmp_path / "app.log"
    append(log, "old-1\n")
    t = Tailer(log)
    assert t.read_new() == ["old-1"]
    append(log, "old-2\n")
    os.replace(log, tmp_path / "app.log.1")  # what RotatingFileHandler does
    append(log, "new-1\n")
    assert t.read_new() == ["old-2", "new-1"]


def test_missing_file(tmp_path):
    assert Tailer(tmp_path / "nope.log").read_new() == []
