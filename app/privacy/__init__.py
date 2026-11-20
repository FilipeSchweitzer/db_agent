"""Privacidade e LGPD: mascaramento, política de backend e trilha de auditoria.

Quatro peças, cada uma com uma responsabilidade só:

- `masking` — valor -> padrão (`###.###.###-##`), puro e testável;
- `report` — aplica a máscara aos artefatos do discovery (top_values, notas,
  riscos) e diz quais colunas têm indício de PII;
- `policy` — interrompe o pipeline quando há PII e o backend de LLM não é
  comprovadamente local;
- `audit` — grava `auditoria.json`, uma entrada por execução.
"""
from __future__ import annotations

from app.privacy.audit import (
    AUDIT_FILE,
    build_record,
    hash_artifacts,
    pii_summary,
    read_audit,
    resolve_approver,
    write_audit_record,
)
from app.privacy.masking import (
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

__all__ = [
    "AUDIT_FILE",
    "PIIPolicyError",
    "backend_is_local",
    "build_record",
    "discovery_pii_columns",
    "enforce_pii_backend_policy",
    "hash_artifacts",
    "mask_free_text",
    "mask_stats",
    "mask_value",
    "mask_values",
    "pii_signal",
    "pii_summary",
    "read_audit",
    "resolve_approver",
    "sanitize_discovery",
    "sanitize_profile",
    "write_audit_record",
]
