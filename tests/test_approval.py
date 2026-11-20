"""Aprovação: o script tem que sair do mapeamento que foi revisado.

O buraco real: `map` grava `mapping.json` e a planilha; se o `map` rodar de
novo entre a revisão e o `generate`, a planilha revisada descreve um
mapeamento que não existe mais. Aqui isso deixa de ser silencioso.
"""
from __future__ import annotations

import json

import pytest

from app.review.approval import (
    APPROVAL_FILE,
    MAPPING_FILE,
    ApprovalError,
    check_schema_compatibility,
    file_sha256,
    review_fingerprint,
    verify_approval,
    write_approval,
)
from tests.conftest import make_mapping


def _prepare(tmp_path, mapping):
    mapping_path = tmp_path / MAPPING_FILE
    mapping_path.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")
    write_approval(
        tmp_path,
        project=mapping.project,
        mapping_id=mapping.mapping_id,
        target_schema_version="test.1",
        entries=len(mapping.entries),
    )
    return mapping_path


def test_approval_guarda_hash_e_metadados(tmp_path):
    mapping = make_mapping()
    _prepare(tmp_path, mapping)

    record = json.loads((tmp_path / APPROVAL_FILE).read_text())
    assert record["mapping_sha256"] == file_sha256(tmp_path / MAPPING_FILE)
    assert record["mapping_id"] == mapping.mapping_id
    assert record["entries"] == len(mapping.entries)
    assert record["target_schema_version"] == "test.1"
    assert record["created_at"]


def test_verify_aprova_quando_nada_mudou(tmp_path):
    mapping = make_mapping()
    _prepare(tmp_path, mapping)
    assert verify_approval(tmp_path)["mapping_id"] == mapping.mapping_id


def test_mapping_regerado_invalida_a_revisao(tmp_path):
    mapping = make_mapping()
    mapping_path = _prepare(tmp_path, mapping)

    mapping.entries[0].source_field = "OUTRO_CAMPO"
    mapping_path.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(ApprovalError, match="mudou desde que a planilha"):
        verify_approval(tmp_path)


def test_sem_approval_nao_da_para_gerar(tmp_path):
    (tmp_path / MAPPING_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(ApprovalError, match="não há registro"):
        verify_approval(tmp_path)


def test_sem_mapping_nao_da_para_gerar(tmp_path):
    with pytest.raises(ApprovalError, match="rode 'map'"):
        verify_approval(tmp_path)


def test_write_approval_sem_mapping_falha_alto(tmp_path):
    with pytest.raises(FileNotFoundError, match="rode 'map'"):
        write_approval(
            tmp_path,
            project="p",
            mapping_id="m",
            target_schema_version="1",
            entries=0,
        )


def test_fingerprint_muda_quando_o_status_muda():
    mapping = make_mapping()
    before = review_fingerprint(mapping)
    mapping.entries[0].status = "rejected"
    assert review_fingerprint(mapping) != before


def test_fingerprint_ignora_ordem_das_entradas():
    mapping = make_mapping()
    before = review_fingerprint(mapping)
    mapping.entries.reverse()
    assert review_fingerprint(mapping) == before


# ----------------------------------------------------------------------
# Compatibilidade de versão do target_schema (Fase 4)
# ----------------------------------------------------------------------


def test_schema_compativel_quando_a_versao_bate():
    result = check_schema_compatibility("2026.1", "2026.1")
    assert result["compatible"] is True


def test_schema_incompativel_interrompe():
    with pytest.raises(ApprovalError, match="target_schema.yaml mudou"):
        check_schema_compatibility("2026.1", "2026.2")


def test_force_registra_a_incompatibilidade_em_vez_de_esconder():
    result = check_schema_compatibility("2026.1", "2026.2", force=True)
    assert result["compatible"] is False
    assert result["mapping_version"] == "2026.1"
    assert result["schema_version"] == "2026.2"


def test_mapping_sem_versao_cai_para_o_approval():
    result = check_schema_compatibility(
        None, "2026.1", {"target_schema_version": "2026.1"}
    )
    assert result["compatible"] is True


def test_sem_versao_em_lugar_nenhum_nao_inventa_veredito():
    result = check_schema_compatibility(None, "2026.1")
    assert result["compatible"] is None
