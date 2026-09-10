import { cva, type VariantProps } from "class-variance-authority";
import { LoaderCircle, X } from "lucide-react";
import {
  forwardRef,
  useEffect,
  useRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type LabelHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "../lib/utils";

const buttonVariants = cva(
  "inline-flex min-h-10 items-center justify-center gap-2 rounded-full border text-sm font-semibold transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vermilion focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        primary:
          "border-transparent bg-vermilion px-4 text-white shadow-cta hover:bg-brand-hover",
        secondary:
          "border-black/10 bg-white px-4 text-ink shadow-sm hover:border-black/25 hover:bg-black hover:text-white dark:border-white/15 dark:bg-white/10 dark:text-white dark:hover:border-white/25 dark:hover:bg-white dark:hover:text-black",
        ghost:
          "border-transparent bg-transparent px-3 text-muted hover:bg-black/5 hover:text-ink dark:hover:bg-white/10 dark:hover:text-white",
        danger:
          "border-[#f0b7b1] bg-transparent px-4 text-[#c93c30] hover:bg-[#fff1ee] dark:border-[#8a3a34] dark:text-[#ff8f85] dark:hover:bg-white/5",
        dark:
          "border-white/15 bg-white/10 px-4 text-white hover:bg-white/15",
      },
      size: {
        sm: "min-h-8 px-3 text-xs",
        md: "",
        lg: "min-h-12 px-5",
        icon: "h-10 w-10 min-h-10 px-0",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  busy?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, busy, children, disabled, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || busy}
      {...props}
    >
      {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : null}
      {children}
    </button>
  ),
);
Button.displayName = "Button";

export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-11 w-full rounded-2xl border border-black/10 bg-black/[0.02] px-4 text-sm text-ink outline-none transition placeholder:text-black/30 focus:border-vermilion focus:ring-2 focus:ring-vermilion/15 disabled:bg-black/[0.03] dark:border-white/10 dark:bg-white/5 dark:placeholder:text-white/30",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "min-h-28 w-full resize-y rounded-2xl border border-black/10 bg-black/[0.02] px-4 py-3 text-sm leading-6 text-ink outline-none transition placeholder:text-black/30 focus:border-vermilion focus:ring-2 focus:ring-vermilion/15 disabled:bg-black/[0.03] dark:border-white/10 dark:bg-white/5 dark:placeholder:text-white/30",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement>
>(({ className, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      "h-11 w-full rounded-2xl border border-black/10 bg-black/[0.02] px-4 text-sm text-ink outline-none transition focus:border-vermilion focus:ring-2 focus:ring-vermilion/15 disabled:bg-black/[0.03] dark:border-white/10 dark:bg-white/5",
      className,
    )}
    {...props}
  />
));
Select.displayName = "Select";

export function Label({
  className,
  children,
  ...props
}: LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn("mb-2 block text-[13px] font-semibold text-ink", className)}
      {...props}
    >
      {children}
    </label>
  );
}

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-card border border-black/[0.06] bg-paper shadow-apple dark:border-white/10 dark:bg-[#101010] dark:shadow-appleDark",
        className,
      )}
      {...props}
    />
  );
}

export function Badge({
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border border-black/10 bg-white/70 px-2.5 py-1 text-[11px] font-semibold leading-none text-muted dark:border-white/10 dark:bg-white/10 dark:text-white/60",
        className,
      )}
      {...props}
    />
  );
}

export interface TabsProps {
  value: string;
  onChange: (value: string) => void;
  items: Array<{ value: string; label: string; count?: number }>;
  className?: string;
}

export function Tabs({ value, onChange, items, className }: TabsProps) {
  return (
    <div
      className={cn(
        "inline-flex max-w-full gap-1 overflow-x-auto rounded-full bg-black/5 p-1 dark:bg-white/10",
        className,
      )}
      role="tablist"
    >
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          aria-selected={value === item.value}
          onClick={() => onChange(item.value)}
          className={cn(
            "whitespace-nowrap rounded-full px-4 py-2 text-sm font-semibold text-muted transition",
            value === item.value &&
              "bg-white text-ink shadow-sm dark:bg-black dark:text-white",
          )}
        >
          {item.label}
          {item.count !== undefined ? (
            <span className="ml-2 text-[11px] text-muted">{item.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  wide,
}: ModalProps) {
  const dialogRef = useRef<HTMLElement | null>(null);
  // 焦点只依赖 open：onClose 常为内联函数（每次渲染引用都会变化），
  // 若放入依赖会导致输入时重渲染反复抢回焦点。
  useEffect(() => {
    if (!open) return;
    // 打开后将焦点移入对话框内的第一个可聚焦元素，避免焦点滞留背景页面导致 Tab 游离。
    const focusable = dialogRef.current?.querySelector<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
    );
    focusable?.focus();
  }, [open]);
  // 使用 ref 读取最新 onClose，避免内联 onClose 导致键盘监听 effect 频繁重建。
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    // 打开时锁定背景滚动，关闭时恢复。
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div
      className="modal-backdrop-in fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4 backdrop-blur-sm"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        className={cn(
          "modal-panel-in flex max-h-[92vh] w-full flex-col overflow-hidden rounded-sheet border border-white/50 bg-paper shadow-[0_30px_120px_rgba(0,0,0,0.22)] dark:border-white/10 dark:bg-[#141414]",
          wide ? "max-w-5xl" : "max-w-2xl",
        )}
      >
        <header className="flex items-start justify-between gap-4 px-6 pb-5 pt-6 md:px-8 md:pt-8">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-vermilion">
              Local production desk
            </p>
            <h2
              id="modal-title"
              className="mt-1.5 text-2xl font-semibold tracking-[-0.04em] text-ink md:text-[28px]"
            >
              {title}
            </h2>
            {description ? (
              <p className="mt-1.5 text-sm leading-6 text-muted">{description}</p>
            ) : null}
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="关闭"
            className="bg-black/5 hover:bg-black/10 dark:bg-white/10 dark:hover:bg-white/15"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6 md:px-8">
          {children}
        </div>
        {footer ? (
          <footer className="flex flex-wrap justify-end gap-3 border-t border-black/[0.06] bg-canvas/60 px-6 py-4 md:px-8 dark:border-white/10 dark:bg-white/[0.04]">
            {footer}
          </footer>
        ) : null}
      </section>
    </div>,
    document.body,
  );
}

export function Divider({ className }: { className?: string }) {
  return <div className={cn("h-px bg-line", className)} />;
}

export function Progress({ value }: { value: number }) {
  const normalized = Math.max(0, Math.min(100, value));
  return (
    <div
      className="h-1.5 overflow-hidden rounded-full bg-black/10 dark:bg-white/10"
      role="progressbar"
      aria-valuenow={normalized}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-vermilion transition-[width] duration-500"
        style={{ width: `${normalized}%` }}
      />
    </div>
  );
}
