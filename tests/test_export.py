"""Round-trip da planilha de revisão — a fronteira de confiança do pipeline.

O que o analista digita no Excel vira `MappingEntry` validado. Estes testes
cobrem o caminho de ida e volta: `export_to_xlsx` -> edição -> `load_reviewed_xlsx`.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.review.export import export_to_xlsx, load_reviewed_xlsx
from app.schemas.mapping import ReviewStatus
from tests.conftest import make_mapping


def _editar(tmp_path, mapping):
    path = tmp_path / "revisao.xlsx"
    export_to_xlsx(mapping, path)
    return path


def _salvar(path, df):
    df.to_excel(path, index=False, sheet_name="mapeamento")


def test_ida_e_volta_preserva_entradas_e_transformacoes(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    back = load_reviewed_xlsx(mapping, path)

    assert len(back.entries) == len(mapping.entries)
    original = {(e.target_table, e.target_field): e for e in mapping.entries}
    for entry in back.entries:
        before = original[(entry.target_table, entry.target_field)]
        assert entry.source_table == before.source_table
        assert entry.source_field == before.source_field
        assert entry.status == before.status
        if before.transformation:
            assert entry.transformation is not None
            assert entry.transformation.type == before.transformation.type
            assert entry.transformation.params == before.transformation.params


def test_status_editado_na_planilha_prevalece(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    df.loc[0, "status"] = "edited"
    _salvar(path, df)

    back = load_reviewed_xlsx(mapping, path)
    assert back.entries[0].status is ReviewStatus.edited


def test_status_invalido_falha_com_a_linha(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    df.loc[0, "status"] = "aprovado-com-carinho"
    _salvar(path, df)

    with pytest.raises(ValueError, match="linha 2.*inválido"):
        load_reviewed_xlsx(mapping, path)


def test_status_vazio_falha_com_a_linha(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    df.loc[1, "status"] = None
    _salvar(path, df)

    with pytest.raises(ValueError, match="linha 3.*status.*vazia"):
        load_reviewed_xlsx(mapping, path)


def test_linha_desconhecida_e_ignorada_com_aviso(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    extra = df.iloc[[0]].copy()
    extra.loc[:, "target_table"] = "tabela_que_nao_existe"
    _salvar(path, pd.concat([df, extra], ignore_index=True))

    back = load_reviewed_xlsx(mapping, path)

    assert len(back.entries) == len(mapping.entries)
    assert any("não existe no mapeamento" in warning for warning in back.warnings)


def test_linha_duplicada_e_ignorada_com_aviso(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    _salvar(path, pd.concat([df, df.iloc[[0]]], ignore_index=True))

    back = load_reviewed_xlsx(mapping, path)

    assert len(back.entries) == len(mapping.entries)
    assert any("duplicada" in warning for warning in back.warnings)


def test_linha_removida_da_planilha_volta_como_pendente(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    alvo = (df["target_table"] == "pessoa") & (df["target_field"] == "cpf")
    _salvar(path, df[~alvo])

    back = load_reviewed_xlsx(mapping, path)

    pendentes = [
        entry
        for entry in back.entries
        if entry.target_table == "pessoa" and entry.target_field == "cpf"
    ]
    assert len(pendentes) == 1
    assert pendentes[0].status is ReviewStatus.pending_review
    assert any("não está na planilha" in warning for warning in back.warnings)


def test_params_de_transformacao_invalidos_falham_com_a_linha(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    linha = df.index[df["transformation_type"] == "regex_extract_digits"][0]
    df.loc[linha, "transformation_params"] = "{isso não é json"
    _salvar(path, df)

    with pytest.raises(ValueError, match="não é JSON válido"):
        load_reviewed_xlsx(mapping, path)


def test_params_de_transformacao_precisam_ser_objeto(tmp_path):
    mapping = make_mapping()
    path = _editar(tmp_path, mapping)

    df = pd.read_excel(path, sheet_name="mapeamento")
    linha = df.index[df["transformation_type"] == "regex_extract_digits"][0]
    df.loc[linha, "transformation_params"] = "[1, 2]"
    _salvar(path, df)

    with pytest.raises(ValueError, match="precisa ser um objeto JSON"):
        load_reviewed_xlsx(mapping, path)
