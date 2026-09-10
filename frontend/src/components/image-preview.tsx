import { ZoomIn } from "lucide-react";
import { useState } from "react";
import { cn } from "../lib/utils";
import { Modal } from "./ui";

export interface ImagePreviewProps {
  src: string;
  alt: string;
  imageClassName?: string;
  buttonClassName?: string;
  loading?: "eager" | "lazy";
}

export function ImagePreview({
  src,
  alt,
  imageClassName,
  buttonClassName,
  loading,
}: ImagePreviewProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        className={cn(
          "group relative block w-full cursor-zoom-in overflow-hidden text-left",
          buttonClassName,
        )}
        aria-label={`放大${alt}`}
        onClick={() => setOpen(true)}
      >
        <img
          src={src}
          alt={alt}
          className={imageClassName}
          loading={loading}
        />
        <span className="pointer-events-none absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full border border-white/25 bg-black/55 text-white opacity-0 backdrop-blur transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
          <ZoomIn className="h-4 w-4" />
        </span>
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title={alt} wide>
        <div className="flex items-center justify-center rounded-xl bg-[#0b0b0d]"> 
          <img
            src={src}
            alt={`${alt}大图`}
            className="max-h-[calc(92vh-170px)] max-w-full object-contain"
          />
        </div>
      </Modal>
    </>
  );
}
