"""SQL gerado: ordem, transação, idempotência por dialeto e validação.

Os testes olham o texto porque é isso que o cliente vai executar — não há
banco em jogo aqui. A prova contra Postgres real está em
`test_integration_postgres.py`.
"""
from __future__ import annotations

from app.config import ETLConfig
from app.etl import generator
from app.etl.plan import build_plan
from tests.conftest import make_discovery, make_mapping, make_schema


def _artifacts(target: str, *, etl: ETLConfig | None = None, source: str = "postgres"):
    schema = make_schema()
    mapping = make_mapping()
    plan = build_plan(
        mapping=mapping,
        target_schema=schema,
        source_dialect=source,
        target_dialect=target,
        source_schema="legado",
        target_schema_name="public",
        discovery=make_discovery(),
        etl=etl,
    )
    return generator.generate(plan, schema, mapping), plan


# ----------------------------------------------------------------------
# Ordem e estrutura
# ----------------------------------------------------------------------
def test_carga_segue_a_ordem_de_dependencia_e_uma_transacao_por_tabela():
    art, plan = _artifacts("postgres")
    load = art.load_sql
    assert [step.table for step in plan.steps] == ["municipio", "bairro", "pessoa"]
    assert load.count("BEGIN;") == 3
    assert load.count("COMMIT;") == 3
    assert load.index('"public"."municipio"') < load.index('"public"."bairro"')
    assert load.index('"public"."bairro"') < load.index('"public"."pessoa"')


def test_schema_cria_sequences_e_pk_com_default_antes_da_carga():
    art, _ = _artifacts("postgres")
    schema = art.schema_sql
    assert schema.count("CREATE SEQUENCE IF NOT EXISTS") == 3
    assert 'DEFAULT nextval(\'public.seq_pessoa\')' in schema
    # DEFAULT de sequence só faz sentido na PK; as demais colunas não têm.
    assert schema.count("DEFAULT nextval") == 3


def test_todos_os_artefatos_esperados_sao_gerados():
    art, _ = _artifacts("postgres")
    assert set(art.files()) == {
        "etl_schema.sql",
        "etl_script.sql",
        "etl_constraints.sql",
        "etl_validation.sql",
        "relatorio.md",
    }


def test_fk_nao_e_criada_no_schema_e_sim_depois_da_carga():
    art, _ = _artifacts("postgres")
    assert "FOREIGN KEY" not in art.schema_sql
    assert "FOREIGN KEY" in art.constraints_sql


# ----------------------------------------------------------------------
# Idempotência: a segunda execução não duplica
# ----------------------------------------------------------------------
def test_postgres_usa_on_conflict_e_filtra_o_de_para_ja_existente():
    art, _ = _artifacts("postgres")
    assert art.load_sql.count("ON CONFLICT DO NOTHING") == 3
    assert art.load_sql.count("AND NOT EXISTS (SELECT 1 FROM") == 3


def test_sqlserver_usa_where_not_exists_e_nao_tem_sintaxe_de_postgres():
    art, _ = _artifacts("sqlserver")
    load = art.load_sql
    assert "ON CONFLICT" not in load
    assert "WHERE NOT EXISTS (SELECT 1 FROM [public].[municipio] t" in load
    assert "BEGIN TRANSACTION;" in load
    # 3 INSERTs de carga filtrados pelo alvo + 3 de-para filtrados por si mesmos.
    assert load.count("WHERE NOT EXISTS (SELECT 1 FROM [public].") == 3
    assert load.count("AND NOT EXISTS (SELECT 1 FROM [public].[_map_") == 3


def test_firebird_usa_update_or_insert_e_nao_on_conflict():
    art, _ = _artifacts("firebird")
    load = art.load_sql
    assert "ON CONFLICT" not in load
    assert load.count("UPDATE OR INSERT INTO") == 3
    assert "NEXT VALUE FOR" in load


def test_firebird_confirma_ddl_antes_do_dml():
    art, _ = _artifacts("firebird")
    load = art.load_sql
    # A tabela de-para é criada e confirmada antes do INSERT de carga.
    create_at = load.index('"_map_municipio"')
    commit_after_create = load.index("COMMIT;", create_at)
    insert_at = load.index('INSERT INTO "public"."_map_municipio"')
    assert create_at < commit_after_create < insert_at


def test_idempotencia_desligada_avisa_e_nao_emite_on_conflict():
    art, _ = _artifacts("postgres", etl=ETLConfig(idempotent=False))
    assert "ON CONFLICT" not in art.load_sql
    assert "duplica os dados" in art.load_sql


# ----------------------------------------------------------------------
# De-para
# ----------------------------------------------------------------------
def test_de_para_guarda_chave_antiga_e_nova_por_tabela():
    art, _ = _artifacts("postgres")
    assert 'INSERT INTO "public"."_map_bairro" ("old_key", "new_id")' in art.load_sql
    # A PK do destino vem do de-para, não da origem.
    assert 'm_self."new_id" AS "id"' in art.load_sql
    # A FK é traduzida pelo de-para do pai.
    assert 'm_cod_bairro."new_id" AS "cod_bairro"' in art.load_sql


def test_toda_tabela_com_pk_gerada_tem_de_para():
    """Mesmo sem ninguém referenciá-la, o de-para mantém a chave estável
    entre execuções — é o que torna a segunda rodada previsível."""
    art, plan = _artifacts("postgres")
    assert all(step.map_table for step in plan.steps if step.sequence)
    assert 'INSERT INTO "public"."_map_pessoa"' in art.load_sql


# ----------------------------------------------------------------------
# Constraints e validação
# ----------------------------------------------------------------------
def test_constraints_sao_reexecutaveis_nos_tres_dialetos():
    assert "pg_constraint WHERE conname" in _artifacts("postgres")[0].constraints_sql
    assert "sys.foreign_keys" in _artifacts("sqlserver")[0].constraints_sql
    assert "RDB$RELATION_CONSTRAINTS" in _artifacts("firebird")[0].constraints_sql


def test_validacao_compara_contagem_de_origem_e_destino():
    art, _ = _artifacts("postgres")
    sql = art.validation_sql
    assert sql.count("AS tabela") == 3
    for table in ("municipio", "bairro", "pessoa"):
        assert f"'{table}' AS tabela" in sql
    assert 'FROM "legado"."CAD_PESSOA"' in sql
    assert 'FROM "public"."pessoa"' in sql


def test_validacao_conta_orfao_de_fk():
    art, _ = _artifacts("postgres")
    assert "orfaos_pessoa_cod_bairro" in art.validation_sql


def test_validacao_aponta_de_para_nao_consumido():
    art, _ = _artifacts("postgres")
    assert "de_para_nao_consumido_municipio" in art.validation_sql


# ----------------------------------------------------------------------
# Schema de destino
# ----------------------------------------------------------------------
def test_origem_sqlserver_declara_a_funcao_como_pre_requisito():
    """A função é avaliada na origem, não no destino: o script só a declara."""
    art, _ = _artifacts("sqlserver", source="sqlserver")
    assert "dbo.fn_strip_nondigits(s.[CPF])" in art.load_sql
    assert "PRÉ-REQUISITO NA ORIGEM" in art.load_sql
    assert "CREATE OR ALTER FUNCTION dbo.fn_strip_nondigits" in art.load_sql
    assert "CREATE OR ALTER FUNCTION" not in art.schema_sql
    assert "Pré-requisitos na origem" in art.report_md


def test_origem_postgres_nao_declara_funcao_auxiliar():
    art, _ = _artifacts("postgres", source="postgres")
    assert "fn_strip_nondigits" not in art.load_sql
    assert "Pré-requisitos" not in art.report_md


def test_tipo_string_sem_largura_vira_texto_no_postgres():
    art, _ = _artifacts("postgres")
    assert '"nome" TEXT NOT NULL' in art.schema_sql
    assert '"uf" VARCHAR(2)' in art.schema_sql


# ----------------------------------------------------------------------
# Relatório
# ----------------------------------------------------------------------
def test_relatorio_lista_ordem_pendencias_e_avisos():
    art, _ = _artifacts("postgres")
    report = art.report_md
    assert "municipio -> bairro -> pessoa" in report
    assert "telefone.numero" in report
    assert "Pendências" in report
    assert "etl_validation.sql" in report


def test_relatorio_nao_promete_execucao_automatica():
    art, _ = _artifacts("postgres")
    assert "mesma conexão" in art.report_md


def test_pk_sem_de_para_mantem_default_e_omite_a_coluna_do_insert():
    """Sem chave antiga não há de-para, mas o banco ainda precisa gerar a PK."""
    from app.schemas.mapping import ReviewStatus

    schema = make_schema()
    mapping = make_mapping()
    mapping.entries = [
        entry for entry in mapping.entries if entry.target_table == "telefone"
    ]
    mapping.entries[0].status = ReviewStatus.approved

    plan = build_plan(
        mapping=mapping,
        target_schema=schema,
        source_dialect="postgres",
        target_dialect="postgres",
        source_schema="legado",
        target_schema_name="public",
    )
    art = generator.generate(plan, schema, mapping)

    assert '"id" BIGINT DEFAULT nextval(\'public.seq_telefone\') NOT NULL' in art.schema_sql
    assert 'INSERT INTO "public"."telefone" ("numero")' in art.load_sql
    assert "_map_telefone" not in art.load_sql


# ----------------------------------------------------------------------
# Schema de destino gerenciado fora do pipeline
# ----------------------------------------------------------------------
def test_create_schema_desligado_nao_emite_ddl_mas_declara_o_que_assume():
    art, _ = _artifacts("postgres", etl=ETLConfig(create_schema=False))

    assert "CREATE TABLE" not in art.schema_sql
    assert "CREATE SEQUENCE" not in art.schema_sql
    assert "--   \"public\".\"municipio\"" in art.schema_sql
    assert "--   \"public\".\"seq_municipio\"" in art.schema_sql
    # A carga continua igual: quem cria o schema é outro time, não o script.
    assert 'INSERT INTO "public"."municipio"' in art.load_sql
    assert "create_schema: false" in art.report_md


def test_validacao_acusa_fk_da_origem_sem_de_para():
    """LEFT JOIN grava NULL silencioso; só a checagem na origem revela."""
    art, _ = _artifacts("postgres")

    assert "fk_orfa_na_origem_pessoa_cod_bairro" in art.validation_sql
    assert 's."COD_BAIRRO"' in art.validation_sql
    assert "_map_bairro" in art.validation_sql
