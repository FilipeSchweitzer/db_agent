"""Prova de que o script gerado corresponde ao mapeamento que foi revisado.

Fluxo real: `map` gera `mapping.json` e a planilha; o analista edita a
planilha; `generate` relê as duas coisas. Se `mapping.json` for regerado
entre a revisão e o `generate`, a planilha passa a descrever um mapeamento
que não existe mais — e o script sairia diferente do que foi aprovado, sem
nenhum aviso.

`approval.json` fecha esse buraco: guarda o SHA-256 do `mapping.json` no
momento da exportação, e o `generate` recusa rodar quando o hash não bate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

APPROVAL_FILE = "approval.json"
MAPPING_FILE = "mapping.json"


class ApprovalError(RuntimeError):
    """O mapeamento em disco não é o que foi revisado."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_approval(
    output_dir: Path,
    *,
    project: str,
    mapping_id: str,
    target_schema_version: str,
    entries: int,
) -> Path:
    mapping_path = output_dir / MAPPING_FILE
    if not mapping_path.exists():
        raise FileNotFoundError(f"{mapping_path} não existe — rode 'map' antes de 'generate'.")

    record = {
        "project": project,
        "mapping_id": mapping_id,
        "target_schema_version": target_schema_version,
        "entries": entries,
        "mapping_sha256": file_sha256(mapping_path),
        "created_at": datetime.now(UTC).isoformat(),
    }
    path = output_dir / APPROVAL_FILE
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def verify_approval(output_dir: Path) -> dict:
    """Confere se `mapping.json` ainda é o que foi exportado para revisão."""
    mapping_path = output_dir / MAPPING_FILE
    approval_path = output_dir / APPROVAL_FILE

    if not mapping_path.exists():
        raise ApprovalError(
            f"{mapping_path} não existe — rode 'map' antes de 'generate'."
        )
    if not approval_path.exists():
        raise ApprovalError(
            f"{approval_path} não existe: não há registro de qual mapeamento foi "
            "revisado. Rode 'map' de novo e revise a planilha."
        )

    record = json.loads(approval_path.read_text(encoding="utf-8"))
    current = file_sha256(mapping_path)
    if record.get("mapping_sha256") != current:
        raise ApprovalError(
            "mapping.json mudou desde que a planilha de revisão foi gerada.\n"
            f"  hash esperado: {record.get('mapping_sha256')}\n"
            f"  hash atual:    {current}\n"
            "A revisão humana foi feita sobre outro mapeamento. Rode 'map' de novo "
            "e revise a planilha nova (ou use --force para assumir o risco)."
        )
    return record


def review_fingerprint(mapping) -> str:
    """Hash do estado revisado: quais campos foram aprovados/editados/rejeitados.

    Gravado no run_log e no relatório — é a trilha de auditoria de "o que
    exatamente foi aprovado nesta execução".
    """
    lines = sorted(
        f"{entry.target_table}.{entry.target_field}={entry.status}" for entry in mapping.entries
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def check_schema_compatibility(
    mapping_version: str | None,
    schema_version: str,
    approval: dict | None = None,
    *,
    force: bool = False,
) -> dict:
    """Compara a versão do schema usada no mapeamento com a atual.

    `target_schema.yaml` tem `version: "2026.1"` desde o começo; até a Fase 4
    ela era decorativa. Aqui ela passa a valer: um mapeamento proposto contra
    a versão A e um schema em disco na versão B não descrevem a mesma coisa.

    `force=True` não impede a execução, mas o resultado sai com
    `compatible=False` para a auditoria registrar o risco assumido.
    """
    expected = mapping_version or (approval or {}).get("target_schema_version")
    compatible = None if not expected else expected == schema_version

    if compatible is False and not force:
        raise ApprovalError(
            "target_schema.yaml mudou desde que o mapeamento foi gerado.\n"
            f"  versão do mapeamento: {expected}\n"
            f"  versão em disco:      {schema_version}\n"
            "O mapeamento descreve outro schema. Rode 'map' de novo e revise a "
            "planilha (ou use --force para assumir o risco, registrado na auditoria)."
        )

    return {
        "mapping_version": expected,
        "schema_version": schema_version,
        "compatible": compatible,
    }
