"""Abstrai a criação de engines SQLAlchemy por dialeto.

Adicionar um novo SGBD legado = adicionar uma entrada aqui, não reescrever
o resto do pipeline.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.config import DBConnection

_DRIVER_BY_DIALECT = {
    "firebird": "firebird+fdb",
    "sqlserver": "mssql+pyodbc",
    "postgres": "postgresql+psycopg2",
}


def build_url(conn: DBConnection) -> str:
    driver = _DRIVER_BY_DIALECT.get(conn.dialect)
    if driver is None:
        raise ValueError(f"Dialeto não suportado: {conn.dialect}")

    if conn.dialect == "sqlserver":
        # pyodbc precisa do driver ODBC nomeado explicitamente
        return (
            f"{driver}://{conn.user}:{conn.password}@{conn.host}:{conn.port}"
            f"/{conn.database}?driver=ODBC+Driver+18+for+SQL+Server"
            "&TrustServerCertificate=yes"
        )

    return f"{driver}://{conn.user}:{conn.password}@{conn.host}:{conn.port}/{conn.database}"


def get_engine(conn: DBConnection, read_only: bool = False) -> Engine:
    url = build_url(conn)
    engine = create_engine(url, pool_pre_ping=True)
    # TODO: para read_only=True, garantir no provisionamento do usuário de
    # banco que ele só tem GRANT de SELECT — não depender só da aplicação.
    return engine
