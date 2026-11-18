"""Prova de aceitação da Fase 2 contra um Postgres real.

Cenário exigido pelo critério: três tabelas relacionadas migram, o script
roda duas vezes seguidas sem duplicar, e a contagem bate.

A origem vive no schema `legado` e o destino em `destino`, os dois no mesmo
banco — exatamente a premissa do script gerado (origem e destino alcançáveis
na mesma conexão). As chaves antigas começam em 500 para que seja visível
que a chave nova é gerada pelo destino e que a FK foi traduzida.

Roda só quando `MIGRA_TEST_POSTGRES_DSN` está definido:

    MIGRA_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/postgres \\
        .venv/bin/pytest tests/test_integration_postgres.py -v
"""
from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.agents.agent3_etl import ETLAgent  # noqa: E402
from tests.conftest import make_mapping, make_schema  # noqa: E402

DSN = os.environ.get("MIGRA_TEST_POSTGRES_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="MIGRA_TEST_POSTGRES_DSN não definido"),
]

SOURCE_SCHEMA = "legado"
TARGET_SCHEMA = "destino"

SOURCE_TABLES = {
    "municipio": "CAD_MUNICIPIO",
    "bairro": "CAD_BAIRRO",
    "pessoa": "CAD_PESSOA",
}

SETUP_SOURCE = f"""
DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE;
CREATE SCHEMA {SOURCE_SCHEMA};

CREATE TABLE {SOURCE_SCHEMA}."CAD_MUNICIPIO" (
    "COD_MUN" integer PRIMARY KEY,
    "NOME_MUN" text NOT NULL,
    "UF" varchar(2)
);

CREATE TABLE {SOURCE_SCHEMA}."CAD_BAIRRO" (
    "COD_BAIRRO" integer PRIMARY KEY,
    "NOME_BAIRRO" text NOT NULL,
    "COD_MUN" integer REFERENCES {SOURCE_SCHEMA}."CAD_MUNICIPIO" ("COD_MUN")
);

CREATE TABLE {SOURCE_SCHEMA}."CAD_PESSOA" (
    "COD_PESSOA" integer PRIMARY KEY,
    "CPF" text,
    "NOME" text NOT NULL,
    "DT_NASC" text,
    "COD_BAIRRO" integer REFERENCES {SOURCE_SCHEMA}."CAD_BAIRRO" ("COD_BAIRRO")
);

INSERT INTO {SOURCE_SCHEMA}."CAD_MUNICIPIO" VALUES
    (500, 'Florianópolis', 'SC'),
    (501, 'Joinville', 'SC');

INSERT INTO {SOURCE_SCHEMA}."CAD_BAIRRO" VALUES
    (700, 'Centro', 500),
    (701, 'Trindade', 500),
    (702, 'Centro', 501);

INSERT INTO {SOURCE_SCHEMA}."CAD_PESSOA" VALUES
    (900, '123.456.789-00', 'Ana Maria', '10/05/1980', 700),
    (901, '987.654.321-00', 'Bruno Souza', '31/12/1990', 701),
    (902, '000.000.000-00', 'Carla Dias', NULL, 702),
    (903, '111.222.333-44', 'Diego Alves', '01/01/2000', NULL);
"""

DROP_TARGET = f'DROP SCHEMA IF EXISTS {TARGET_SCHEMA} CASCADE;'
CREATE_TARGET = f"CREATE SCHEMA {TARGET_SCHEMA};"

# Cada consulta da validação devolve uma linha; a carga está correta quando
# origem == destino e todo contador de órfão é 0.
ORPHAN_COUNTS = [
    f"""SELECT COUNT(*) FROM {TARGET_SCHEMA}."bairro" c
        LEFT JOIN {TARGET_SCHEMA}."municipio" p ON p."id" = c."cod_municipio"
        WHERE c."cod_municipio" IS NOT NULL AND p."id" IS NULL""",
    f"""SELECT COUNT(*) FROM {TARGET_SCHEMA}."pessoa" c
        LEFT JOIN {TARGET_SCHEMA}."bairro" p ON p."id" = c."cod_bairro"
        WHERE c."cod_bairro" IS NOT NULL AND p."id" IS NULL""",
]


@pytest.fixture
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = True
    yield connection
    connection.close()


@pytest.fixture
def generated(tmp_path, conn):
    with conn.cursor() as cur:
        cur.execute(DROP_TARGET)
        cur.execute(SETUP_SOURCE)
        cur.execute(CREATE_TARGET)

    result = ETLAgent().run(
        make_mapping(),
        target_schema=make_schema(),
        source_dialect="postgres",
        target_dialect="postgres",
        output_dir=tmp_path,
        source_schema=SOURCE_SCHEMA,
        target_schema_name=TARGET_SCHEMA,
    )
    yield result.written


def _exec(conn, path):
    conn.cursor().execute(path.read_text(encoding="utf-8"))


def _count(conn, query) -> int:
    with conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchone()[0]


def _validation_rows(conn, path) -> list[tuple]:
    """psycopg2 não expõe múltiplos result sets no Postgres: vai por statement."""
    rows: list[tuple] = []
    for statement in _statements(path.read_text(encoding="utf-8")):
        with conn.cursor() as cur:
            cur.execute(statement)
            if cur.description is not None:
                rows.extend(cur.fetchall())
    return rows


def _statements(sql: str) -> list[str]:
    """Suficiente para o arquivo de validação: comentários + SELECTs simples."""
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [part.strip() for part in without_comments.split(";") if part.strip()]


def _counts(conn) -> dict[str, tuple[int, int]]:
    return {
        table: (
            _count(conn, f'SELECT COUNT(*) FROM {SOURCE_SCHEMA}."{source}"'),
            _count(conn, f'SELECT COUNT(*) FROM {TARGET_SCHEMA}."{table}"'),
        )
        for table, source in SOURCE_TABLES.items()
    }


def test_tres_tabelas_relacionadas_migram_e_contagem_bate(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])

    counts = _counts(conn)
    assert counts == {
        "municipio": (2, 2),
        "bairro": (3, 3),
        "pessoa": (4, 4),
    }


def test_segunda_execucao_nao_duplica(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])
    first = _counts(conn)

    _exec(conn, generated["etl_script.sql"])
    second = _counts(conn)

    assert first == second
    assert second["pessoa"] == (4, 4)


def test_segunda_execucao_preserva_as_chaves_novas(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])
    before = _count(conn, f'SELECT COUNT(DISTINCT "id") FROM {TARGET_SCHEMA}."pessoa"')
    ids_before = {
        row[0]
        for row in _rows(conn, f'SELECT "id" FROM {TARGET_SCHEMA}."pessoa"')
    }

    _exec(conn, generated["etl_script.sql"])

    ids_after = {
        row[0] for row in _rows(conn, f'SELECT "id" FROM {TARGET_SCHEMA}."pessoa"')
    }
    assert ids_after == ids_before
    assert len(ids_after) == before == 4


def test_fk_aponta_para_o_pai_certo_depois_do_de_para(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])

    rows = _rows(
        conn,
        f"""SELECT p."nome_completo", b."nome", m."nome"
            FROM {TARGET_SCHEMA}."pessoa" p
            JOIN {TARGET_SCHEMA}."bairro" b ON b."id" = p."cod_bairro"
            JOIN {TARGET_SCHEMA}."municipio" m ON m."id" = b."cod_municipio"
            ORDER BY p."nome_completo" """,
    )
    assert rows == [
        ("Ana Maria", "Centro", "Florianópolis"),
        ("Bruno Souza", "Trindade", "Florianópolis"),
        ("Carla Dias", "Centro", "Joinville"),
    ]


def test_chave_nova_e_gerada_pelo_destino_e_nao_copiada(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])

    old_ids = {
        row[0]
        for row in _rows(conn, f'SELECT "COD_MUN" FROM {SOURCE_SCHEMA}."CAD_MUNICIPIO"')
    }
    new_ids = {
        row[0] for row in _rows(conn, f'SELECT "id" FROM {TARGET_SCHEMA}."municipio"')
    }
    assert new_ids == {1, 2}
    assert old_ids == {500, 501}


def test_transformacoes_foram_aplicadas(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])

    rows = dict(
        _rows(
            conn,
            f'SELECT "nome_completo", "cpf" FROM {TARGET_SCHEMA}."pessoa"',
        )
    )
    assert rows["Ana Maria"] == "12345678900"
    assert rows["Bruno Souza"] == "98765432100"

    nascimento = _rows(
        conn,
        f'SELECT "data_nascimento" FROM {TARGET_SCHEMA}."pessoa" '
        'WHERE "nome_completo" = \'Ana Maria\'',
    )[0][0]
    assert nascimento.isoformat() == "1980-05-10"


def test_constraints_podem_ser_reaplicadas(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])
    _exec(conn, generated["etl_constraints.sql"])
    _exec(conn, generated["etl_constraints.sql"])

    fks = _count(
        conn,
        f"""SELECT COUNT(*) FROM pg_constraint
            WHERE contype = 'f' AND connamespace = '{TARGET_SCHEMA}'::regnamespace""",
    )
    assert fks == 2


def test_validacao_do_proprio_script_confirma_a_carga(conn, generated):
    _exec(conn, generated["etl_schema.sql"])
    _exec(conn, generated["etl_script.sql"])
    _exec(conn, generated["etl_script.sql"])

    rows = _validation_rows(conn, generated["etl_validation.sql"])
    assert rows, "etl_validation.sql não devolveu nenhuma linha"

    for row in rows:
        if len(row) == 3:
            # (tabela, origem, destino): contagem tem que bater.
            assert row[1] == row[2], row
        else:
            # Contadores de órfão e de de-para não consumido.
            assert row[0] == 0, row

    for query in ORPHAN_COUNTS:
        assert _count(conn, query) == 0


def test_segunda_execucao_com_constraints_nao_falha(conn, generated):
    """A sequência completa de execução é repetível, não só a carga."""
    for _ in range(2):
        _exec(conn, generated["etl_schema.sql"])
        _exec(conn, generated["etl_script.sql"])
        _exec(conn, generated["etl_constraints.sql"])
        _exec(conn, generated["etl_validation.sql"])

    assert _counts(conn)["pessoa"] == (4, 4)


def _rows(conn, query) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()
