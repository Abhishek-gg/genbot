"""Follow a growing log file, surviving restarts, truncation and rotation."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Checkpoint:
    offset: int = 0
    inode: int | None = None


class Tailer:
    def __init__(self, path: Path, checkpoint: Checkpoint | None = None):
        self.path = Path(path)
        self.cp = checkpoint or Checkpoint()

    def read_new(self) -> list[str]:
        """Return complete lines written since the last call. A trailing partial line is left for next time."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []

        lines: list[str] = []
        if self.cp.inode is not None and st.st_ino != self.cp.inode:
            # Rotated: RotatingFileHandler renamed the old file to .1, so drain what's left of it first.
            rotated = self.path.with_name(self.path.name + ".1")
            if rotated.exists() and os.stat(rotated).st_ino == self.cp.inode:
                lines, _ = self._read_from(rotated, self.cp.offset)
            self.cp = Checkpoint(0, st.st_ino)
        elif st.st_size < self.cp.offset:
            self.cp.offset = 0  # truncated

        self.cp.inode = st.st_ino
        new, self.cp.offset = self._read_from(self.path, self.cp.offset)
        return lines + new

    @staticmethod
    def _read_from(path: Path, offset: int) -> tuple[list[str], int]:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end == -1:
            return [], offset
        complete = data[: end + 1]
        return complete.decode("utf-8", errors="replace").splitlines(), offset + len(complete)
