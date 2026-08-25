export type InventoryQueryField =
  | "search"
  | "endpoint"
  | "share"
  | "path"
  | "ext"
  | "access"
  | "provider"
  | "source"
  | "resource_type"
  | "item_type"
  | "file_archive_status"
  | "exposure";
export type InventoryQueryOperator = "equals" | "contains" | "startswith";
export type InventoryQueryClause = {
  field: InventoryQueryField;
  operator: InventoryQueryOperator;
  value: string;
  negated: boolean;
};
export type InventoryQueryGroup = InventoryQueryClause[];

const FIELD_ALIASES: Record<string, InventoryQueryField> = {
  search: "search",
  q: "search",
  text: "search",
  endpoint: "endpoint",
  host: "endpoint",
  hostname: "endpoint",
  ip: "endpoint",
  share: "share",
  resource: "share",
  path: "path",
  path_prefix: "path",
  pathprefix: "path",
  ext: "ext",
  extension: "ext",
  access: "access",
  access_level: "access",
  accesslevel: "access",
  share_access: "access",
  shareaccess: "access",
  provider: "provider",
  source: "source",
  resource_type: "resource_type",
  resourcetype: "resource_type",
  type: "resource_type",
  item_type: "item_type",
  itemtype: "item_type",
  entry_type: "item_type",
  entrytype: "item_type",
  kind: "item_type",
  file_archive_status: "file_archive_status",
  filearchivestatus: "file_archive_status",
  file_archive_state: "file_archive_status",
  exposure: "exposure",
  visibility: "exposure",
};

const WORD_OPERATORS: Record<string, InventoryQueryOperator> = {
  "=": "equals",
  ":": "contains",
  "~": "contains",
  "^": "startswith",
  equals: "equals",
  contains: "contains",
  startswith: "startswith",
};

const COMPACT_OPERATORS = ["!^", "!~", "!=", "=", ":", "~", "^"];

export function parseInventoryQuery(raw: string): InventoryQueryGroup[] {
  const tokens = tokenizeInventoryQuery(raw);
  if (tokens.length === 0) return [];

  const groups: InventoryQueryGroup[] = [];
  let currentGroup: InventoryQueryGroup = [];
  let pendingConnector: "AND" | "OR" = "AND";
  let expectingClause = true;
  let index = 0;

  while (index < tokens.length) {
    if (expectingClause) {
      const parsed = parseInventoryQueryClause(tokens, index);
      if (currentGroup.length === 0 || pendingConnector === "AND") {
        currentGroup.push(parsed.clause);
      } else {
        groups.push(currentGroup);
        currentGroup = [parsed.clause];
      }
      index = parsed.nextIndex;
      expectingClause = false;
      continue;
    }

    const connector = tokens[index].trim().toUpperCase();
    if (connector === "AND" || connector === "OR") {
      pendingConnector = connector;
      index += 1;
    } else {
      pendingConnector = "AND";
    }
    expectingClause = true;
  }

  if (expectingClause) {
    throw new Error("Inventory query ended after a boolean operator.");
  }

  if (currentGroup.length > 0) groups.push(currentGroup);
  return groups;
}

function tokenizeInventoryQuery(raw: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  const input = raw.trim();

  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    if (quote) {
      if (char === quote) {
        if (input[index + 1] === quote) {
          current += quote;
          index += 1;
        } else {
          quote = null;
        }
      } else {
        current += char;
      }
      continue;
    }

    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }

    if (/\s/.test(char)) {
      if (current) {
        tokens.push(current);
        current = "";
      }
      continue;
    }

    current += char;
  }

  if (quote) {
    throw new Error("Unterminated quoted value in inventory query.");
  }
  if (current) tokens.push(current);
  return tokens;
}

function parseInventoryQueryClause(tokens: string[], index: number): { clause: InventoryQueryClause; nextIndex: number } {
  if (index >= tokens.length) {
    throw new Error("Expected inventory query clause.");
  }

  let negated = false;
  let token = tokens[index];
  const normalized = token.trim().toUpperCase();
  if (normalized === "NOT" || normalized === "!") {
    negated = true;
    index += 1;
    if (index >= tokens.length) {
      throw new Error("Expected clause after NOT.");
    }
    token = tokens[index];
  }

  const compact = parseCompactInventoryQueryClause(token, negated);
  if (compact) {
    return { clause: compact, nextIndex: index + 1 };
  }

  if (token.startsWith("!") && token.length > 1) {
    negated = !negated;
    token = token.slice(1);
  }

  const field = normalizeInventoryQueryField(token);
  index += 1;
  if (index >= tokens.length) {
    throw new Error(`Expected operator after ${field}.`);
  }

  const operatorToken = tokens[index].trim().toLowerCase();
  const operator = WORD_OPERATORS[operatorToken];
  if (!operator) {
    throw new Error(`Unsupported operator: ${tokens[index]}.`);
  }

  index += 1;
  if (index >= tokens.length) {
    throw new Error(`Expected value after ${field} ${tokens[index - 1]}.`);
  }

  const value = tokens[index].trim();
  if (!value) {
    throw new Error(`Missing value for ${field}.`);
  }

  return {
    clause: { field, operator, value, negated },
    nextIndex: index + 1,
  };
}

function parseCompactInventoryQueryClause(token: string, inheritedNegated = false): InventoryQueryClause | null {
  const compact = token.trim();
  if (!compact) return null;

  const prefixNegated = compact.startsWith("!");
  const body = prefixNegated ? compact.slice(1) : compact;

  let fieldToken: string | null = null;
  let operatorToken: string | null = null;
  let valueToken: string | null = null;
  for (const candidate of COMPACT_OPERATORS) {
    const marker = body.indexOf(candidate);
    if (marker <= 0) continue;
    fieldToken = body.slice(0, marker);
    operatorToken = candidate;
    valueToken = body.slice(marker + candidate.length);
    break;
  }

  if (!fieldToken || !operatorToken || valueToken == null) return null;

  const field = normalizeInventoryQueryField(fieldToken);
  const value = valueToken.trim();
  if (!value) {
    throw new Error(`Missing value for ${field}.`);
  }

  const operator = WORD_OPERATORS[operatorToken.startsWith("!") ? operatorToken.slice(1) : operatorToken];
  const negated =
    [inheritedNegated, prefixNegated, operatorToken.startsWith("!")].filter((entry) => entry).length % 2 === 1;
  return { field, operator, value, negated };
}

function normalizeInventoryQueryField(value: string): InventoryQueryField {
  const field = FIELD_ALIASES[value.trim().toLowerCase()];
  if (!field) {
    throw new Error(`Unsupported inventory query field: ${value}.`);
  }
  return field;
}
