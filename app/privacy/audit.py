"""Trilha de auditoria por execução: `output/<projeto>/auditoria.json`.

Não é log de aplicação: é a resposta a "quem aprovou o quê, quando, e contra
qual versão do schema". Cada execução de `discover`, `map` ou `generate`
acrescenta uma entrada em `executions`, com:

- `run_id` e horários (início/fim/duração);
- quem executou (usuário do SO + host) e quem aprovou;
- versão do `target_schema` e se ela bate com a do mapeamento;
- SHA-256 do `mapping.json`, do estado revisado e dos artefatos gerados;
- resumo de PII: colunas detectadas, backend de LLM e se ele é local.

O arquivo é cumulativo e escrito de forma atômica (tmp + replace): uma
execução interrompida não deixa JSON pela metade.
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.llm import current_backend_name
from app.privacy.policy import backend_is_local
from app.review.approval import file_sha256

AUDIT_FILE = "auditoria.json"
AUDIT_SCHEMA_VERSION = 1

# Variável de ambiente aceita quando o CLI não recebe `--approved-by`.
APPROVER_ENV = "MIGRA_APPROVER"


def audit_path(output_dir: Path) -> Path:
    return output_dir / AUDIT_FILE


def read_audit(output_dir: Path) -> dict:
    """Conteúdo atual da trilha (vazio se ainda não existe)."""
    path = audit_path(output_dir)
    if not path.exists():
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "executions": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _actor() -> dict[str, str]:
    try:
        user = getpass.getuser()
    except Exception:  # ambientes sem passwd (container minimalista)
        user = os.environ.get("USER") or "desconhecido"
    return {"user": user, "host": socket.gethostname()}


def resolve_approver(explicit: str | None = None) -> str:
    """Quem assina a aprovação: CLI > `MIGRA_APPROVER` > usuário do SO.

    O padrão nunca é vazio — uma trilha de auditoria sem nome não serve para
    nada. Para uso real, defina `MIGRA_APPROVER` com o analista responsável.
    """
    candidate = (explicit or os.environ.get(APPROVER_ENV) or "").strip()
    return candidate or _actor()["user"]


def pii_summary(columns: list[str] | None) -> dict:
    """Bloco `pii` da auditoria: o que foi detectado e para onde iria."""
    detected = sorted(set(columns or []))
    backend = current_backend_name()
    return {
        "detected": bool(detected),
        "columns": detected,
        "masked": True,
        "llm_backend": backend,
        "llm_local": backend_is_local(backend),
    }


def hash_artifacts(output_dir: Path, names: list[str]) -> dict[str, str]:
    """SHA-256 dos artefatos que existem — prova do que foi entregue."""
    hashes: dict[str, str] = {}
    for name in sorted(set(names)):
        path = output_dir / name
        if path.is_file():
            hashes[name] = file_sha256(path)
    return hashes


def _llm_summary(llm) -> dict | None:
    if llm is None:
        return None
    backend = current_backend_name()
    return {
        "backend": backend,
        "model": llm.model_name,
        "local": backend_is_local(backend),
        "totals": llm.totals,
    }


def build_record(
    *,
    project: str,
    phase: str,
    started_at: datetime,
    duration_ms: float,
    pii: dict,
    llm=None,
    **extra: object,
) -> dict:
    """Monta a entrada de auditoria de UMA execução de fase."""
    return {
        "run_id": uuid.uuid4().hex,
        "project": project,
        "phase": phase,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_ms": round(duration_ms, 1),
        "actor": _actor(),
        "pii": pii,
        "llm": _llm_summary(llm),
        **extra,
    }


def write_audit_record(output_dir: Path, record: dict) -> Path:
    """Acrescenta a entrada ao `auditoria.json` do projeto."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = audit_path(output_dir)
    data = read_audit(output_dir)
    data["schema_version"] = AUDIT_SCHEMA_VERSION
    data.setdefault("project", record.get("project"))
    data.setdefault("executions", []).append(record)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path
