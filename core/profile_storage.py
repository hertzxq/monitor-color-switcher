import json
import os
import sys
import time
from typing import List

from models.profile import GameProfile


class ProfileStorage:
    def __init__(self, file_path: str):
        self.file_path = file_path

    def load(self) -> List[GameProfile]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # The file exists but is unreadable / malformed. Returning [] would
            # let MainWindow create a fresh desktop profile and overwrite the
            # broken file, destroying any recoverable user data. Move it aside
            # first so the user can recover by hand.
            self._move_aside_broken(reason=f"{type(e).__name__}: {e}")
            return []

        if not isinstance(raw, list):
            self._move_aside_broken(reason="root JSON is not a list")
            return []

        out: List[GameProfile] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                out.append(GameProfile.from_dict(item))
            except (TypeError, ValueError):
                # Skip a single malformed entry but keep the rest.
                continue
        return out

    def _move_aside_broken(self, reason: str) -> None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        broken = f"{self.file_path}.broken-{ts}"
        try:
            os.replace(self.file_path, broken)
            print(
                f"[profile_storage] profiles file unreadable ({reason}); "
                f"moved aside to {broken}",
                file=sys.stderr,
            )
        except OSError as e:
            print(
                f"[profile_storage] profiles file unreadable ({reason}); "
                f"failed to back up: {e}",
                file=sys.stderr,
            )

    def save(self, profiles: List[GameProfile]) -> None:
        data = [p.to_dict() for p in profiles]
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.file_path)
