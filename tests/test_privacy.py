"""Fase 4 — PII nunca vira valor no prompt; backend local é regra, não dica.

O teste que importa é o de ponta a ponta do Agente 1: um banco com CPF e
nome reais, um LLM de mentira, e a asserção de que o valor real não aparece
em nenhum prompt. Os demais cobrem as peças isoladas.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.agents.agent1_discovery import DiscoveryAgent
from app.db import introspection as tools
from app.privacy.audit import (
    AUDIT_FILE,
    build_record,
    pii_summary,
    read_audit,
    resolve_approver,
    write_audit_record,
)
from app.privacy.masking import (
    CPF_PATTERN,
    EMAIL_PATTERN,
    NAME_PATTERN,
    mask_free_text,
    mask_value,
    mask_values,
    pii_signal,
)
from app.privacy.policy import (
    PIIPolicyError,
    backend_is_local,
    enforce_pii_backend_policy,
)
from app.privacy.report import (
    discovery_pii_columns,
    mask_stats,
    sanitize_discovery,
    sanitize_profile,
)
from app.schemas.profiling import ColumnProfile, DiscoveryReport, TableProfile
from support import ScriptedClient

# ----------------------------------------------------------------------
# Mascaramento de valor
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "column", "expected"),
    [
        ("123.456.789-00", "CPF", CPF_PATTERN),
        ("12345678900", "CPF", CPF_PATTERN),
        ("12.345.678/0001-90", "CNPJ", "##.###.###/####-##"),
        ("joao@prefeitura.gov.br", "EMAIL", EMAIL_PATTERN),
        ("(48) 99876-5432", "TELEFONE", "(##) #####-####"),
        ("12/03/1985", "DT_NASC", "DD/MM/AAAA"),
        ("1985-03-12", "DATA_NASCIMENTO", "AAAA-MM-DD"),
        ("88010-400", "CEP", "#####-###"),
        ("João da Silva", "NOME", NAME_PATTERN),
        ("Rua das Flores, 123", "ENDERECO", "<endereço>"),
    ],
)
def test_mask_value_usa_nome_e_forma(value, column, expected):
    assert mask_value(value, column) == expected
    assert value not in mask_value(value, column)


def test_mask_value_generico_preserva_a_forma():
    assert mask_value("SC", "UF") == "AA"
    assert mask_value("123", "CODIGO") == "###"


def test_mask_values_deduplica():
    assert mask_values(["M", "F", "M"], "SEXO") == ["A"]


def test_mask_free_text_esconde_documento_em_nota():
    nota = "titular João, CPF 123.456.789-00, e-mail joao@casa.com"
    mascarada = mask_free_text(nota)

    assert "123.456.789-00" not in mascarada
    assert "joao@casa.com" not in mascarada
    assert CPF_PATTERN in mascarada
    assert EMAIL_PATTERN in mascarada


def test_pii_signal_pega_nome_e_forma():
    assert pii_signal(["algo"], "CPF")
    assert pii_signal(["123.456.789-00"], "NUM_DOC")
    assert not pii_signal(["SC", "RS"], "UF")


def test_data_sozinha_nao_exige_backend_local_mas_ainda_e_mascarada():
    # Decisão documentada: data é dado de negócio; a máscara continua valendo.
    assert not pii_signal(["12/03/1985"], "DATA_CADASTRO")
    assert not pii_signal(["1985-03-12"], "CODIGO")
    assert mask_value("12/03/1985", "CODIGO") == "DD/MM/AAAA"
    # Data de nascimento é pega pelo nome da coluna.
    assert pii_signal(["12/03/1985"], "DT_NASC")


# ----------------------------------------------------------------------
# Política de backend local
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "base_url", "expected"),
    [
        ("ollama", None, True),
        ("local", None, True),
        ("openai", "http://localhost:11434/v1", True),
        ("openai", "http://127.0.0.1:8000/v1", True),
        ("openai", "http://192.168.0.10:8000/v1", True),
        ("openai", "http://ollama:11434/v1", True),
        ("openai", "http://vllm.local:8000/v1", True),
        ("openai", "https://api.openai.com/v1", False),
        ("openai", None, False),
        ("anthropic", None, False),
    ],
)
def test_backend_is_local(backend, base_url, expected):
    assert backend_is_local(backend, base_url) is expected


def test_sem_pii_backend_remoto_continua_permitido():
    enforce_pii_backend_policy(False, backend="openai", base_url="https://api.openai.com/v1")


def test_pii_com_backend_remoto_interrompe():
    with pytest.raises(PIIPolicyError, match="sai da rede"):
        enforce_pii_backend_policy(True, backend="openai", base_url="https://api.openai.com/v1")


def test_pii_com_backend_local_passa():
    enforce_pii_backend_policy(True, backend="local")


# ----------------------------------------------------------------------
# Máscara nos artefatos
# ----------------------------------------------------------------------


def _report_com_pii() -> DiscoveryReport:
    return DiscoveryReport(
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
                        pii=False,  # ← relatório antigo: flag ainda não marcada
                        notes="CPF do titular 123.456.789-00",
                    ),
                    ColumnProfile(
                        name="UF",
                        inferred_type="string",
                        null_rate=0.0,
                        top_values=["SC"],
                    ),
                ],
                risks=["validar CPF 123.456.789-00"],
            )
        ],
    )


def test_sanitize_discovery_mascara_valores_notas_e_riscos():
    clean = sanitize_discovery(_report_com_pii())
    serialized = clean.model_dump_json()

    assert "123.456.789-00" not in serialized
    assert clean.tables[0].columns[0].top_values == [CPF_PATTERN]
    assert clean.tables[0].columns[1].top_values == ["AA"]


def test_sanitize_discovery_esconde_nome_citado_em_nota():
    report = DiscoveryReport(
        source_dialect="firebird",
        tables=[
            TableProfile(
                table="CAD_PESSOA",
                row_count=1,
                columns=[
                    ColumnProfile(
                        name="NOME",
                        inferred_type="string",
                        null_rate=0.0,
                        top_values=["João da Silva"],
                        notes="o titular João da Silva é o responsável",
                    )
                ],
                risks=["confirmar se João da Silva aparece em outra tabela"],
            )
        ],
    )

    clean = sanitize_discovery(report)

    assert "João da Silva" not in clean.model_dump_json()
    assert clean.tables[0].columns[0].notes == "o titular <nome> é o responsável"
    assert clean.tables[0].risks == ["confirmar se <nome> aparece em outra tabela"]


def test_mask_stats_nao_altera_o_original():
    stats = {"column": "CPF", "top_values": ["123.456.789-00"]}
    masked = mask_stats(stats)

    assert stats["top_values"] == ["123.456.789-00"]
    assert masked["top_values"] == [CPF_PATTERN]


def test_discovery_pii_columns_infere_pela_forma_mesmo_sem_flag():
    report = _report_com_pii()
    assert discovery_pii_columns(report) == ["CAD_PESSOA.CPF"]


def test_sanitize_profile_mascara_a_resposta_do_modelo():
    profile = TableProfile(
        table="CAD_PESSOA",
        row_count=1,
        columns=[
            ColumnProfile(
                name="NOME",
                inferred_type="string",
                null_rate=0.0,
                top_values=["João"],
                pii=True,
                notes="nome completo",
            )
        ],
    )

    sanitize_profile(profile)

    assert profile.columns[0].top_values == [NAME_PATTERN]


# ----------------------------------------------------------------------
# Agente 1: o valor real não chega ao LLM
# ----------------------------------------------------------------------


def _legacy(monkeypatch, *, cpf="123.456.789-00", nome="João da Silva") -> None:
    monkeypatch.setattr(tools, "list_tables", lambda engine: ["CAD_PESSOA"])
    monkeypatch.setattr(
        tools,
        "get_schema",
        lambda engine, table: [
            {"name": "CPF", "type": "VARCHAR(14)", "nullable": True},
            {"name": "NOME", "type": "VARCHAR(80)", "nullable": True},
        ],
    )

    def profile(engine, table, column):
        valores = [cpf] if column == "CPF" else [nome]
        return {
            "table": table,
            "column": column,
            "row_count": 10,
            "null_rate": 0.0,
            "distinct_count": len(valores),
            "top_values": valores,
        }

    monkeypatch.setattr(tools, "profile_column", profile)


RESPOSTA_PESSOA = json.dumps(
    {
        "table": "CAD_PESSOA",
        "row_count": 10,
        "columns": [
            {
                "name": "CPF",
                "inferred_type": "string",
                "null_rate": 0.0,
                "top_values": [CPF_PATTERN],
                "pii": True,
            },
            {
                "name": "NOME",
                "inferred_type": "string",
                "null_rate": 0.0,
                "top_values": [NAME_PATTERN],
                "pii": True,
            },
        ],
        "risks": [],
    }
)


def test_agente1_mascara_antes_de_enviar(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    _legacy(monkeypatch)
    client = ScriptedClient([RESPOSTA_PESSOA])

    report = DiscoveryAgent(client).run(object(), "firebird")

    prompt = client.prompts[0]
    assert "123.456.789-00" not in prompt
    assert "João da Silva" not in prompt
    assert CPF_PATTERN in prompt
    assert report.tables[0].columns[0].top_values == [CPF_PATTERN]
    assert report.pii_columns == ["CAD_PESSOA.CPF", "CAD_PESSOA.NOME"]


def test_agente1_nao_chama_llm_quando_pii_com_backend_remoto(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    _legacy(monkeypatch)
    client = ScriptedClient([RESPOSTA_PESSOA])

    with pytest.raises(PIIPolicyError):
        DiscoveryAgent(client).run(object(), "firebird")

    assert client.prompts == []


def test_agente1_politica_de_pii_vem_antes_da_fabrica_de_llm(monkeypatch):
    """A recusa por PII não pode ser mascarada por um erro de configuração."""
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    _legacy(monkeypatch)
    chamadas: list[object] = []

    def factory():
        chamadas.append(object())
        return ScriptedClient([RESPOSTA_PESSOA])

    with pytest.raises(PIIPolicyError):
        DiscoveryAgent(llm_factory=factory).run(object(), "firebird")

    # A fábrica nem foi chamada: a política veio antes de qualquer LLM.
    assert chamadas == []


def test_agente1_sem_pii_aceita_backend_remoto(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(tools, "list_tables", lambda engine: ["CAD_UF"])
    monkeypatch.setattr(
        tools, "get_schema", lambda engine, table: [{"name": "UF", "type": "CHAR(2)"}]
    )
    monkeypatch.setattr(
        tools,
        "profile_column",
        lambda engine, table, column: {
            "table": table,
            "column": column,
            "row_count": 2,
            "null_rate": 0.0,
            "distinct_count": 2,
            "top_values": ["SC", "RS"],
        },
    )
    client = ScriptedClient(
        [json.dumps({"table": "CAD_UF", "row_count": 2, "columns": [], "risks": []})]
    )

    DiscoveryAgent(client).run(object(), "firebird")

    assert len(client.prompts) == 1


# ----------------------------------------------------------------------
# auditoria.json
# ----------------------------------------------------------------------


def test_pii_summary_monta_o_bloco_da_auditoria(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    summary = pii_summary(["CAD_PESSOA.CPF"])

    assert summary["detected"] is True
    assert summary["masked"] is True
    assert summary["llm_backend"] == "ollama"
    assert summary["llm_local"] is True


def test_auditoria_acumula_execucoes(tmp_path):
    for phase in ("discovery", "mapping"):
        record = build_record(
            project="p",
            phase=phase,
            started_at=datetime.now(UTC),
            duration_ms=1.0,
            pii=pii_summary([]),
        )
        write_audit_record(tmp_path, record)

    data = json.loads((tmp_path / AUDIT_FILE).read_text(encoding="utf-8"))
    assert [entry["phase"] for entry in data["executions"]] == ["discovery", "mapping"]
    assert all(entry["run_id"] for entry in data["executions"])
    assert read_audit(tmp_path)["executions"][0]["actor"]["user"]


def test_auditoria_inexistente_le_vazio(tmp_path):
    data = read_audit(tmp_path)

    assert data["executions"] == []
    assert data["schema_version"] == 1


def test_aprovador_vem_do_cli_depois_do_ambiente(monkeypatch):
    monkeypatch.setenv("MIGRA_APPROVER", "analista")

    assert resolve_approver("maria") == "maria"
    assert resolve_approver(None) == "analista"
