import * as React from "react";
import { cn } from "@/lib/utils";

/** 与 Input 同一套外观；调用方传 className 覆盖高度/字体（tailwind-merge 生效） */
function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "w-full rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground outline-none transition-colors",
        "placeholder:text-muted-foreground focus:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
