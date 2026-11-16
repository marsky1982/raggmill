import * as React from "react";
import { cn } from "@/lib/utils";

/** 对应原 index.css 的 `.label`：text-xs / text-muted-foreground / mb-1 / block。
 *  未引入 @radix-ui/react-label —— 现有表单大量 label 没有 htmlFor，radix 版也只是一层 label，加依赖无收益。 */
function Label({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      data-slot="label"
      className={cn("mb-1 block text-xs text-muted-foreground", className)}
      {...props}
    />
  );
}

export { Label };
