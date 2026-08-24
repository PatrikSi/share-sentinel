import { normalizedProvider } from "@/lib/provider-context";

export type InventoryConnectionTarget = {
  kind: "path" | "url";
  value: string;
};

type InventoryConnectionContext = {
  endpointKey: string;
  hostname?: string | null;
  ip?: string | null;
  provider?: string | null;
  resourceType?: string | null;
  shareType?: string | null;
};

type InventoryItemConnectionContext = InventoryConnectionContext & {
  resourceName: string;
  path: string;
  isDirectory: boolean;
  webUrl?: string | null;
};

type InventoryResourceConnectionContext = InventoryConnectionContext & {
  resourceName: string;
  webUrl?: string | null;
};

type InventoryEndpointConnectionContext = InventoryConnectionContext & {
  webUrl?: string | null;
};

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

function connectionProvider(context: InventoryConnectionContext): string {
  const declared = normalizedProvider(context.provider, context.resourceType, context.shareType);
  if (declared !== "unknown" && declared !== "network") return declared;
  const endpointKey = context.endpointKey.trim().toLowerCase();
  if (endpointKey.startsWith("sharepoint:")) return "sharepoint";
  if (endpointKey.endsWith(":445")) return "smb";
  if (endpointKey.endsWith(":2049")) return "nfs";
  return declared;
}

function endpointHost(context: InventoryConnectionContext): string | null {
  const candidates = [context.hostname, context.ip, context.endpointKey];
  for (const candidate of candidates) {
    if (typeof candidate !== "string") continue;
    let value = candidate.trim();
    if (!value || value.toLowerCase().startsWith("sharepoint:")) continue;
    if (value.endsWith(":445")) value = value.slice(0, -4);
    if (value.endsWith(":2049")) value = value.slice(0, -5);
    if (!value || CONTROL_CHARACTERS.test(value) || /[\\/]/.test(value)) continue;
    return value;
  }
  return null;
}

function windowsSegments(value: string): string[] | null {
  if (CONTROL_CHARACTERS.test(value)) return null;
  return value
    .replaceAll("/", "\\")
    .split("\\")
    .filter((segment) => segment.length > 0);
}

function smbTarget(
  context: InventoryConnectionContext,
  resourceName?: string,
  itemPath?: string,
  isDirectory = true,
): InventoryConnectionTarget | null {
  const host = endpointHost(context);
  if (!host || host.includes(":")) return null;
  let target = `\\\\${host}`;
  if (resourceName !== undefined) {
    const shareSegments = windowsSegments(resourceName);
    if (!shareSegments || shareSegments.length !== 1) return null;
    target += `\\${shareSegments[0]}`;
  }
  if (itemPath !== undefined) {
    const pathSegments = windowsSegments(itemPath);
    if (!pathSegments) return null;
    const targetSegments = isDirectory ? pathSegments : pathSegments.slice(0, -1);
    if (targetSegments.length > 0) target += `\\${targetSegments.join("\\")}`;
  }
  return { kind: "path", value: target };
}

function nfsHost(context: InventoryConnectionContext): string | null {
  const host = endpointHost(context);
  if (!host) return null;
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}

function nfsTarget(
  context: InventoryConnectionContext,
  resourceName?: string,
  itemPath?: string,
  isDirectory = true,
): InventoryConnectionTarget | null {
  const host = nfsHost(context);
  if (!host) return null;
  const resourcePath = resourceName?.trim().replaceAll("\\", "/") || "/";
  if (CONTROL_CHARACTERS.test(resourcePath)) return null;
  let path = `/${resourcePath.split("/").filter(Boolean).join("/")}`;
  if (itemPath !== undefined) {
    const itemSegments = itemPath.replaceAll("\\", "/").split("/").filter(Boolean);
    const targetSegments = isDirectory ? itemSegments : itemSegments.slice(0, -1);
    if (targetSegments.some((segment) => CONTROL_CHARACTERS.test(segment))) return null;
    if (targetSegments.length > 0) path = `${path.replace(/\/$/, "")}/${targetSegments.join("/")}`;
  }
  return { kind: "path", value: `${host}:${path}` };
}

export function itemConnectionTarget(context: InventoryItemConnectionContext): InventoryConnectionTarget | null {
  const provider = connectionProvider(context);
  if (provider === "sharepoint") {
    const value = context.webUrl?.trim();
    return value ? { kind: "url", value } : null;
  }
  if (provider === "smb") {
    return smbTarget(context, context.resourceName, context.path, context.isDirectory);
  }
  if (provider === "nfs") {
    return nfsTarget(context, context.resourceName, context.path, context.isDirectory);
  }
  return null;
}

export function resourceConnectionTarget(context: InventoryResourceConnectionContext): InventoryConnectionTarget | null {
  const provider = connectionProvider(context);
  if (provider === "sharepoint") {
    const value = context.webUrl?.trim();
    return value ? { kind: "url", value } : null;
  }
  if (provider === "smb") return smbTarget(context, context.resourceName);
  if (provider === "nfs") return nfsTarget(context, context.resourceName);
  return null;
}

export function endpointConnectionTarget(context: InventoryEndpointConnectionContext): InventoryConnectionTarget | null {
  const provider = connectionProvider(context);
  if (provider === "sharepoint") {
    const value = context.webUrl?.trim();
    return value ? { kind: "url", value } : null;
  }
  if (provider === "smb") return smbTarget(context);
  // NFS has no browseable server-level target: an export path is required.
  if (provider === "nfs") return null;
  return null;
}
