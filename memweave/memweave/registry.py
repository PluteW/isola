"""项目注册表 v0：YAML 持久化。描述必须目标导向（O4：近况片段无区分力）。"""
from __future__ import annotations
import time
import yaml
import pathlib


class Registry:
    def __init__(self, path: str):
        self.path = pathlib.Path(path)
        self._data = {"projects": []}
        if self.path.exists():
            self._data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {"projects": []}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")

    def active_projects(self) -> list:
        return [p for p in self._data["projects"] if p.get("status", "active") == "active"]

    def get(self, pid: int) -> dict | None:
        return next((p for p in self._data["projects"] if p["id"] == pid), None)

    def add(self, name: str, desc: str) -> dict:
        pid = max((p["id"] for p in self._data["projects"]), default=0) + 1
        p = {"id": pid, "name": name, "desc": desc, "status": "active",
             "created": time.strftime("%Y-%m-%d")}
        self._data["projects"].append(p)
        self._save()
        return p

    def archive(self, pid: int):
        p = self.get(pid)
        if p:
            p["status"] = "archived"
            self._save()
