from dataclasses import dataclass

from fastapi import HTTPException

CANONICAL_INVENTORY_QUERY_FIELDS = {
    "search",
    "endpoint",
    "share",
    "path",
    "ext",
    "access",
    "provider",
    "resource_type",
    "item_type",
    "file_archive_status",
    "exposure",
    "source",
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
    "provider": "provider",
    "source": "source",
    "resource_type": "resource_type",
    "resourcetype": "resource_type",
    "type": "resource_type",
    "item_type": "item_type",
    "itemtype": "item_type",
    "entry_type": "item_type",
    "entrytype": "item_type",
    "kind": "item_type",
    "file_archive_status": "file_archive_status",
    "filearchivestatus": "file_archive_status",
    "file_archive_state": "file_archive_status",
    "exposure": "exposure",
    "visibility": "exposure",
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
MAX_INVENTORY_QUERY_CHARS = 4096
MAX_INVENTORY_QUERY_TOKENS = 100
MAX_INVENTORY_QUERY_CLAUSES = 25
MAX_INVENTORY_QUERY_VALUE_CHARS = 1024


@dataclass(frozen=True)
class InventoryQueryClause:
    field: str
    operator: str
    value: str
    negated: bool = False


def parse_inventory_query(raw: str | None) -> list[list[InventoryQueryClause]]:
    query = raw or ""
    if len(query) > MAX_INVENTORY_QUERY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"inventory query is too long; maximum is {MAX_INVENTORY_QUERY_CHARS} characters",
        )

    tokens = _tokenize_inventory_query(query)
    if not tokens:
        return []
    if len(tokens) > MAX_INVENTORY_QUERY_TOKENS:
        raise HTTPException(
            status_code=400,
            detail=f"inventory query is too complex; maximum is {MAX_INVENTORY_QUERY_CLAUSES} clauses",
        )

    groups: list[list[InventoryQueryClause]] = []
    current_group: list[InventoryQueryClause] = []
    pending_connector = "AND"
    expecting_clause = True
    index = 0
    clause_count = 0

    while index < len(tokens):
        if expecting_clause:
            clause, index = _parse_inventory_query_clause(tokens, index)
            clause_count += 1
            if clause_count > MAX_INVENTORY_QUERY_CLAUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"inventory query is too complex; maximum is {MAX_INVENTORY_QUERY_CLAUSES} clauses",
                )
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
    normalized = raw.strip()
    index = 0

    while index < len(normalized):
        char = normalized[index]
        if quote:
            if char == quote:
                if index + 1 < len(normalized) and normalized[index + 1] == quote:
                    current.append(quote)
                    index += 1
                else:
                    quote = None
            else:
                current.append(char)
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            index += 1
            continue

        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            continue

        current.append(char)
        index += 1

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
    _validate_inventory_query_value(value)

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
    _validate_inventory_query_value(value)

    operator = INVENTORY_QUERY_WORD_OPERATORS[operator_token[-1] if operator_token.startswith("!") else operator_token]
    negated = inherited_negated ^ prefix_negated ^ operator_token.startswith("!")
    return InventoryQueryClause(field=field, operator=operator, value=value, negated=negated)


def _normalize_inventory_query_field(value: str) -> str:
    token = value.strip().lower()
    field = INVENTORY_QUERY_FIELD_ALIASES.get(token)
    if field is None:
        raise HTTPException(status_code=400, detail=f"unsupported inventory query field: {value}")
    return field


def _validate_inventory_query_value(value: str) -> None:
    if len(value) > MAX_INVENTORY_QUERY_VALUE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(f"inventory query value is too long; maximum is {MAX_INVENTORY_QUERY_VALUE_CHARS} characters"),
        )
