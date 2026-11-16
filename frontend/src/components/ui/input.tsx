import * as React from "react";
import { cn } from "@/lib/utils";

/** 与 index.css 中原全局 `input` 规则一致：bg-muted + 描边 + focus 变主色，不留视觉差 */
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "w-full rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground outline-none transition-colors",
        "placeholder:text-muted-foreground focus:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
