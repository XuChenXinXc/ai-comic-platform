import { useEffect, useRef, useState } from "react";
import { prettyJson } from "../lib/utils";
import { FieldError } from "./status";
import { Textarea } from "./ui";

export function JsonEditor({
  value,
  onChange,
  onValidityChange,
  label = "结构化内容",
  rows = 18,
}: {
  value: unknown;
  onChange: (value: unknown) => void;
  onValidityChange?: (valid: boolean) => void;
  label?: string;
  rows?: number;
}) {
  const [text, setText] = useState(() => prettyJson(value));
  const [error, setError] = useState("");
  // 记录「最近一次同步进编辑器的外部值」；只有真正来自外部/父组件的
  // 变更（如加载、保存后回填）才覆盖正在编辑的文本，避免每次按键后的
  // 重渲染把输入重新 pretty 化导致光标跳动。
  const lastSynced = useRef(prettyJson(value));
  const focusedRef = useRef(false);

  useEffect(() => {
    const next = prettyJson(value);
    if (lastSynced.current === next) return;
    if (focusedRef.current) {
      if (text !== lastSynced.current) {
        // 用户正在编辑（文本已偏离最近一次同步值）：不覆盖输入，避免光标
        // 跳回文末；把当前文本记为已同步，后续相同外部值不再触发。
        lastSynced.current = text;
        return;
      }
    }
    lastSynced.current = next;
    setText(next);
    setError("");
  }, [value, text]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-[13px] font-semibold text-ink">{label}</span>
        <span className="rounded-full bg-black/5 px-2 py-0.5 text-[11px] font-semibold text-muted dark:bg-white/10">
          JSON
        </span>
      </div>
      <Textarea
        value={text}
        rows={rows}
        spellCheck={false}
        className="font-mono text-xs leading-5"
        aria-invalid={Boolean(error)}
        onFocus={() => {
          focusedRef.current = true;
        }}
        onBlur={() => {
          focusedRef.current = false;
          lastSynced.current = text;
        }}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            const parsed = JSON.parse(next);
            setError("");
            onValidityChange?.(true);
            onChange(parsed);
          } catch (parseError) {
            setError(
              parseError instanceof Error ? parseError.message : "JSON 格式不正确",
            );
            onValidityChange?.(false);
          }
        }}
      />
      <FieldError>{error}</FieldError>
    </div>
  );
}
