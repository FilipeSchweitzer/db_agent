"""Transformações: contrato de expressão de uma coluna só.

O que importa aqui não é a sintaxe bonita do CASE, é que o SQL gerado seja
executável e que valor desconhecido não vire NULL em silêncio.
"""
from __future__ import annotations

import pytest

from app.transforms.registry import (
    TRANSFORM_REGISTRY,
    available_transform_names,
    render_sql,
)


def test_map_enum_gera_case_com_else_preservando_o_valor():
    sql = render_sql(
        "map_enum",
        "postgres",
        's."SEXO"',
        {"mapping": {"M": "masculino", "F": "feminino"}},
    )
    assert sql == (
        """CASE s."SEXO" WHEN 'M' THEN 'masculino' WHEN 'F' THEN 'feminino' """
        """ELSE s."SEXO" END"""
    )


def test_map_enum_escapa_apostrofo_no_literal():
    sql = render_sql("map_enum", "postgres", "c", {"mapping": {"O'Brien": "x"}})
    assert "WHEN 'O''Brien' THEN 'x'" in sql


def test_map_enum_aceita_todas_as_mesmas_chaves_nos_tres_dialetos():
    for dialect in ("postgres", "sqlserver", "firebird"):
        sql = render_sql("map_enum", dialect, "c", {"mapping": {"1": "a"}})
        assert sql.startswith("CASE c WHEN '1' THEN 'a'")
        assert sql.endswith("END")


@pytest.mark.parametrize("params", [{}, {"mapping": {}}, {"mapping": None}])
def test_map_enum_sem_mapa_falha_alto(params):
    with pytest.raises(ValueError, match="map_enum"):
        render_sql("map_enum", "postgres", "c", params)


def test_transformacao_desconhecida_lista_as_disponiveis():
    with pytest.raises(ValueError, match="regex_extract_digits"):
        render_sql("remover_acentos", "postgres", "c")


def test_dialeto_sem_template_falha_alto():
    with pytest.raises(ValueError, match="oracle"):
        render_sql("parse_date_br", "oracle", "c")


def test_regex_extract_digits_nao_depende_de_udf_no_firebird():
    """Firebird 5 não tem REGEXP_REPLACE — cadeia de REPLACE é o que roda."""
    sql = render_sql("regex_extract_digits", "firebird", "s.\"CPF\"")
    assert "REPLACE" in sql
    assert "REGEXP_REPLACE" not in sql


def test_sqlserver_usa_funcao_que_o_script_de_schema_cria():
    sql = render_sql("regex_extract_digits", "sqlserver", "s.[CPF]")
    assert sql == "dbo.fn_strip_nondigits(s.[CPF])"


def test_parse_date_br_firebird_monta_data_iso():
    sql = render_sql("parse_date_br", "firebird", "s.\"DT_NASC\"")
    assert "SUBSTRING" in sql
    assert "TO_DATE" not in sql


def test_parse_date_br_sqlserver_usa_convert():
    sql = render_sql("parse_date_br", "sqlserver", "s.[DT_NASC]")
    assert sql == "CONVERT(date, s.[DT_NASC], 103)"


def test_split_nome_completo_foi_removida_do_registro():
    """Produzir duas colunas não cabe no contrato entrada -> uma coluna."""
    assert "split_nome_completo" not in TRANSFORM_REGISTRY


def test_registro_nao_tem_engine_python_ou_python_fn():
    for spec in TRANSFORM_REGISTRY.values():
        assert not hasattr(spec, "python_fn")


def test_toda_transformacao_tem_os_tres_dialetos_de_origem():
    """Cada transformação deve ser renderizável nos 3 dialetos de origem."""
    for name in available_transform_names():
        for dialect in ("postgres", "sqlserver", "firebird"):
            # Não deve levantar ValueError de dialeto não suportado
            params = {"mapping": {"a": "b"}} if name == "map_enum" else None
            sql = render_sql(name, dialect, "col", params)
            assert isinstance(sql, str) and sql.strip()


def test_render_sql_aceita_colunas_citadas():
    """Coluna já citada (s.\"CPF\") passa direto para o template."""
    for dialect in ("postgres", "sqlserver", "firebird"):
        sql = render_sql("regex_extract_digits", dialect, 's."CPF"')
        assert "CPF" in sql


def test_render_sql_falha_em_transformacao_desconhecida():
    with pytest.raises(ValueError, match="Transformação desconhecida"):
        render_sql("nao_existe", "postgres", "col")