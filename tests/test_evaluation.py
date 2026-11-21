"""Harness de avaliação (Fase 5): métricas, golden set e os dois modos.

O caso sintético `eval/_example` é exercitado de ponta a ponta; os testes
puros cobrem as definições que, se erradas, fariam o número mentir — em
especial "mapeou errado" (falso positivo e negativo ao mesmo tempo) e
"deixou sem origem quando devia mapear".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.evaluation import (
    GoldenError,
    evaluate_case,
    load_case,
    scaffold_case,
    write_reports,
)
from app.evaluation.golden import (
    DISCOVERY_FILE,
    GOLDEN_FILE,
    SCHEMA_FILE,
    GoldenCase,
    GoldenColumn,
    GoldenMapping,
)
from app.evaluation.metrics import (
    MISSED,
    SPURIOUS,
    WRONG_SOURCE,
    evaluate_discovery,
    evaluate_mapping,
    type_family,
)
from app.schemas.mapping import (
    Confidence,
    MappingDocument,
    MappingEntry,
    Transformation,
)
from app.schemas.profiling import ColumnProfile, DiscoveryReport, TableProfile
from support import ScriptedClient
from tests.conftest import make_discovery, make_schema

EXAMPLE_CASE = Path(__file__).resolve().parents[1] / "eval" / "_example"

# Mapeamento manual completo do schema de `make_schema()`.
ROWS = [
    ("pessoa", "id", "CAD_PESSOA", "COD_PESSOA"),
    ("pessoa", "cpf", "CAD_PESSOA", "CPF"),
    ("pessoa", "nome_completo", "CAD_PESSOA", "NOME"),
    ("pessoa", "data_nascimento", "CAD_PESSOA", "DT_NASC"),
    ("pessoa", "cod_bairro", "CAD_PESSOA", "COD_BAIRRO"),
    ("bairro", "id", "CAD_BAIRRO", "COD_BAIRRO"),
    ("bairro", "nome", "CAD_BAIRRO", "NOME_BAIRRO"),
    ("bairro", "cod_municipio", "CAD_BAIRRO", "COD_MUN"),
    ("municipio", "id", "CAD_MUNICIPIO", "COD_MUN"),
    ("municipio", "nome", "CAD_MUNICIPIO", "NOME_MUN"),
    ("municipio", "uf", "CAD_MUNICIPIO", "UF"),
    ("telefone", "id", "CAD_TELEFONE", "COD_TELEFONE"),
    ("telefone", "numero", "CAD_TELEFONE", "NUMERO"),
    ("telefone", "cod_pessoa", "CAD_TELEFONE", "COD_PESSOA"),
]


def _rows(**overrides) -> list[dict]:
    rows = []
    for table, field, source_table, source_field in ROWS:
        row = {
            "target_table": table,
            "target_field": field,
            "source_table": source_table,
            "source_field": source_field,
        }
        key = f"{table}.{field}"
        if key in overrides:
            row.update(overrides[key])
        rows.append(row)
    return rows


def _document(rows=None, *, transformations=None) -> MappingDocument:
    transformations = transformations or {}
    entries = [
        MappingEntry(
            target_table=table,
            target_field=field,
            source_table=source_table,
            source_field=source_field,
            transformation=transformations.get(f"{table}.{field}"),
            confidence=Confidence.high,
            requires_human_review=False,
        )
        for table, field, source_table, source_field in (ROWS if rows is None else rows)
    ]
    return MappingDocument(mapping_id="teste", project="teste", entries=entries)


def _golden_mapping(rows=None) -> list[GoldenMapping]:
    return [GoldenMapping(**row) for row in (rows or _rows())]


def _write_case(
    base: Path,
    *,
    rows=None,
    schema=None,
    discovery=None,
    expectations=None,
    case: str = "caso",
    **extra,
) -> Path:
    schema = schema or make_schema()
    discovery = discovery or make_discovery()
    data = {
        "case": case,
        "description": "caso de teste",
        "synthetic": True,
        "source_dialect": discovery.source_dialect,
        "target_schema_version": schema.version,
        "mapping": rows if rows is not None else _rows(),
        "discovery": expectations or [],
        **extra,
    }
    base.mkdir(parents=True, exist_ok=True)
    (base / SCHEMA_FILE).write_text(
        yaml.safe_dump(schema.model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (base / DISCOVERY_FILE).write_text(
        discovery.model_dump_json(indent=2), encoding="utf-8"
    )
    (base / GOLDEN_FILE).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return base


def _response_for(case, table: str) -> str:
    entries: list[dict] = []
    unmapped: list[str] = []
    for row in case.golden.mapping:
        if row.target_table != table:
            continue
        if row.source_table is None:
            unmapped.append(f"{table}.{row.target_field}")
            continue
        entry = {
            "target_table": table,
            "target_field": row.target_field,
            "source_table": row.source_table,
            "source_field": row.source_field,
            "confidence": "high",
            "requires_human_review": False,
        }
        if "transformation" in row.model_fields_set and row.transformation:
            entry["transformation"] = {"type": row.transformation, "params": {}}
        entries.append(entry)
    return json.dumps({"entries": entries, "unmapped_target_fields": unmapped})


# ----------------------------------------------------------------------
# Métricas de mapeamento
# ----------------------------------------------------------------------


def test_mapeamento_perfeito():
    result = evaluate_mapping(_golden_mapping(), _document())

    assert result["metrics"]["precision"] == 1.0
    assert result["metrics"]["recall"] == 1.0
    assert result["metrics"]["f1"] == 1.0
    assert result["metrics"]["exact_accuracy"] == 1.0
    assert result["errors"] == []


def test_origem_errada_conta_como_falso_positivo_e_falso_negativo():
    rows = _rows(**{"pessoa.cpf": {"source_field": "NUM_DOC"}})
    result = evaluate_mapping(_golden_mapping(rows), _document())

    metrics = result["metrics"]
    assert metrics["tp"] == len(ROWS) - 1
    assert metrics["fp"] == 1
    assert metrics["fn"] == 1
    assert metrics["precision"] < 1.0
    assert metrics["recall"] < 1.0
    assert result["errors"][0]["kind"] == WRONG_SOURCE


def test_faltou_mapear_e_mapeou_o_que_devia_ficar_sem_origem():
    golden = _golden_mapping()
    # O golden diz que pessoa.id fica sem origem; o documento mapeia (spurious).
    golden[0] = GoldenMapping(target_table="pessoa", target_field="id")
    # O golden mapeia telefone.numero; o documento omite (missed).
    rows = [row for row in ROWS if (row[0], row[1]) != ("telefone", "numero")]
    result = evaluate_mapping(golden, _document(rows))

    kinds = {error["kind"] for error in result["errors"]}
    assert kinds == {MISSED, SPURIOUS}
    assert result["metrics"]["unmapped"]["accuracy"] == 0.0
    assert result["metrics"]["exact_accuracy"] < 1.0


def test_deixar_sem_origem_quando_o_golden_tambem_deixa():
    golden = _golden_mapping()
    golden[0] = GoldenMapping(target_table="pessoa", target_field="id")
    rows = [(t, f, st, sf) for t, f, st, sf in ROWS if (t, f) != ("pessoa", "id")]
    result = evaluate_mapping(golden, _document(rows))

    metrics = result["metrics"]
    assert metrics["exact_accuracy"] == 1.0
    assert metrics["unmapped"]["accuracy"] == 1.0
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert result["errors"] == []


def test_tudo_unmapped_nao_divide_por_zero():
    golden = [GoldenMapping(target_table="pessoa", target_field="id")]
    result = evaluate_mapping(golden, _document([]))

    metrics = result["metrics"]
    assert metrics["precision"] is None
    assert metrics["recall"] is None
    assert metrics["f1"] is None
    assert metrics["exact_accuracy"] == 1.0
    assert metrics["unmapped"]["accuracy"] == 1.0


def test_campo_fora_do_golden_e_falso_positivo():
    golden = _golden_mapping(_rows()[:-1])
    result = evaluate_mapping(golden, _document())

    assert result["unknown_predictions"]
    assert result["unknown_predictions"][0]["kind"] == "unknown_field"
    assert result["metrics"]["fp"] == 1


def test_transformacao_avaliada_so_quando_o_golden_pede():
    rows = _rows(**{"pessoa.data_nascimento": {"transformation": "parse_date_br"}})
    golden = _golden_mapping(rows)
    mapping = _document(
        ROWS,
        transformations={"pessoa.data_nascimento": Transformation(type="parse_date_br")},
    )

    result = evaluate_mapping(golden, mapping)
    assert result["transformations"]["evaluated"] == 1
    assert result["transformations"]["accuracy"] == 1.0

    wrong = _document(
        ROWS,
        transformations={"pessoa.data_nascimento": Transformation(type="regex_extract_digits")},
    )
    result = evaluate_mapping(golden, wrong)
    assert result["transformations"]["accuracy"] == 0.0
    assert result["transformations"]["errors"][0]["target"] == "pessoa.data_nascimento"


def test_sem_expectativa_de_transformacao_nada_e_avaliado():
    result = evaluate_mapping(_golden_mapping(), _document())

    assert result["transformations"]["evaluated"] == 0
    assert result["transformations"]["accuracy"] is None


# ----------------------------------------------------------------------
# Métricas de discovery
# ----------------------------------------------------------------------


def _discovery_report(*, cpf_pii: bool, cpf_type: str = "string") -> DiscoveryReport:
    return DiscoveryReport(
        source_dialect="firebird",
        tables=[
            TableProfile(
                table="CAD_PESSOA",
                row_count=10,
                columns=[
                    ColumnProfile(
                        name="CPF",
                        inferred_type=cpf_type,
                        null_rate=0.0,
                        pii=cpf_pii,
                    ),
                    ColumnProfile(
                        name="NOME",
                        inferred_type="string",
                        null_rate=0.0,
                        pii=True,
                    ),
                ],
            )
        ],
    )


def test_pii_falso_negativo_e_listado_como_erro_caro():
    golden = [
        GoldenColumn(table="CAD_PESSOA", column="CPF", inferred_type="string", pii=True),
        GoldenColumn(table="CAD_PESSOA", column="NOME", inferred_type="string", pii=True),
    ]
    result = evaluate_discovery(golden, _discovery_report(cpf_pii=False))

    assert result["pii"]["tp"] == 1
    assert result["pii"]["fn"] == 1
    assert result["pii"]["recall"] == 0.5
    assert result["pii"]["false_negatives"][0]["column"] == "CAD_PESSOA.CPF"


def test_tipo_compara_por_familia():
    golden = [
        GoldenColumn(table="CAD_PESSOA", column="CPF", inferred_type="varchar", pii=None)
    ]
    result = evaluate_discovery(golden, _discovery_report(cpf_pii=False))

    assert result["type"]["accuracy"] == 1.0
    assert type_family("VARCHAR(14)") == "string"
    assert type_family("int") == type_family("integer")
    assert type_family("timestamp") != type_family("date")


def test_coluna_ausente_no_relatorio_aparece():
    golden = [
        GoldenColumn(table="CAD_PESSOA", column="NAO_PERFILADA", pii=True)
    ]
    result = evaluate_discovery(golden, _discovery_report(cpf_pii=True))

    assert result["missing"] == ["CAD_PESSOA.NAO_PERFILADA"]
    assert result["pii"]["fn"] == 1
    assert result["pii"]["false_negatives"][0]["predicted"] is None


# ----------------------------------------------------------------------
# Carregamento do caso
# ----------------------------------------------------------------------


def test_load_case_do_exemplo():
    case = load_case(EXAMPLE_CASE)

    assert case.name == "_example"
    assert case.golden.synthetic is True
    assert len(case.golden.mapping) == len(case.schema_fields())
    assert case.discovery.source_dialect == "firebird"


def test_golden_incompleto_nao_pode_esconder_campo_nao_avaliado(tmp_path):
    rows = [row for row in _rows() if row["target_table"] != "telefone"]
    _write_case(tmp_path / "caso", rows=rows)

    with pytest.raises(GoldenError, match="não cobre todos os campos"):
        load_case(tmp_path / "caso")


def test_golden_com_campo_inexistente_no_schema(tmp_path):
    rows = _rows() + [
        {
            "target_table": "pessoa",
            "target_field": "fantasma",
            "source_table": "X",
            "source_field": "Y",
        }
    ]
    _write_case(tmp_path / "caso", rows=rows)

    with pytest.raises(GoldenError, match="não existe no target_schema"):
        load_case(tmp_path / "caso")


def test_golden_com_transformacao_desconhecida(tmp_path):
    rows = _rows(**{"pessoa.cpf": {"transformation": "magica"}})
    _write_case(tmp_path / "caso", rows=rows)

    with pytest.raises(GoldenError, match="transformação desconhecida"):
        load_case(tmp_path / "caso")


def test_golden_com_dialeto_incoerente(tmp_path):
    _write_case(tmp_path / "caso", source_dialect="firebird")

    with pytest.raises(GoldenError, match="source_dialect"):
        load_case(tmp_path / "caso")


def test_caso_sem_arquivos_falha_cedo(tmp_path):
    with pytest.raises(GoldenError, match="caso incompleto"):
        load_case(tmp_path / "vazio")


def test_caso_com_discovery_corrompido_falha_com_erro_limpo(tmp_path):
    base = _write_case(tmp_path / "caso")
    (base / DISCOVERY_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(GoldenError, match="discovery inválido"):
        load_case(base)


def test_caso_com_target_schema_corrompido_falha_com_erro_limpo(tmp_path):
    base = _write_case(tmp_path / "caso")
    (base / SCHEMA_FILE).write_text("[tables: ", encoding="utf-8")

    with pytest.raises(GoldenError):
        load_case(base)


# ----------------------------------------------------------------------
# Modos de execução
# ----------------------------------------------------------------------


def test_offline_gera_relatorios_json_e_markdown(tmp_path):
    case = load_case(_write_case(tmp_path / "caso"))

    result = evaluate_case(case, mapping=_document())
    json_path, md_path = write_reports(result, tmp_path / "saida")

    assert result["mode"] == "offline"
    assert result["mapping"]["summary"]["precision"] == 1.0

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["case"] == "caso"
    assert data["mapping"]["runs"][0]["metrics"]["errors"] == []
    assert "Avaliação — caso" in md_path.read_text(encoding="utf-8")


def test_offline_recusa_repeats_maior_que_um(tmp_path):
    case = load_case(_write_case(tmp_path / "caso"))

    with pytest.raises(ValueError, match="modo live"):
        evaluate_case(case, mapping=_document(), repeats=2)


def test_live_roda_o_agente_contra_o_discovery_congelado(tmp_path):
    case = load_case(_write_case(tmp_path / "caso"))
    responses = [_response_for(case, table) for table in case.target_schema.tables]
    client = ScriptedClient(responses)

    result = evaluate_case(case, llm=client, repeats=1)

    assert result["mode"] == "live"
    assert len(client.prompts) == len(case.target_schema.tables)
    assert result["mapping"]["summary"]["precision"] == 1.0
    assert result["mapping"]["consistency"]["evaluated"] is False


def test_repeats_medem_consistencia(tmp_path):
    case = load_case(_write_case(tmp_path / "caso"))
    good = [_response_for(case, table) for table in case.target_schema.tables]
    bad = json.loads(good[0])
    bad["entries"][0]["source_field"] = "OUTRA_COLUNA"
    client = ScriptedClient(good + [json.dumps(bad)] + good[1:])

    result = evaluate_case(case, llm=client, repeats=2)

    consistency = result["mapping"]["consistency"]
    assert consistency["evaluated"] is True
    assert consistency["agreement"] < 1.0
    assert consistency["unstable"]


def test_avaliacao_live_nao_deixa_valor_real_no_prompt(tmp_path):
    discovery = DiscoveryReport(
        source_dialect="firebird",
        tables=[
            TableProfile(
                table="CAD_PESSOA",
                row_count=1,
                columns=[
                    ColumnProfile(
                        name="CPF",
                        inferred_type="string",
                        null_rate=0.0,
                        top_values=["123.456.789-00"],
                        pii=True,
                    )
                ],
            )
        ],
    )
    case = load_case(_write_case(tmp_path / "caso", discovery=discovery))
    client = ScriptedClient(
        [_response_for(case, table) for table in case.target_schema.tables]
    )

    evaluate_case(case, llm=client)

    assert "123.456.789-00" not in " ".join(client.prompts)


# ----------------------------------------------------------------------
# eval-init / scaffold
# ----------------------------------------------------------------------


def test_scaffold_cria_rascunho_com_todos_os_campos(tmp_path):
    discovery = DiscoveryReport(
        source_dialect="firebird",
        tables=[
            TableProfile(
                table="CAD_PESSOA",
                row_count=10,
                columns=[
                    ColumnProfile(
                        name="CPF",
                        inferred_type="string",
                        null_rate=0.0,
                        top_values=["123.456.789-00"],
                        pii=True,
                    )
                ],
            )
        ],
    )
    schema_path = tmp_path / "origem" / SCHEMA_FILE
    schema_path.parent.mkdir()
    schema_path.write_text(
        yaml.safe_dump(make_schema().model_dump(), sort_keys=False), encoding="utf-8"
    )
    discovery_path = tmp_path / "origem" / DISCOVERY_FILE
    discovery_path.write_text(discovery.model_dump_json(indent=2), encoding="utf-8")
    mapping_path = tmp_path / "origem" / "mapping.json"
    mapping_path.write_text(_document().model_dump_json(indent=2), encoding="utf-8")

    golden_path = scaffold_case(
        tmp_path / "caso",
        schema_path=schema_path,
        discovery_path=discovery_path,
        mapping_path=mapping_path,
        project="cliente",
    )

    assert golden_path.exists()
    case = load_case(tmp_path / "caso")
    assert len(case.golden.mapping) == len(case.schema_fields())
    assert case.golden.synthetic is False
    assert case.golden.source_dialect == "firebird"
    # A cópia congelada do discovery não leva valor real.
    assert "123.456.789-00" not in (tmp_path / "caso" / DISCOVERY_FILE).read_text(
        encoding="utf-8"
    )


def test_scaffold_nao_sobrescreve_sem_force(tmp_path):
    schema_path = tmp_path / "origem" / SCHEMA_FILE
    schema_path.parent.mkdir()
    schema_path.write_text(
        yaml.safe_dump(make_schema().model_dump(), sort_keys=False), encoding="utf-8"
    )
    discovery_path = tmp_path / "origem" / DISCOVERY_FILE
    discovery_path.write_text(make_discovery().model_dump_json(indent=2), encoding="utf-8")
    mapping_path = tmp_path / "origem" / "mapping.json"
    mapping_path.write_text(_document().model_dump_json(indent=2), encoding="utf-8")

    kwargs = {
        "schema_path": schema_path,
        "discovery_path": discovery_path,
        "mapping_path": mapping_path,
        "project": "cliente",
    }
    scaffold_case(tmp_path / "caso", **kwargs)

    with pytest.raises(GoldenError, match="já existe"):
        scaffold_case(tmp_path / "caso", **kwargs)

    assert scaffold_case(tmp_path / "caso", force=True, **kwargs)


def test_golden_case_recusa_coluna_repetida():
    with pytest.raises(ValueError, match="coluna repetida"):
        GoldenCase(
            case="x",
            mapping=[],
            discovery=[
                GoldenColumn(table="T", column="A"),
                GoldenColumn(table="T", column="A"),
            ],
        )
