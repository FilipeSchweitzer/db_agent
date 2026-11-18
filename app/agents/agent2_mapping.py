"""Agente 2 — propõe o mapeamento origem -> destino.

Produz um MappingDocument completo, mas NUNCA o considere pronto para
uso: o checkpoint humano (app/review/export.py + aprovação manual) é
obrigatório antes de qualquer entrada seguir para o Agente 3.
"""
from __future__ import annotations

import uuid

from app.llm.client import LLMClient
from app.schemas.mapping import MappingDocument
from app.schemas.profiling import DiscoveryReport
from app.schemas.target_schema import TargetSchema
from app.transforms.registry import available_transform_names

SYSTEM_PROMPT = """\
Você mapeia campos de um banco legado para o schema de destino de um
sistema de gestão municipal.

Para cada campo de destino, proponha a origem mais provável (tabela e
coluna do legado) e, se necessário, uma transformação — escolhida
OBRIGATORIAMENTE entre os tipos de transformação disponíveis listados
abaixo. Nunca invente um tipo de transformação novo.

Transformações disponíveis: {transform_names}

Regras:
- Campos marcados pii=true no schema de destino SEMPRE recebem
  requires_human_review=true, mesmo com confiança alta.
- Se não encontrar correspondência razoável, não force uma: inclua o
  campo em unmapped_target_fields.
- confidence="low" ou "medium" sempre vem com review_reason preenchido.
"""


class MappingAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(
        self,
        project: str,
        discovery: DiscoveryReport,
        target_schema: TargetSchema,
    ) -> MappingDocument:
        system_prompt = SYSTEM_PROMPT.format(
            transform_names=", ".join(available_transform_names())
        )

        user_prompt = (
            f"Schema de destino (JSON): {target_schema.model_dump_json()}\n\n"
            f"Perfilamento do banco legado (JSON): {discovery.model_dump_json()}\n\n"
            "Produza o MappingDocument completo, cobrindo todo campo do "
            "schema de destino."
        )

        doc = self.llm.complete_structured(system_prompt, user_prompt, MappingDocument)
        doc.mapping_id = str(uuid.uuid4())
        doc.project = project
        return doc
