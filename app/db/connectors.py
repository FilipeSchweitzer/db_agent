"""Abstrai a criação de engines SQLAlchemy por dialeto.

Adicionar um novo SGBD legado = adicionar uma entrada aqui, não reescrever
o resto do pipeline.

Firebird saiu do core do SQLAlchemy 2.0 (C5): o dialeto vem do pacote
`sqlalchemy-firebird` e o DBAPI é o `firebird-driver` (URL
`firebird+firebird://`, equivalente ao antigo `firebird+fdb://`). O driver
carrega a biblioteca **cliente** do Firebird (`libfbclient`) — que não é o
servidor. Em Linux ela vem de `libfbclient2`; se a versão do sistema não
servir, aponte outra em `FIREBIRD_CLIENT_LIBRARY`.

Caminho de banco no Firebird é do ponto de vista do **servidor**: use o
caminho absoluto (`/dados/PREFEITURA.FDB`) e o SQLAlchemy o renderiza com
barra dupla, como o driver exige.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

from app.config import DBConnection

_DRIVER_BY_DIALECT = {
    "firebird": "firebird+firebird",
    "sqlserver": "mssql+pyodbc",
    "postgres": "postgresql+psycopg2",
}

# Driver ODBC do SQL Server (instalado na imagem; ver Dockerfile).
_SQLSERVER_QUERY = {
    "driver": "ODBC Driver 18 for SQL Server",
    "TrustServerCertificate": "yes",
}


def build_url(conn: DBConnection) -> str:
    driver = _DRIVER_BY_DIALECT.get(conn.dialect)
    if driver is None:
        raise ValueError(f"Dialeto não suportado: {conn.dialect}")

    query: dict[str, str] = {}
    if conn.dialect == "sqlserver":
        # pyodbc precisa do nome do driver ODBC; o 18 vem com a imagem.
        query.update(_SQLSERVER_QUERY)
    if conn.dialect == "firebird":
        client_library = os.environ.get("FIREBIRD_CLIENT_LIBRARY")
        if client_library:
            query["fb_client_library"] = client_library

    # URL.create cuida do escape de senha com '@', '/', ':' etc. — concatenar
    # string aqui já quebrou conexão com senha especial no passado.
    url = URL.create(
        drivername=driver,
        username=conn.user,
        password=conn.password or None,
        host=conn.host,
        port=conn.port,
        database=conn.database,
        query=query,
    )
    return url.render_as_string(hide_password=False)


def get_engine(conn: DBConnection, read_only: bool = False) -> Engine:
    """Engine da conexão.

    `read_only=True` é usado para o **legado** (descoberta e tools). No
    Postgres isso vira `default_transaction_read_only=on` na sessão — o
    servidor recusa escrita de verdade. Firebird e SQL Server não têm
    equivalente de sessão: nesses dois, a garantia é o `GRANT SELECT` do
    usuário (ver README, "Provisionamento do usuário de leitura").
    """
    url = build_url(conn)
    connect_args: dict = {}
    if read_only and conn.dialect == "postgres":
        connect_args["options"] = "-c default_transaction_read_only=on"
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def firebird_client_library_path() -> str | None:
    """Atalho para diagnóstico: onde o `firebird-driver` procura a lib cliente."""
    return os.environ.get("FIREBIRD_CLIENT_LIBRARY") or None


__all__ = ["build_url", "get_engine", "firebird_client_library_path"]
