import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatError(error: unknown) {
  if (error instanceof Error) return error.message;
  return "操作失败，请稍后重试";
}

export function parseJson<T>(value: unknown, fallback: T): T {
  if (value == null || value === "") return fallback;
  if (typeof value !== "string") return value as T;
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

export function prettyJson(value: unknown) {
  return JSON.stringify(parseJson(value, value), null, 2);
}

export function makeDownloadName(name: string, extension: string) {
  const safe = name.replace(/[\\/:*?"<>|]+/g, "-").trim() || "download";
  return `${safe}.${extension}`;
}
