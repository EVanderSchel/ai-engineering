export interface Plan {
  env: Record<string, string>;
  uploads: string[][];
  invalidate: string[];
  url: string;
}
export function plan(auth: Record<string, string>, ui: Record<string, string>): Plan;
