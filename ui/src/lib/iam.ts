export type UserRow = {
  id: string;
  email: string;
  is_active: boolean;
  is_sysadmin: boolean;
  is_approved: boolean;
  created_at: string;
};

export type Project = {
  id: string;
  name: string;
};

export type Membership = {
  project_id: string;
  project_name: string;
  user_id: string;
  user_email: string;
  role: string;
};

export const PROJECT_ROLES = ["viewer", "operator", "admin"];

export function rolePillClass(role: string): string {
  if (role === "admin") return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200";
  if (role === "operator") return "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200";
  return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";
}
