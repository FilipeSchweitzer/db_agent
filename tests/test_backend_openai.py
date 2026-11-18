from __future__ import annotations

from pydantic import BaseModel

from app.llm.backend_openai import to_strict_schema


class Item(BaseModel):
    nome: str
    tags: list[str] = []


class Raiz(BaseModel):
    itens: list[Item]
    titulo: str = "padrao"
    # Campo com nome igual a uma palavra-chave do JSON Schema: não pode ser
    # removido do mapa de propriedades pela limpeza de keywords.
    title: str = "campo-chamado-title"


def _sem_keywords_removidas(node: object) -> bool:
    if isinstance(node, list):
        return all(_sem_keywords_removidas(item) for item in node)
    if not isinstance(node, dict):
        return True
    if "default" in node:
        return False
    return all(
        _sem_keywords_removidas(value)
        for key, value in node.items()
        if key != "properties"
    ) and all(_sem_keywords_removidas(spec) for spec in (node.get("properties") or {}).values())


def test_objeto_raiz_vira_fechado_e_totalmente_obrigatorio():
    schema = to_strict_schema(Raiz.model_json_schema())

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["itens", "titulo", "title"]
    assert "title" not in schema


def test_objetos_aninhados_tambem_sao_fechados():
    schema = to_strict_schema(Raiz.model_json_schema())
    item = schema["$defs"]["Item"]

    assert item["additionalProperties"] is False
    assert item["required"] == ["nome", "tags"]


def test_defaults_saem_do_schema():
    assert _sem_keywords_removidas(to_strict_schema(Raiz.model_json_schema()))


def test_campo_chamado_title_sobrevive_na_limpeza():
    schema = to_strict_schema(Raiz.model_json_schema())

    assert "title" in schema["properties"]
