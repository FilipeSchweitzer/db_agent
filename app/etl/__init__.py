"""Geração de ETL a partir do mapeamento aprovado.

`plan` decide o que migrar e em que ordem; `generator` escreve o SQL;
`dialects` concentra o que muda entre Postgres, SQL Server e Firebird.
"""
