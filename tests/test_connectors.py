import pytest

from app.config import DBConnection
from app.db.connectors import build_url


def test_build_url_postgres():
    conn = DBConnection(
        dialect="postgres", host="localhost", port=5432,
        database="gestao", user="migrador", password="x",
    )
    url = build_url(conn)
    assert url.startswith("postgresql+psycopg2://migrador:x@localhost:5432/gestao")


def test_build_url_firebird():
    conn = DBConnection(
        dialect="firebird", host="192.168.1.50", port=3050,
        database="/dados/PREFEITURA.FDB", user="SYSDBA", password="masterkey",
    )
    url = build_url(conn)
    assert url.startswith("firebird+firebird://SYSDBA:masterkey@192.168.1.50:3050")


def test_build_url_dialeto_invalido():
    conn = DBConnection(
        dialect="oracle", host="h", port=1, database="d", user="u", password="p",
    )
    with pytest.raises(ValueError):
        build_url(conn)
