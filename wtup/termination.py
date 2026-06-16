from __future__ import annotations

from typing import Any


class TaskTerminatedError(RuntimeError):
    def __init__(self, stage: str = ""):
        self.stage = str(stage or "").strip()
        message = "任务已被后台开关终止"
        if self.stage:
            message = f"{message}: {self.stage}"
        super().__init__(message)


def task_should_terminate(settings: Any) -> bool:
    checker = getattr(settings, "task_termination_checker", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(settings, "terminate_running_task", False))


def check_task_termination(settings: Any, stage: str = "") -> None:
    if task_should_terminate(settings):
        raise TaskTerminatedError(stage)


def reset_task_termination(settings: Any) -> None:
    resetter = getattr(settings, "task_termination_resetter", None)
    if callable(resetter):
        resetter()
