from dataclasses import dataclass, asdict


@dataclass
class GameProfile:
    name: str
    process: str
    exe_path: str = ""        # full path to .exe (used as icon source)
    vibrance: int = 50        # 0..100  (NvAPI DVC, 0 = neutral, 50 = default mid)
    brightness: float = 1.0   # 0.3..2.0
    contrast: float = 1.0     # 0.3..2.0
    gamma: float = 1.0        # 0.3..3.0
    black_lift: float = 0.0   # 0.0..0.5 — selectively brighten dark pixels
                              # (shadow boost / "black equalizer"); 0 = off
    is_desktop: bool = False  # True for the singleton "Рабочий стол" profile
                              # applied when no game is running

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GameProfile":
        return cls(
            name=str(data.get("name", "")),
            process=str(data.get("process", "")),
            exe_path=str(data.get("exe_path", "")),
            vibrance=int(data.get("vibrance", 50)),
            brightness=float(data.get("brightness", 1.0)),
            contrast=float(data.get("contrast", 1.0)),
            gamma=float(data.get("gamma", 1.0)),
            black_lift=float(data.get("black_lift", 0.0)),
            is_desktop=bool(data.get("is_desktop", False)),
        )
