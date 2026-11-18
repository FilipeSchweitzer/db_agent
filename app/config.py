"""Carrega a config do cliente (project.yaml) e variáveis de ambiente.

Nada específico de cliente é hardcoded em código — tudo vem de
config/<client>/project.yaml, montado como volume no container.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

CONFIG_ROOT = Path(os.environ.get("CONFIG_ROOT", "/app/config"))

# Estratégias aceitas para a PK de uma tabela de destino:
#   sequence -> o destino gera a chave (o de-para preserva o relacionamento)
#   preserve -> a chave da origem é copiada como está
PK_STRATEGIES = ("sequence", "preserve")


class DBConnection(BaseModel):
    dialect: str
    host: str
    port: int
    database: str
    user: str
    password: str = ""  # preenchido a partir de env var na leitura
    # Schema do banco. No script gerado ele qualifica as tabelas
    # (`legado.CAD_PESSOA`); vazio = sem qualificação.
    schema_name: str | None = None


class ETLConfig(BaseModel):
    # O script gerado cria sequences e tabelas do destino. Desligue quando o
    # schema de destino já existe e é gerenciado por outro time.
    create_schema: bool = True
    # INSERT idempotente por dialeto (ON CONFLICT / WHERE NOT EXISTS /
    # UPDATE OR INSERT). Sem isso, rodar o script duas vezes duplica dados.
    idempotent: bool = True
    # Override por tabela: {"pessoa": "preserve"} quando a PK da origem deve
    # ser mantida em vez de gerada pela sequence do destino.
    pk_strategy: dict[str, str] = {}
    # Padrão de nome das sequences criadas/usadas no destino. Com
    # create_schema=false, o script assume que elas já existem com este nome.
    sequence_pattern: str = "seq_{table}"
    # FK apontando para tabela que não está sendo migrada: falha por padrão,
    # porque o valor seria copiado sem tradução de chave.
    allow_unresolved_fks: bool = False
    # Acima disso, o relatório avisa que a carga em uma transação só é pesada.
    high_volume_rows: int = 100_000

    @field_validator("pk_strategy")
    @classmethod
    def _validar_estrategia(cls, value: dict[str, str]) -> dict[str, str]:
        for table, strategy in value.items():
            if strategy not in PK_STRATEGIES:
                raise ValueError(
                    f"etl.pk_strategy[{table!r}] = {strategy!r} — use um de {PK_STRATEGIES}"
                )
        return value

    @field_validator("sequence_pattern")
    @classmethod
    def _validar_pattern(cls, value: str) -> str:
        if "{table}" not in value:
            raise ValueError(
                f"etl.sequence_pattern precisa conter '{{table}}': recebido {value!r}"
            )
        return value


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
