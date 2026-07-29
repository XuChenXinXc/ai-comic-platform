import { cva, type VariantProps } from "class-variance-authority";
import { LoaderCircle, X } from "lucide-react";
import {
  forwardRef,
  useEffect,
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
  "inline-flex min-h-10 items-center justify-center gap-2 rounded-md border text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vermilion focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        primary:
          "border-vermilion bg-vermilion px-4 text-white hover:bg-[#cf4f2c]",
        secondary:
          "border-line bg-paper px-4 text-ink hover:border-[#bdb8ad] hover:bg-white",
        ghost:
          "border-transparent bg-transparent px-3 text-muted hover:bg-black/5 hover:text-ink",
        danger:
          "border-[#c94a3f] bg-transparent px-4 text-[#a3332a] hover:bg-[#fff1ee]",
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
      "h-11 w-full rounded-md border border-line bg-white px-3 text-sm text-ink outline-none transition placeholder:text-[#999b95] focus:border-vermilion focus:ring-2 focus:ring-vermilion/10 disabled:bg-black/[0.03]",
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
      "min-h-28 w-full resize-y rounded-md border border-line bg-white px-3 py-2.5 text-sm leading-6 text-ink outline-none transition placeholder:text-[#999b95] focus:border-vermilion focus:ring-2 focus:ring-vermilion/10 disabled:bg-black/[0.03]",
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
      "h-11 w-full rounded-md border border-line bg-white px-3 text-sm text-ink outline-none transition focus:border-vermilion focus:ring-2 focus:ring-vermilion/10 disabled:bg-black/[0.03]",
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
      className={cn("mb-1.5 block text-xs font-semibold text-[#555852]", className)}
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
        "rounded-lg border border-line bg-paper shadow-[0_1px_0_rgba(25,26,23,0.02)]",
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
        "inline-flex items-center rounded-full border border-line bg-white px-2.5 py-1 text-[11px] font-semibold leading-none text-muted",
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
        "inline-flex max-w-full gap-1 overflow-x-auto rounded-md border border-line bg-[#e9e6de] p-1",
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
            "whitespace-nowrap rounded px-3 py-2 text-sm font-semibold text-muted transition",
            value === item.value && "bg-paper text-ink shadow-sm",
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
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, open]);

  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[#111513]/55 p-4 backdrop-blur-[2px]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        className={cn(
          "flex max-h-[92vh] w-full flex-col overflow-hidden rounded-xl border border-white/30 bg-paper shadow-2xl",
          wide ? "max-w-5xl" : "max-w-2xl",
        )}
      >
        <header className="flex items-start justify-between border-b border-line px-6 py-5">
          <div>
            <h2 id="modal-title" className="text-xl font-bold tracking-tight text-ink">
              {title}
            </h2>
            {description ? (
              <p className="mt-1 text-sm leading-6 text-muted">{description}</p>
            ) : null}
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="关闭"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">{children}</div>
        {footer ? (
          <footer className="flex flex-wrap justify-end gap-3 border-t border-line bg-white/45 px-6 py-4">
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
      className="h-1.5 overflow-hidden rounded-full bg-black/10"
      role="progressbar"
      aria-valuenow={normalized}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-vermilion transition-[width]"
        style={{ width: `${normalized}%` }}
      />
    </div>
  );
}
