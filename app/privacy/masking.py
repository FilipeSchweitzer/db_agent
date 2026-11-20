"""Mascaramento de valores de amostra antes de qualquer chamada de LLM.

O modelo não precisa do CPF do cidadão; precisa do **padrão** — que é o que
distingue `###.###.###-##` (CPF) de `DD/MM/AAAA` (data) e de `##.###.###/####-##`
(CNPJ). Este módulo troca valor por padrão e é puro: não conhece banco, LLM
nem config, o que o torna trivial de testar.

Regras, em ordem:

1. **nome da coluna** manda quando é inequívoco (`CPF`, `CNPJ`, `EMAIL`,
   `TELEFONE`, `DT_NASC`, `NOME`, `ENDERECO`, ...);
2. **forma do valor** cobre nome que não ajuda (`NUM_DOC`, `CODIGO`): e-mail,
   CPF/CNPJ, telefone e CEP têm formato reconhecível;
3. o resto vira **forma genérica**: dígito -> `#`, letra -> `A`, separadores
   preservados. `"SC"` -> `"AA"`, `"Rua das Flores, 123"` -> `"AAA AAA AAAAAA, ###"`.

Nada aqui decide se um valor *é* PII — isso é `pii_signal()`, que só precisa
ser conservador: falso positivo custa uma máscara a mais, falso negativo custa
dado pessoal na rede.
"""
from __future__ import annotations

import re
import unicodedata

# Pistas por nome de coluna. Curto de propósito: é ponto de partida, e o
# Agente 1 deve refinar com o valor (por isso o padrão também é analisado).
_NAME_HINTS = re.compile(
    r"cpf|cnpj|rg\b|identidade|nome|endereco|telefone|fone|celular|whatsapp"
    r"|e_?mail|nasc|aniversario|obito|matricula|titulo|cnh|cep\b",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Datas: DD/MM/AAAA, DD-MM-AA, AAAA-MM-DD (com hora opcional).
_DATE_BR = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")

# Padrões canônicos que o LLM passa a ver no lugar do valor.
CPF_PATTERN = "###.###.###-##"
CNPJ_PATTERN = "##.###.###/####-##"
PHONE_PATTERN = "(##) #####-####"
DATE_BR_PATTERN = "DD/MM/AAAA"
DATE_ISO_PATTERN = "AAAA-MM-DD"
CEP_PATTERN = "#####-###"
EMAIL_PATTERN = "<email>"
NAME_PATTERN = "<nome>"
ADDRESS_PATTERN = "<endereço>"
DOCUMENT_PATTERN = "<documento>"

# Padrões já mascarados. Mascarar de novo (`mask_value` roda no prompt e na
# resposta do modelo) não pode degradar `###.###.###-##` para `<documento>`.
_MASKED_PATTERNS = {
    CPF_PATTERN,
    CNPJ_PATTERN,
    PHONE_PATTERN,
    DATE_BR_PATTERN,
    DATE_ISO_PATTERN,
    CEP_PATTERN,
    EMAIL_PATTERN,
    NAME_PATTERN,
    ADDRESS_PATTERN,
    DOCUMENT_PATTERN,
}

# Limite de comprimento do padrão genérico: o modelo precisa da forma, não
# de um despejo de 4 KB de texto.
_MAX_PATTERN_CHARS = 48


def normalize(text: str) -> str:
    """Minúsculas e sem acento — `ENDEREÇO` e `endereco` são a mesma pista."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode().lower()


def is_pii_name(column_name: str) -> bool:
    return bool(_NAME_HINTS.search(normalize(column_name)))


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _looks_like_pii(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if _EMAIL.match(text):
        return True
    # Data sozinha não é dado pessoal para efeito de política de rede (é dado
    # de negócio na maioria das tabelas e o valor já vai mascarado). Quando é
    # data de nascimento, o nome da coluna carrega a pista.
    if _DATE_BR.match(text) or _DATE_ISO.match(text):
        return False
    digits = _digits(text)
    # 8 (CEP), 11 (CPF/telefone), 14 (CNPJ), 10 (telefone fixo).
    if len(digits) in (8, 10, 11, 14) and len(digits) >= len(text) - 4:
        return True
    return False


def pii_signal(values: list[str] | None, column_name: str) -> bool:
    """Há evidência suficiente de dado pessoal nesta coluna?

    Conservador por desenho: nome que sugere PII basta, e qualquer valor com
    forma de documento/e-mail também. Data sozinha fica de fora (é dado de
    negócio na maioria das tabelas); `DT_NASC` é pega pelo nome. O custo de
    errar para mais é uma máscara; para menos é vazamento.
    """
    if is_pii_name(column_name):
        return True
    return any(_looks_like_pii(str(value)) for value in (values or [])[:10])


def _shape(value: str) -> str:
    out: list[str] = []
    for char in value:
        if char.isdigit():
            out.append("#")
        elif char.isalpha():
            out.append("A")
        elif char in " .-/_@()+,:;":
            out.append(char)
        else:
            out.append("?")
    text = "".join(out)
    if len(text) > _MAX_PATTERN_CHARS:
        text = text[: _MAX_PATTERN_CHARS - 1] + "…"
    return text


def mask_value(value: object, column_name: str) -> str:
    """Troca um valor de amostra pelo seu padrão. Nunca devolve o valor."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text in _MASKED_PATTERNS:
        return text

    name = normalize(column_name)
    digits = _digits(text)

    # 1. Pelo nome da coluna, quando ele é específico.
    if "cnpj" in name:
        return CNPJ_PATTERN if len(digits) == 14 else DOCUMENT_PATTERN
    if "cpf" in name:
        return CPF_PATTERN if len(digits) == 11 else DOCUMENT_PATTERN
    if "email" in name:
        return EMAIL_PATTERN
    if any(hint in name for hint in ("telefone", "fone", "celular", "whatsapp")):
        return PHONE_PATTERN
    if "cep" in name:
        return CEP_PATTERN
    if any(hint in name for hint in ("nasc", "data", "aniversario", "obito")):
        return DATE_ISO_PATTERN if re.match(r"^\d{4}-", text) else DATE_BR_PATTERN
    if any(hint in name for hint in ("matricula", "titulo", "cnh", "identidade")) or re.search(
        r"(^|_)rg($|_)", name
    ):
        return DOCUMENT_PATTERN
    if any(hint in name for hint in ("endereco", "logradouro", "rua", "avenida", "bairro")):
        return ADDRESS_PATTERN
    if "nome" in name:
        return NAME_PATTERN

    # 2. Pela forma do valor.
    if _EMAIL.match(text):
        return EMAIL_PATTERN
    if _DATE_BR.match(text):
        return DATE_BR_PATTERN
    if _DATE_ISO.match(text):
        return DATE_ISO_PATTERN
    if len(digits) == 14 and digits == re.sub(r"[.\-/]", "", text):
        return CNPJ_PATTERN
    if len(digits) == 11 and len(digits) >= len(text) - 4:
        return CPF_PATTERN
    if len(digits) == 8 and digits == re.sub(r"[.\-]", "", text) and "-" in text:
        return CEP_PATTERN
    if len(digits) in (10, 11) and re.search(r"[()+\- ]", text):
        return PHONE_PATTERN

    # 3. Forma genérica.
    return _shape(text)


def mask_values(values: list[str], column_name: str) -> list[str]:
    """Mascara e deduplica: `M`/`F` viram um único `A`, não dois iguais."""
    patterns: list[str] = []
    for value in values:
        pattern = mask_value(value, column_name)
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return patterns


# PII dentro de texto livre (notas do LLM, riscos, mensagens). Aqui não há
# nome de coluna para consultar, então o reconhecimento é só pela forma.
# CNPJ antes de CPF: 14 dígitos contêm uma sequência de 11.
_FREE_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), CNPJ_PATTERN),
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), CPF_PATTERN),
    (re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"), EMAIL_PATTERN),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), DATE_BR_PATTERN),
    (re.compile(r"\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b"), PHONE_PATTERN),
)


def mask_free_text(text: str | None) -> str:
    """Troca PII reconhecível dentro de um texto por padrões.

    Defesa em profundidade: o caminho normal nunca deixa valor real chegar
    ao LLM, mas um `discovery_report.json` antigo (gerado antes da Fase 4) ou
    uma nota que cite um documento não podem atravessar o prompt intactos.
    """
    if not text:
        return ""
    masked = text
    for pattern, replacement in _FREE_TEXT_RULES:
        masked = pattern.sub(replacement, masked)
    return masked
