"""Agente 3 — renderiza o mapeamento APROVADO em script executável.

Só roda sobre entradas com status == "approved" ou "edited". Entradas
"pending_review" ou "rejected" são ignoradas e aparecem no relatório como
pendências — este agente nunca decide por conta própria o que incluir.
"""
from __future__ import annotations

from pathlib import Path

from app.schemas.mapping import MappingDocument, ReviewStatus
from app.transforms.registry import render_sql


class ETLAgent:
    def run(
        self,
        mapping: MappingDocument,
        source_dialect: str,
        target_dialect: str,
        output_dir: Path,
    ) -> None:
        approved = [
            e for e in mapping.entries
            if e.status in (ReviewStatus.approved, ReviewStatus.edited)
        ]
        pending = [e for e in mapping.entries if e.status == ReviewStatus.pending_review]
        rejected = [e for e in mapping.entries if e.status == ReviewStatus.rejected]

        sql_lines = [
            f"-- Script gerado para o mapeamento {mapping.mapping_id}",
            f"-- Projeto: {mapping.project}",
            f"-- {len(approved)} campo(s) aprovado(s), {len(pending)} pendente(s) de revisão",
            "",
        ]

        by_target_table: dict[str, list] = {}
        for entry in approved:
            by_target_table.setdefault(entry.target_table, []).append(entry)

        for target_table, entries in by_target_table.items():
            select_cols = []
            for e in entries:
                if e.transformation:
                    expr = render_sql(
                        e.transformation.type,
                        source_dialect,
                        e.source_field or "",
                        e.transformation.params,
                    )
                else:
                    expr = e.source_field
                select_cols.append(f"  {expr} AS {e.target_field}")

            source_tables = {e.source_table for e in entries if e.source_table}
            if len(source_tables) != 1:
                sql_lines.append(
                    f"-- ATENÇÃO: {target_table} depende de múltiplas tabelas de origem "
                    f"({source_tables}) — revisar JOIN manualmente.\n"
                )
                continue

            source_table = source_tables.pop()
            sql_lines.append(f"INSERT INTO {target_table} (")
            sql_lines.append(",\n".join(f"  {e.target_field}" for e in entries))
            sql_lines.append(")")
            sql_lines.append("SELECT")
            sql_lines.append(",\n".join(select_cols))
            sql_lines.append(f"FROM {source_table};\n")

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "etl_script.sql").write_text("\n".join(sql_lines), encoding="utf-8")

        self._write_report(mapping, approved, pending, rejected, output_dir)

    def _write_report(self, mapping, approved, pending, rejected, output_dir: Path) -> None:
        lines = [
            f"# Relatório de migração — {mapping.project}",
            "",
            f"- Campos aprovados e incluídos no script: {len(approved)}",
            f"- Campos ainda pendentes de revisão (NÃO incluídos): {len(pending)}",
            f"- Campos rejeitados: {len(rejected)}",
            "",
        ]
        if pending:
            lines.append("## Pendências — revisar antes de rodar a migração final\n")
            for e in pending:
                lines.append(
                    f"- `{e.target_table}.{e.target_field}` "
                    f"(origem sugerida: {e.source_table}.{e.source_field}) — {e.review_reason}"
                )
        (output_dir / "relatorio.md").write_text("\n".join(lines), encoding="utf-8")
