from dataclasses import dataclass

from fastapi import HTTPException

CANONICAL_INVENTORY_QUERY_FIELDS = {
    "search",
    "endpoint",
    "share",
    "path",
    "ext",
    "access",
}

INVENTORY_QUERY_FIELD_ALIASES = {
    "search": "search",
    "q": "search",
    "text": "search",
    "endpoint": "endpoint",
    "host": "endpoint",
    "hostname": "endpoint",
    "ip": "endpoint",
    "share": "share",
    "resource": "share",
    "path": "path",
    "path_prefix": "path",
    "pathprefix": "path",
    "ext": "ext",
    "extension": "ext",
    "access": "access",
    "access_level": "access",
    "accesslevel": "access",
    "share_access": "access",
    "shareaccess": "access",
}

INVENTORY_QUERY_WORD_OPERATORS = {
    "=": "equals",
    ":": "contains",
    "~": "contains",
    "^": "startswith",
    "equals": "equals",
    "contains": "contains",
    "startswith": "startswith",
}

INVENTORY_QUERY_COMPACT_OPERATORS = ("!^", "!~", "!=", "=", ":", "~", "^")


@dataclass(frozen=True)
class InventoryQueryClause:
    field: str
    operator: str
    value: str
    negated: bool = False


def parse_inventory_query(raw: str | None) -> list[list[InventoryQueryClause]]:
    tokens = _tokenize_inventory_query(raw or "")
    if not tokens:
        return []

    groups: list[list[InventoryQueryClause]] = []
    current_group: list[InventoryQueryClause] = []
    pending_connector = "AND"
    expecting_clause = True
    index = 0

    while index < len(tokens):
        if expecting_clause:
            clause, index = _parse_inventory_query_clause(tokens, index)
            if not current_group or pending_connector == "AND":
                current_group.append(clause)
            else:
                groups.append(current_group)
                current_group = [clause]
            expecting_clause = False
            continue

        connector = tokens[index].strip().upper()
        if connector in {"AND", "OR"}:
            pending_connector = connector
            index += 1
        else:
            pending_connector = "AND"
        expecting_clause = True

    if expecting_clause:
        raise HTTPException(status_code=400, detail="inventory query ended after a boolean operator")

    if current_group:
        groups.append(current_group)
    return groups


def _tokenize_inventory_query(raw: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None

    for char in raw.strip():
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue

        if char in {"'", '"'}:
            quote = char
            continue

        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue

        current.append(char)

    if quote:
        raise HTTPException(status_code=400, detail="unterminated quoted value in inventory query")
    if current:
        tokens.append("".join(current))
    return tokens


def _parse_inventory_query_clause(tokens: list[str], index: int) -> tuple[InventoryQueryClause, int]:
    if index >= len(tokens):
        raise HTTPException(status_code=400, detail="expected inventory query clause")

    negated = False
    token = tokens[index]
    normalized_token = token.strip().upper()
    if normalized_token in {"NOT", "!"}:
        negated = True
        index += 1
        if index >= len(tokens):
            raise HTTPException(status_code=400, detail="expected clause after NOT")
        token = tokens[index]

    compact_clause = _parse_compact_inventory_query_clause(token, negated)
    if compact_clause is not None:
        return compact_clause, index + 1

    if token.startswith("!") and len(token) > 1:
        negated = not negated
        token = token[1:]

    field = _normalize_inventory_query_field(token)
    index += 1
    if index >= len(tokens):
        raise HTTPException(status_code=400, detail=f"expected operator after {field}")

    operator_token = tokens[index].strip().lower()
    operator = INVENTORY_QUERY_WORD_OPERATORS.get(operator_token)
    if operator is None:
        raise HTTPException(status_code=400, detail=f"unsupported operator: {tokens[index]}")

    index += 1
    if index >= len(tokens):
        raise HTTPException(status_code=400, detail=f"expected value after {field} {tokens[index - 1]}")

    value = tokens[index].strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"missing value for {field}")

    return InventoryQueryClause(field=field, operator=operator, value=value, negated=negated), index + 1


def _parse_compact_inventory_query_clause(token: str, inherited_negated: bool = False) -> InventoryQueryClause | None:
    compact = token.strip()
    if not compact:
        return None

    prefix_negated = compact.startswith("!")
    body = compact[1:] if prefix_negated else compact

    field_token: str | None = None
    operator_token: str | None = None
    value_token: str | None = None
    for candidate in INVENTORY_QUERY_COMPACT_OPERATORS:
        marker = body.find(candidate)
        if marker <= 0:
            continue
        field_token = body[:marker]
        operator_token = candidate
        value_token = body[marker + len(candidate) :]
        break

    if field_token is None or operator_token is None or value_token is None:
        return None

    field = _normalize_inventory_query_field(field_token)
    value = value_token.strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"missing value for {field}")

    operator = INVENTORY_QUERY_WORD_OPERATORS[operator_token[-1] if operator_token.startswith("!") else operator_token]
    negated = inherited_negated ^ prefix_negated ^ operator_token.startswith("!")
    return InventoryQueryClause(field=field, operator=operator, value=value, negated=negated)


def _normalize_inventory_query_field(value: str) -> str:
    token = value.strip().lower()
    field = INVENTORY_QUERY_FIELD_ALIASES.get(token)
    if field is None:
        raise HTTPException(status_code=400, detail=f"unsupported inventory query field: {value}")
    return field
