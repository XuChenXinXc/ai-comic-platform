import { useEffect, useState } from "react";
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

  useEffect(() => {
    setText(prettyJson(value));
    setError("");
  }, [value]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-bold text-[#555852]">{label}</span>
        <span className="text-[11px] text-muted">JSON</span>
      </div>
      <Textarea
        value={text}
        rows={rows}
        spellCheck={false}
        className="font-mono text-xs leading-5"
        aria-invalid={Boolean(error)}
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
