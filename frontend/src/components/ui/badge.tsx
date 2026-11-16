import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center justify-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium w-fit whitespace-nowrap shrink-0 [&>svg]:size-3 [&>svg]:pointer-events-none overflow-hidden transition-colors focus-visible:ring-ring/50 focus-visible:ring-[3px]",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        destructive: "border-transparent bg-destructive text-white",
        outline: "border-border text-foreground",
        // 状态徽章：浅底（--success-soft）+ 同色系描边（--success）+ 深字（--success-foreground），
        // 深浅两套由 index.css 的 :root / .dark 提供，页面不再写死 Tailwind 色阶
        success: "border-success/30 bg-success-soft text-success-foreground",
        warning: "border-warning/30 bg-warning-soft text-warning-foreground",
        danger: "border-danger/30 bg-danger-soft text-danger-foreground",
        info: "border-info/30 bg-info-soft text-info-foreground",
      },
      // 11px 是设计稿允许的徽章最小档位；页面侧只选 size，不再散写 text-[11px]。
      // xs = 列表内紧凑徽章（方角）；pill = 胶囊徽章（状态/模式标记）。
      size: {
        default: "",
        xs: "text-[11px] px-1.5 py-0.5 rounded",
        pill: "text-[11px] px-2 py-0.5 rounded-full",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Badge({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot : "span";
  return (
    <Comp
      data-slot="badge"
      className={cn(badgeVariants({ variant, size }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
