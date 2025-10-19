"""Herramientas simples para mostrar una barra de progreso en consola."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class TextProgressBar:
    total: int
    width: int = 30
    stream = sys.stdout

    def __post_init__(self) -> None:
        if self.total <= 0:
            self.total = 1
        self.current = 0
        self._render(force=True)

    def step(self, label: str = "") -> None:
        self.current = min(self.current + 1, self.total)
        self._render(label=label)

    def close(self, label: str = "") -> None:
        self.current = self.total
        self._render(label=label, end="\n")

    def _render(self, label: str = "", *, force: bool = False, end: str = "\r") -> None:
        filled = int(self.width * self.current / self.total)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = self.current / self.total * 100
        prefix = f"[{bar}] {percent:5.1f}%"
        message = f"{prefix} {label}" if label else prefix
        self.stream.write(message.ljust(self.width + 30) + end)
        self.stream.flush()
