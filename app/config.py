"""Carrega a config do cliente (project.yaml) e variáveis de ambiente.

Nada específico de cliente é hardcoded em código — tudo vem de
config/<client>/project.yaml, montado como volume no container.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_ROOT = Path(os.environ.get("CONFIG_ROOT", "/app/config"))


class DBConnection(BaseModel):
    dialect: str
    host: str
    port: int
    database: str
    user: str
    password: str = ""  # preenchido a partir de env var na leitura


class ETLConfig(BaseModel):
    engine: str = "sql"
    batch_size: int = 2000


class ProjectConfig(BaseModel):
    project: str
    source: DBConnection
    target: DBConnection
    target_schema_file: str
    scope_tables: list[str] = []
    etl: ETLConfig = ETLConfig()

    @property
    def target_schema_path(self) -> Path:
        return CONFIG_ROOT / self.project / self.target_schema_file


def load_project_config(client_name: str) -> ProjectConfig:
    path = CONFIG_ROOT / client_name / "project.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config não encontrada: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = ProjectConfig(**data)

    # Senhas nunca ficam no YAML — vêm de env vars específicas do projeto
    cfg.source.password = os.environ.get("SOURCE_DB_PASSWORD", "")
    cfg.target.password = os.environ.get("TARGET_DB_PASSWORD", "")
    return cfg
